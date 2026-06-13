from typing import Any
from pydantic import BaseModel, Field

from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec, VolumeSpec
from app.orchestration.blueprints.base import ActionHandler, ActionResult, Allocation, Blueprint, StackHealth


class GitBlueprintInputs(BaseModel):
    repo: str = Field(..., title="Repository URL", description="e.g. https://github.com/org/repo.git")
    branch: str = Field("main", title="Branch")
    build_method: str = Field("dockerfile", title="Build Method", description="dockerfile or compose")
    internal_port: int = Field(3000, title="Application Port")
    domain: str = Field("", title="Domain (optional)")
    env: dict[str, str] = Field(default_factory=dict)
    source_id: int | None = Field(None, title="Git Source ID")


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

        # Basic Dockerfile rendering
        if inputs.build_method == "dockerfile":
            svc = ServiceSpec(
                name="web",
                image="hosty-build-target",
                build_repo=inputs.repo,
                build_branch=inputs.branch,
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
        
        # Compose rendering logic is complex and ideally hooks into ComposeBlueprint parsing
        # For now, we fallback to simple web service
        raise NotImplementedError("Dynamic compose generation pending integration")

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
        return ActionResult(ok=True, message="Triggering repository pull and image rebuild in the background.")

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
