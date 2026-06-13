"""Blueprints (v2/M4, ADR-013): typed recipes that render user inputs into
a StackSpec. Registered explicitly — importing this package registers the
built-ins."""

from app.orchestration.blueprints import raw_image as _raw_image
from app.orchestration.blueprints import wordpress as _wordpress
from app.orchestration.blueprints import postgres as _postgres
from app.orchestration.blueprints.base import get_blueprint, list_blueprints, register

__all__ = ["get_blueprint", "list_blueprints", "register"]

register(_raw_image.RawImageBlueprint())
register(_wordpress.WordPressBlueprint())
register(_postgres.PostgresBlueprint())
