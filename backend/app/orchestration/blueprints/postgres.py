"""Blueprint for PostgreSQL database."""

from __future__ import annotations

import secrets
from pydantic import BaseModel, Field

from app.domain.specs import ServiceSpec, StackSpec, VolumeSpec
from app.orchestration.blueprints.base import ActionHandler, Allocation, StackHealth

DB_SERVICE = "db"
DB_VOLUME = "db-data"

class PostgresInputs(BaseModel):
    version: str = Field(default="16", description="PostgreSQL version (e.g. 15, 16)")
    database: str = Field(default="postgres", description="Default database name")
    user: str = Field(default="postgres", description="Superuser name")

class PostgresBlueprint:
    id = "postgres"
    version = 1
    category = "Databases"
    icon = "postgres"
    display_name = "PostgreSQL"
    description = "Deploy a PostgreSQL database."

    def inputs(self) -> type[PostgresInputs]:
        return PostgresInputs

    def ports_needed(self, inputs: PostgresInputs) -> list[str]:
        return [DB_SERVICE]

    def secrets_needed(self, inputs: PostgresInputs) -> list[str]:
        return ["db_password"]

    def render(self, name: str, inputs: PostgresInputs, alloc: Allocation) -> StackSpec:
        db_password = alloc.secrets["db_password"]
        env = {
            "POSTGRES_DB": inputs.database,
            "POSTGRES_USER": inputs.user,
            "POSTGRES_PASSWORD": db_password,
        }
        service = ServiceSpec(
            name=DB_SERVICE,
            image=f"docker.io/library/postgres:{inputs.version}",
            env=tuple(sorted(env.items())),
            internal_port=5432,
            host_port=alloc.ports[DB_SERVICE],
            is_web=False,
        )
        volume = VolumeSpec(name=DB_VOLUME, service=DB_SERVICE, mount_path="/var/lib/postgresql/data")

        return StackSpec(
            name=name,
            tenant=alloc.tenant,
            services=(service,),
            volumes=(volume,),
            endpoints=(),
        )

    def actions(self) -> dict[str, ActionHandler]:
        return {}

    def backup_hooks(self) -> None:
        return None

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        if observed_active.get(DB_SERVICE):
            return StackHealth(healthy=True)
        return StackHealth(healthy=False, detail="db service is not running")
