"""Dynamic Docker Compose Blueprint parser."""

from __future__ import annotations

import os
import yaml
from pathlib import Path
from pydantic import BaseModel, Field, create_model

from app.domain.specs import ServiceSpec, StackSpec, VolumeSpec
from app.orchestration.blueprints.base import ActionHandler, Allocation, Blueprint, StackHealth

class ComposeBlueprint:
    def __init__(self, yaml_path: Path):
        self.yaml_path = yaml_path
        with open(yaml_path, "r") as f:
            self.raw_data = yaml.safe_load(f)

        self.id = self.yaml_path.stem
        hosty_meta = self.raw_data.get("x-hosty", {})
        
        self.version = hosty_meta.get("version", 1)
        self.category = hosty_meta.get("category", "Other")
        self.icon = hosty_meta.get("icon", "boxes")
        self.display_name = hosty_meta.get("name", self.id)
        self.description = hosty_meta.get("description", f"Deploy {self.id}")
        
        self._secrets = hosty_meta.get("secrets", [])
        self._inputs_def = hosty_meta.get("inputs", {})
        self._ports = hosty_meta.get("ports", []) # list of service names that need ports
        self._InputsModel = self._build_inputs_model()

    def _build_inputs_model(self) -> type[BaseModel]:
        fields = {}
        for key, spec in self._inputs_def.items():
            type_ = str
            if spec.get("type") == "boolean":
                type_ = bool
            elif spec.get("type") == "number":
                type_ = int
            
            field_kwargs = {}
            if "default" in spec:
                field_kwargs["default"] = spec["default"]
            if "description" in spec:
                field_kwargs["description"] = spec["description"]
            if spec.get("secret"):
                field_kwargs["json_schema_extra"] = {"secret": True}
            
            fields[key] = (type_, Field(**field_kwargs))
            
        return create_model(f"{self.id.capitalize()}Inputs", **fields)

    def inputs(self) -> type[BaseModel]:
        return self._InputsModel

    def ports_needed(self, inputs: BaseModel) -> list[str]:
        return self._ports

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
            # Basic substitution for environment and image
            # In a real implementation we would recursively substitute
            # For this pivot, we handle simple string replacements
            
            image = svc_data.get("image", "hosty-build-target")
            for k, v in subs.items():
                image = image.replace(f"${{{k}}}", v).replace(f"${k}", v)

            build_repo = None
            build_branch = None
            raw_build = svc_data.get("build")
            if raw_build and isinstance(raw_build, str):
                build_str = raw_build
                for k, v in subs.items():
                    build_str = build_str.replace(f"${{{k}}}", v).replace(f"${k}", v)
                
                if "#" in build_str:
                    build_repo, build_branch = build_str.split("#", 1)
                else:
                    build_repo = build_str

            env_vars = {}
            raw_env = svc_data.get("environment", {})
            if isinstance(raw_env, list):
                for e in raw_env:
                    if "=" in e:
                        ek, ev = e.split("=", 1)
                        env_vars[ek] = ev
            elif isinstance(raw_env, dict):
                env_vars = dict(raw_env)

            for ek, ev in env_vars.items():
                if isinstance(ev, str):
                    for k, v in subs.items():
                        ev = ev.replace(f"${{{k}}}", v).replace(f"${k}", v)
                env_vars[ek] = ev
                
            internal_port = None
            if svc_data.get("ports"):
                # Simplistic port parsing
                p = str(svc_data["ports"][0])
                internal_port = int(p.split(":")[-1])
            elif svc_name in alloc.ports:
                # If they asked for a port but didn't list it in compose
                # This could be improved, but usually internal_port is defined
                pass

            # If the service requires a host port (requested via x-hosty.ports)
            host_port = alloc.ports.get(svc_name)

            services.append(ServiceSpec(
                name=svc_name,
                image=image,
                env=tuple(sorted(env_vars.items())),
                internal_port=internal_port,
                host_port=host_port,
                is_web=False, # We can add x-hosty.web: [svc_name] later
                build_repo=build_repo,
                build_branch=build_branch,
            ))

            for vol in svc_data.get("volumes", []):
                # "volume_name:/path/in/container"
                if ":" in vol:
                    v_name, v_path = vol.split(":", 1)
                    volumes.append(VolumeSpec(
                        name=v_name,
                        service=svc_name,
                        mount_path=v_path
                    ))

        return StackSpec(
            name=name,
            tenant=alloc.tenant,
            services=tuple(services),
            volumes=tuple(volumes),
            endpoints=(),
        )

    def actions(self) -> dict[str, ActionHandler]:
        return {}

    def backup_hooks(self) -> None:
        return None

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        for svc_name in self.raw_data.get("services", {}).keys():
            if not observed_active.get(svc_name):
                return StackHealth(healthy=False, detail=f"{svc_name} service is not running")
        return StackHealth(healthy=True)
