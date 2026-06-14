"""Dynamic Docker Compose Blueprint parser."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec, VolumeSpec
from app.domain.validate import SpecValidationError, validate_build_path
from app.orchestration.blueprints.base import ActionHandler, Allocation, StackHealth

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])(.*?))?\}")
_BARE_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _substitute(value: object, subs: dict[str, str]) -> str:
    text = str(value)

    def braced(match: re.Match[str]) -> str:
        key = match.group(1)
        op = match.group(2)
        fallback = match.group(3) or ""
        current = subs.get(key)
        if op in (":-", "-"):
            return current if current not in (None, "") else fallback
        if op in (":?", "?"):
            if current not in (None, ""):
                return current
            if fallback:
                return fallback
            raise SpecValidationError(f"Missing required template variable {key!r}")
        return current if current is not None else ""

    text = _VAR_RE.sub(braced, text)
    return _BARE_VAR_RE.sub(lambda match: subs.get(match.group(1), ""), text)


class ComposeBlueprint:
    def __init__(self, yaml_path: Path):
        self.yaml_path = yaml_path
        with open(yaml_path, encoding="utf-8") as f:
            self.raw_data = yaml.safe_load(f)
        if not isinstance(self.raw_data, dict):
            raise SpecValidationError(f"Compose template {yaml_path.name!r} must be a mapping")
        services = self.raw_data.get("services")
        if not isinstance(services, dict) or not services:
            raise SpecValidationError(
                f"Compose template {yaml_path.name!r} must declare at least one service"
            )
        for name, service in services.items():
            if not isinstance(service, dict):
                raise SpecValidationError(f"Compose service {name!r} must be a mapping")

        self.id = self.yaml_path.stem
        hosty_meta = self.raw_data.get("x-hosty", {})
        if not isinstance(hosty_meta, dict):
            raise SpecValidationError("x-hosty must be a mapping")

        self.version = hosty_meta.get("version", 1)
        self.category = hosty_meta.get("category", "Other")
        self.icon = hosty_meta.get("icon", "boxes")
        self.display_name = hosty_meta.get("name", self.id)
        self.description = hosty_meta.get("description", f"Deploy {self.id}")

        raw_secrets = hosty_meta.get("secrets", [])
        raw_ports = hosty_meta.get("ports", [])
        if not isinstance(raw_secrets, list) or not all(
            isinstance(value, str) for value in raw_secrets
        ):
            raise SpecValidationError("x-hosty.secrets must be a list of names")
        if not isinstance(raw_ports, list) or not all(
            isinstance(value, str) for value in raw_ports
        ):
            raise SpecValidationError("x-hosty.ports must be a list of service names")
        self._secrets = list(raw_secrets)
        self._inputs_def = hosty_meta.get("inputs", {})
        self._ports = list(raw_ports)
        if not isinstance(self._inputs_def, dict):
            raise SpecValidationError("x-hosty.inputs must be a mapping")
        self._web_service = hosty_meta.get("web")
        self._domain_input = hosty_meta.get("domain_input")
        # Optional connection metadata: how to build a `scheme://user:pass@
        # host:port/db` link for a DB service (services/stacks.connection_links).
        conn = hosty_meta.get("connection")
        if conn is not None and not isinstance(conn, dict):
            raise SpecValidationError("x-hosty.connection must be a mapping")
        self._connection = conn
        # Inputs that should render as a registry-sourced version dropdown:
        # `versions_from: <docker repo>` on the input. The list of series is
        # fetched + cached at form-render time (services/image_versions.py).
        self._version_inputs = {
            key: str(spec["versions_from"])
            for key, spec in self._inputs_def.items()
            if isinstance(spec, dict) and spec.get("versions_from")
        }
        self._InputsModel = self._build_inputs_model()

    def _build_inputs_model(self) -> type[BaseModel]:
        fields = {}
        for key, spec in self._inputs_def.items():
            if not isinstance(spec, dict):
                raise SpecValidationError(f"Input definition {key!r} must be a mapping")
            type_ = str
            if spec.get("type") == "boolean":
                type_ = bool
            elif spec.get("type") in ("number", "integer"):
                type_ = int

            field_kwargs = {}
            if "default" in spec:
                field_kwargs["default"] = spec["default"]
            if "description" in spec:
                field_kwargs["description"] = spec["description"]
            if spec.get("secret"):
                field_kwargs["json_schema_extra"] = {"secret": True}

            fields[key] = (type_, Field(**field_kwargs))

        return create_model(
            f"{self.id.capitalize()}Inputs",
            __config__=ConfigDict(title=self.display_name),
            **fields,
        )

    def inputs(self) -> type[BaseModel]:
        return self._InputsModel

    def connection_meta(self) -> dict | None:
        """How to render a DB connection link: {scheme, service, user_env?,
        password_env?, database_env?}. None for stacks with no DB endpoint."""
        return self._connection

    def version_inputs(self) -> dict[str, tuple[str, str]]:
        """input name -> (docker repo, default version) for fields that
        should render as a dynamic version dropdown. The default doubles as
        the offline fallback and sets the series granularity."""
        return {
            key: (repo, str(self._inputs_def[key].get("default", "")))
            for key, repo in self._version_inputs.items()
        }

    def secrets_needed(self, inputs: BaseModel) -> list[str]:
        return self._secrets

    def render(self, name: str, inputs: BaseModel, alloc: Allocation) -> StackSpec:
        services_data = self.raw_data.get("services", {})
        services = []
        volumes = []

        # Build substitution dictionary
        subs = {}
        for k, v in alloc.secrets.items():
            subs[k] = v
        for k, v in dict(inputs).items():
            subs[k] = str(v)

        for svc_name, svc_data in services_data.items():
            image = _substitute(svc_data.get("image", "hosty-build-target"), subs)

            build_repo = None
            build_branch = None
            build_context = "."
            dockerfile_path = "Dockerfile"
            build_args: dict[str, str] = {}
            raw_build = svc_data.get("build")
            if raw_build and isinstance(raw_build, str):
                build_str = _substitute(raw_build, subs)
                if build_str.startswith(("https://", "git@", "ssh://")):
                    # Remote git URL — use as build_repo
                    if "#" in build_str:
                        build_repo, build_branch = build_str.split("#", 1)
                    else:
                        build_repo = build_str
                else:
                    # Local path like "." or "./subdir" — marker for git.py blueprint to fill in
                    image = "hosty-build-target"
                    build_repo = None
                    build_context = build_str
            elif isinstance(raw_build, dict):
                context = raw_build.get("context")
                if isinstance(context, str) and context.startswith(("https://", "git@", "ssh://")):
                    build_str = _substitute(context, subs)
                    if "#" in build_str:
                        build_repo, build_branch = build_str.split("#", 1)
                    else:
                        build_repo = build_str
                else:
                    # Local build context dict (context: .) — marker for git.py blueprint
                    image = "hosty-build-target"
                    build_repo = None
                    build_context = _substitute(context or ".", subs)
                    dockerfile_path = _substitute(raw_build.get("dockerfile", "Dockerfile"), subs)
                    raw_args = raw_build.get("args", {})
                    if isinstance(raw_args, dict):
                        for key, value in raw_args.items():
                            build_args[str(key)] = (
                                subs.get(str(key), "")
                                if value is None
                                else _substitute(value, subs)
                            )
                    elif isinstance(raw_args, list):
                        for raw_arg in raw_args:
                            key, separator, value = str(raw_arg).partition("=")
                            build_args[key] = (
                                _substitute(value, subs) if separator else subs.get(key, "")
                            )
                    elif raw_args:
                        raise SpecValidationError(
                            f"Service {svc_name!r} build args must be a mapping or list"
                        )

            env_vars = {}
            raw_env = svc_data.get("environment", {})
            if isinstance(raw_env, list):
                for e in raw_env:
                    item = str(e)
                    if "=" in item:
                        ek, ev = item.split("=", 1)
                        env_vars[ek] = ev
                    else:
                        env_vars[item] = subs.get(item, "")
            elif isinstance(raw_env, dict):
                env_vars = dict(raw_env)

            # Inject global shared variables unless the service overrides them.
            for gk, gv in alloc.shared_variables.items():
                if gk not in env_vars:
                    env_vars[gk] = gv

            env_vars = {str(ek): _substitute(ev, subs) for ek, ev in env_vars.items()}

            internal_port = None
            published_ports = svc_data.get("ports") or svc_data.get("expose")
            if published_ports:
                raw_port = published_ports[0]
                if isinstance(raw_port, dict):
                    raw_port = raw_port.get("target")
                if raw_port is None:
                    raise SpecValidationError(f"Service {svc_name!r} has an invalid port mapping")
                p = _substitute(raw_port, subs)
                try:
                    internal_port = int(p.rsplit(":", 1)[-1].split("/", 1)[0])
                except ValueError as exc:
                    raise SpecValidationError(
                        f"Service {svc_name!r} has an invalid container port {p!r}"
                    ) from exc

            # Parse depends_on — supports both list and dict (condition) forms.
            raw_deps = svc_data.get("depends_on", [])
            if isinstance(raw_deps, list):
                dep_names = tuple(str(d) for d in raw_deps)
            elif isinstance(raw_deps, dict):
                dep_names = tuple(raw_deps.keys())
            else:
                dep_names = ()

            # Parse command — supports string ("cmd arg1") and list forms.
            raw_cmd = svc_data.get("command")
            if isinstance(raw_cmd, list):
                cmd_tuple: tuple[str, ...] = tuple(_substitute(str(w), subs) for w in raw_cmd)
            elif isinstance(raw_cmd, str):
                cmd_tuple = tuple(shlex.split(_substitute(raw_cmd, subs)))
            else:
                cmd_tuple = ()

            services.append(
                ServiceSpec(
                    name=svc_name,
                    image=image,
                    env=tuple(sorted(env_vars.items())),
                    internal_port=internal_port,
                    is_web=svc_name == self._web_service,
                    build_repo=build_repo,
                    build_branch=build_branch,
                    build_context=validate_build_path(
                        build_context, what="build context", allow_dot=True
                    ),
                    dockerfile_path=validate_build_path(dockerfile_path, what="Dockerfile path"),
                    build_args=tuple(sorted(build_args.items())),
                    memory_mb=int(inputs.memory_limit)
                    if hasattr(inputs, "memory_limit") and inputs.memory_limit
                    else None,
                    cpu_percent=int(inputs.cpu_limit)
                    if hasattr(inputs, "cpu_limit") and inputs.cpu_limit
                    else None,
                    depends_on=dep_names,
                    command=cmd_tuple,
                )
            )

            for vol in svc_data.get("volumes", []):
                if isinstance(vol, dict):
                    vol_type = vol.get("type", "volume")
                    if vol_type != "volume":
                        # bind/tmpfs/npipe mounts are skipped — rootless Podman
                        # does not support arbitrary host bind mounts safely.
                        continue
                    v_name, v_path = vol.get("source"), vol.get("target")
                else:
                    parts = str(vol).split(":", 2)
                    if len(parts) < 2:
                        raise SpecValidationError(
                            f"Service {svc_name!r} has an invalid volume mapping {vol!r}"
                        )
                    v_name, v_path = parts[0], parts[1]
                    # Host bind mounts start with / or ./ — skip silently.
                    if str(v_name).startswith("/") or str(v_name).startswith("."):
                        continue
                if not v_name or not v_path:
                    continue
                volumes.append(
                    VolumeSpec(name=str(v_name), service=svc_name, mount_path=str(v_path))
                )

        endpoints = ()
        if self._web_service and self._domain_input:
            domain = subs.get(self._domain_input, "").strip().lower()
            if domain:
                endpoints = (EndpointSpec(domain=domain, service=self._web_service),)

        return StackSpec(
            name=name,
            tenant=alloc.tenant,
            loopback_ip=alloc.loopback_ip,
            services=tuple(services),
            volumes=tuple(volumes),
            endpoints=endpoints,
        )

    def actions(self) -> dict[str, ActionHandler]:
        return {}

    def backup_hooks(self) -> None:
        return None

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        for svc_name in self.raw_data.get("services", {}):
            if not observed_active.get(svc_name):
                return StackHealth(healthy=False, detail=f"{svc_name} service is not running")
        return StackHealth(healthy=True)
