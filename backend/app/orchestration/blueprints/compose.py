"""Dynamic Docker Compose Blueprint parser."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec, VolumeSpec
from app.domain.validate import SpecValidationError
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

    def version_inputs(self) -> dict[str, tuple[str, str]]:
        """input name -> (docker repo, default version) for fields that
        should render as a dynamic version dropdown. The default doubles as
        the offline fallback and sets the series granularity."""
        return {
            key: (repo, str(self._inputs_def[key].get("default", "")))
            for key, repo in self._version_inputs.items()
        }

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
            image = _substitute(svc_data.get("image", "hosty-build-target"), subs)

            build_repo = None
            build_branch = None
            raw_build = svc_data.get("build")
            if raw_build and isinstance(raw_build, str):
                build_str = _substitute(raw_build, subs)

                if "#" in build_str:
                    build_repo, build_branch = build_str.split("#", 1)
                else:
                    build_repo = build_str
            elif isinstance(raw_build, dict):
                context = raw_build.get("context")
                if isinstance(context, str) and context.startswith(("https://", "git@", "ssh://")):
                    build_str = _substitute(context, subs)
                    if "#" in build_str:
                        build_repo, build_branch = build_str.split("#", 1)
                    else:
                        build_repo = build_str
                elif not svc_data.get("image"):
                    raise SpecValidationError(
                        f"Service {svc_name!r} uses a local build context without a fallback image"
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

            env_vars = {str(ek): _substitute(ev, subs) for ek, ev in env_vars.items()}

            internal_port = None
            if svc_data.get("ports"):
                raw_port = svc_data["ports"][0]
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

            host_port = alloc.ports.get(svc_name)
            if host_port is not None and internal_port is None:
                raise SpecValidationError(
                    f"Service {svc_name!r} requests a Hosty port but declares no container port"
                )

            services.append(
                ServiceSpec(
                    name=svc_name,
                    image=image,
                    env=tuple(sorted(env_vars.items())),
                    internal_port=internal_port,
                    host_port=host_port,
                    is_web=svc_name == self._web_service,
                    build_repo=build_repo,
                    build_branch=build_branch,
                )
            )

            for vol in svc_data.get("volumes", []):
                if isinstance(vol, dict):
                    if vol.get("type", "volume") != "volume":
                        raise SpecValidationError(
                            f"Service {svc_name!r}: only named volumes are supported"
                        )
                    v_name, v_path = vol.get("source"), vol.get("target")
                else:
                    parts = str(vol).split(":", 2)
                    if len(parts) < 2:
                        raise SpecValidationError(
                            f"Service {svc_name!r} has an invalid volume mapping {vol!r}"
                        )
                    v_name, v_path = parts[0], parts[1]
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
