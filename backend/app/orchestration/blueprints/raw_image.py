"""Blueprint #0: raw-image (v2/M4, ADR-013). Any single OCI image — covers
Django, Next.js, Ghost, anything that listens on one port. The simplest
possible blueprint, and the contract's reference implementation."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec, VolumeSpec
from app.orchestration.blueprints.base import (
    ActionHandler,
    Allocation,
    StackHealth,
)

WEB_SERVICE = "web"


class RawImageInputs(BaseModel):
    image: str = Field(
        description="OCI image reference, e.g. ghcr.io/acme/app:v1",
        max_length=512,
    )
    internal_port: int = Field(ge=1, le=65535, description="Port the app listens on")
    domain: str | None = Field(
        default=None, max_length=253, description="Public domain (HTTPS via Caddy)"
    )
    behind_cloudflare: bool = Field(
        default=False, description="Domain is proxied through Cloudflare"
    )
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    volumes: dict[str, str] = Field(
        default_factory=dict,
        description="Persistent volumes: name -> container mount path",
    )
    memory_mb: int | None = Field(default=None, ge=16, le=1_048_576)
    cpu_percent: int | None = Field(default=None, ge=1, le=6400)


class RawImageBlueprint:
    id = "raw-image"
    version = 1

    def inputs(self) -> type[RawImageInputs]:
        return RawImageInputs

    def ports_needed(self, inputs: RawImageInputs) -> list[str]:
        return [WEB_SERVICE]

    def secrets_needed(self, inputs: RawImageInputs) -> list[str]:
        return []

    def render(self, name: str, inputs: RawImageInputs, alloc: Allocation) -> StackSpec:
        service = ServiceSpec(
            name=WEB_SERVICE,
            image=inputs.image,
            env=tuple(sorted(inputs.env.items())),
            internal_port=inputs.internal_port,
            host_port=alloc.ports[WEB_SERVICE],
            memory_mb=inputs.memory_mb,
            cpu_percent=inputs.cpu_percent,
            is_web=True,
        )
        volumes = tuple(
            VolumeSpec(name=vol_name, service=WEB_SERVICE, mount_path=mount)
            for vol_name, mount in sorted(inputs.volumes.items())
        )
        endpoints = (
            (
                EndpointSpec(
                    domain=inputs.domain,
                    service=WEB_SERVICE,
                    behind_cloudflare=inputs.behind_cloudflare,
                ),
            )
            if inputs.domain
            else ()
        )
        return StackSpec(
            name=name,
            tenant=alloc.tenant,
            services=(service,),
            volumes=volumes,
            endpoints=endpoints,
        )

    def actions(self) -> dict[str, ActionHandler]:
        return {}

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        if observed_active.get(WEB_SERVICE):
            return StackHealth(healthy=True)
        return StackHealth(healthy=False, detail="web service is not running")
