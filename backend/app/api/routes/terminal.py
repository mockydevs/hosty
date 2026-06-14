"""WebSocket terminal for container exec (v2/M5+).

Each connection opens a PTY-backed shell inside the target container.
Auth uses a JWT access-token passed as ?token= (HTTP Authorization headers
are unavailable during the WebSocket upgrade handshake).

PTY bridge uses loop.add_reader() rather than a thread or preexec_fn because
production runs uvloop, which rejects extra kwargs on create_subprocess_exec.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

if sys.platform != "win32":
    import fcntl
    import pty
    import struct
    import termios

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import UnauthorizedError
from app.core.security import (
    create_terminal_ticket,
    decode_access_token,
    decode_terminal_ticket,
)
from app.db.models import Stack, Tenant, User
from app.domain.validate import validate_object_name

router = APIRouter()

_SUPPORTED = sys.platform != "win32"


class TerminalTicketResponse:
    def __init__(self, ticket: str) -> None:
        self.ticket = ticket


async def _auth_user_from_ticket(
    websocket: WebSocket, db: AsyncSession, token: str, stack_id: int, service_name: str
) -> User | None:
    """Validate a terminal ticket OR fall back to a full access token."""
    settings = websocket.app.state.settings
    try:
        payload = decode_terminal_ticket(
            token, secret=settings.secret_key, stack_id=stack_id, service_name=service_name
        )
    except (UnauthorizedError, Exception):
        # Fall back to full access token (backward compat / direct API use)
        try:
            payload = decode_access_token(token, secret=settings.secret_key)
        except (UnauthorizedError, Exception):
            return None
    user = await db.get(User, int(payload["sub"]))
    if user is None or user.suspended:
        return None
    if "ver" in payload and payload.get("ver") != user.token_version:
        return None
    return user


@router.get("/stacks/{stack_id}/terminal/{service_name}/ticket")
async def issue_terminal_ticket(
    stack_id: int,
    service_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Issue a 30-second single-purpose token for the WebSocket terminal upgrade.
    Use this token as ?token= instead of the full access JWT so it is not logged."""
    from app.core.errors import ConflictError, NotFoundError

    stack = await db.get(Stack, stack_id)
    if stack is None or (stack.owner_id != user.id and user.role != "admin"):
        raise NotFoundError("Stack not found")
    try:
        validate_object_name(f"{stack.name}-{service_name}")
    except Exception:
        raise ConflictError("Invalid service name")
    settings = request.app.state.settings
    ticket = create_terminal_ticket(
        subject=str(user.id),
        stack_id=stack_id,
        service_name=service_name,
        secret=settings.secret_key,
    )
    return {"ticket": ticket}


@router.websocket("/stacks/{stack_id}/terminal/{service_name}")
async def stack_container_terminal(
    websocket: WebSocket,
    stack_id: int,
    service_name: str,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """PTY-backed interactive shell inside a stack's container.

    Binary frames: raw PTY I/O.
    Text frames:   JSON control messages — {"type":"resize","cols":N,"rows":N}
    Server text:   {"type":"error","message":"..."} on fatal errors.
    """
    if not _SUPPORTED:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({"type": "error", "message": "Terminal not supported on this host OS"})
        )
        await websocket.close(code=4500)
        return

    user = await _auth_user_from_ticket(websocket, db, token, stack_id, service_name)
    if user is None:
        await websocket.close(code=4001)
        return

    stack = await db.get(Stack, stack_id)
    if stack is None or (stack.owner_id != user.id and user.role != "admin"):
        await websocket.close(code=4004)
        return

    # Resolve the tenant uid — needed for the Podman socket path.
    owner_id = stack.owner_id if stack.owner_id is not None else user.id
    tenant = (
        await db.execute(select(Tenant).where(Tenant.user_id == owner_id))
    ).scalar_one_or_none()
    if tenant is None or tenant.uid is None:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({"type": "error", "message": "Tenant not provisioned — deploy the stack first"})
        )
        await websocket.close(code=4500)
        return

    try:
        container_name = validate_object_name(f"{stack.name}-{service_name}")
    except Exception:
        await websocket.close(code=4400)
        return

    argv = [
        "podman",
        "--url",
        f"unix:/run/user/{tenant.uid}/podman/podman.sock",
        "exec",
        "--tty",
        "--interactive",
        container_name,
        "/bin/sh",
    ]

    await websocket.accept()

    master_fd, slave_fd = pty.openpty()

    # Initial terminal size: 80×24
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    # Non-blocking so the reader callback never stalls the event loop.
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_pty_readable() -> None:
        try:
            data = os.read(master_fd, 4096)
            output_queue.put_nowait(data)
        except OSError:
            loop.remove_reader(master_fd)
            output_queue.put_nowait(None)

    loop.add_reader(master_fd, _on_pty_readable)

    try:
        # NOTE: no preexec_fn or extra kwargs — uvloop rejects them.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
    except Exception as exc:
        loop.remove_reader(master_fd)
        os.close(master_fd)
        os.close(slave_fd)
        await websocket.send_text(
            json.dumps({"type": "error", "message": f"Failed to start terminal: {exc}"})
        )
        await websocket.close(code=4500)
        return

    os.close(slave_fd)

    async def _send_output() -> None:
        while True:
            data = await output_queue.get()
            if data is None:
                break
            try:
                await websocket.send_bytes(data)
            except Exception:
                break

    async def _handle_input() -> None:
        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type", "")
                if msg_type == "websocket.disconnect":
                    break
                if msg_type == "websocket.receive":
                    raw_bytes = msg.get("bytes")
                    raw_text = msg.get("text")
                    if raw_bytes:
                        try:
                            os.write(master_fd, raw_bytes)
                        except OSError:
                            break
                    elif raw_text:
                        try:
                            ctrl = json.loads(raw_text)
                            if ctrl.get("type") == "resize":
                                cols = max(1, int(ctrl.get("cols", 80)))
                                rows = max(1, int(ctrl.get("rows", 24)))
                                fcntl.ioctl(
                                    master_fd,
                                    termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0),
                                )
                        except Exception:
                            pass
        except Exception:
            pass

    output_task = asyncio.create_task(_send_output())
    input_task = asyncio.create_task(_handle_input())

    _done, pending = await asyncio.wait(
        [output_task, input_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    loop.remove_reader(master_fd)
    try:
        os.close(master_fd)
    except OSError:
        pass

    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    try:
        await websocket.close()
    except Exception:
        pass
