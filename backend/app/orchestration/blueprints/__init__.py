"""Blueprints (v2/M4, ADR-013): typed recipes that render user inputs into
a StackSpec. Registered explicitly — importing this package registers the
built-ins."""

from pathlib import Path

from app.orchestration.blueprints import raw_image as _raw_image
from app.orchestration.blueprints import wordpress as _wordpress
from app.orchestration.blueprints.base import get_blueprint, list_blueprints, register
from app.orchestration.blueprints.compose import ComposeBlueprint
from app.orchestration.blueprints.git import GitBlueprint

__all__ = ["get_blueprint", "list_blueprints", "register"]

register(_raw_image.RawImageBlueprint())
register(_wordpress.WordPressBlueprint())
register(GitBlueprint())

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "templates"
if TEMPLATES_DIR.exists():
    for yml_file in TEMPLATES_DIR.glob("*.yml"):
        if get_blueprint(yml_file.stem) is not None:
            continue
        try:
            register(ComposeBlueprint(yml_file))
        except Exception as e:
            print(f"Failed to register template {yml_file.name}: {e}")
