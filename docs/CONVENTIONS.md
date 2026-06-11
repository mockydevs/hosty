# Conventions

## Commits

Conventional Commits: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `ci:`.
Small, focused commits; the repo must be releasable after every merge to `main`.

## Branching

Trunk-based: short-lived branches off `main`, merged via PR with green CI.
No long-running feature branches.

## Python

- Ruff is the single source of truth for lint + format (line length 100).
- Full type hints on all public functions; `from __future__ import annotations` everywhere.
- Pydantic models at API boundaries; SQLAlchemy 2.0 typed `Mapped[...]` models.
- No `subprocess` outside `app/system/runner.py`. No `shell=True` anywhere. CI enforces both.
- Errors: raise `AppError` subclasses; never return ad-hoc error dicts.
- Naming: modules/functions `snake_case`, classes `PascalCase`, constants `UPPER_SNAKE`.

## Testing

- Every command builder: unit test asserting **exact argv**, plus injection-attempt cases.
- Every endpoint: at least success + auth-required + validation-failure cases.
- Integration tests that need a privileged VM are marked `@pytest.mark.vm`
  (excluded by default, run with `pytest -m vm` on the dev VM).
- No TODO comments without a tracked task in TASKS.md.

## API design

- All routes under `/api`. JSON only.
- Errors use one envelope: `{"error": {"code", "message", "details"}}`.
- Mutations are idempotent where the operation allows it.
- 401 = not authenticated, 403 = authenticated but forbidden, 404 = unknown
  resource (also for resources outside the managed allowlist), 409 = conflict,
  422 = validation, 429 = rate-limited, 502 = downstream system failure.
