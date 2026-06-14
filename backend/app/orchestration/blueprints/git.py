import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, create_model

from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec
from app.domain.validate import SpecValidationError
from app.orchestration.blueprints.base import (
    ActionHandler,
    ActionResult,
    Allocation,
    Blueprint,
    StackHealth,
)


class GitBlueprintInputs(BaseModel):
    repo: str = Field(
        ..., title="Repository URL", description="e.g. https://github.com/org/repo.git"
    )
    branch: str = Field("main", title="Branch")
    build_method: str = Field(
        "dockerfile", title="Build Method", description="dockerfile, compose, or nixpacks"
    )
    internal_port: int = Field(3000, title="Application Port")
    domain: str = Field("", title="Domain (optional)")
    env: dict[str, str] = Field(default_factory=dict)
    source_id: int | None = Field(None, title="Git Source ID")
    compose_file_content: str = Field("", title="Compose File Content")


class GitBlueprint(Blueprint):
    id = "git"
    version = 1
    category = "Custom"
    icon = "git-branch"
    display_name = "Git Repository"
    description = "Deploy from a public or private Git repository"

    def inputs(self) -> type[BaseModel]:
        return GitBlueprintInputs

    def secrets_needed(self, inputs: BaseModel) -> list[str]:
        return []

    def render(self, name: str, inputs: BaseModel, alloc: Allocation) -> StackSpec:
        assert isinstance(inputs, GitBlueprintInputs)

        # Basic Dockerfile or Nixpacks rendering
        if inputs.build_method in ("dockerfile", "nixpacks"):
            svc = ServiceSpec(
                name="web",
                image="hosty-build-target",
                build_repo=inputs.repo,
                build_branch=inputs.branch,
                build_tool=inputs.build_method,
                internal_port=inputs.internal_port,
                env=tuple(sorted(inputs.env.items())),
                is_web=True,
            )

            endpoints = []
            if inputs.domain:
                endpoints.append(EndpointSpec(domain=inputs.domain, service="web"))

            return StackSpec(
                name=name,
                tenant=alloc.tenant,
                loopback_ip=alloc.loopback_ip,
                services=(svc,),
                endpoints=tuple(endpoints),
            )

        # Compose rendering
        if inputs.build_method == "compose":
            from app.orchestration.blueprints.compose import ComposeBlueprint

            if not inputs.compose_file_content:
                raise SpecValidationError("Docker Compose build method requires a compose file")

            with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
                f.write(inputs.compose_file_content)
                temp_path = f.name

            try:
                compose_bp = ComposeBlueprint(Path(temp_path))

                # Create dummy inputs from the env dictionary to satisfy render substitution
                DummyInputs = create_model(
                    "DummyInputs", **{k: (str, v) for k, v in inputs.env.items()}
                )
                dummy_inputs = DummyInputs(**inputs.env)

                spec = compose_bp.render(name, dummy_inputs, alloc)

                # Override build repository for services that should be built from this repo
                # In standard Compose setups from Git, `build: .` is common. The ComposeBlueprint
                # will set build_repo="." or similar. We overwrite any non-None build_repo or
                # hosty-build-target image to use this Git repository.
                new_services = []
                for svc in spec.services:
                    if svc.build_repo or svc.image in (
                        "hosty-build-target",
                        "docker.io/library/hosty-build-target",
                    ):
                        svc = replace(
                            svc,
                            build_repo=inputs.repo,
                            build_branch=inputs.branch,
                            build_tool="dockerfile",
                        )
                    new_services.append(svc)

                return replace(spec, services=tuple(new_services))
            finally:
                Path(temp_path).unlink(missing_ok=True)

        raise SpecValidationError(f"Unknown build method: {inputs.build_method}")

    async def rebuild(
        self,
        db: Any,
        settings: Any,
        stack: Any,
        inputs: BaseModel,
        params: dict[str, str],
    ) -> ActionResult:
        # Re-converge with bumped generation to force unit execution
        stack.generation += 1
        return ActionResult(
            ok=True, message="Triggering repository pull and image rebuild in the background."
        )

    def actions(self) -> dict[str, ActionHandler]:
        return {
            "rebuild": self.rebuild,
        }

    def backup_hooks(self) -> None:
        return None

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        if not observed_active:
            return StackHealth(healthy=False, detail="No services defined")
        all_active = all(observed_active.values())
        return StackHealth(
            healthy=all_active,
            detail="All services running" if all_active else "Some services stopped",
        )
