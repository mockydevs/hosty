"""Export the OpenAPI schema for frontend type generation.

Usage: uv run python scripts/export_openapi.py > ../frontend/openapi.json
"""

from __future__ import annotations

import json
import sys

from app.main import create_app


def main() -> None:
    app = create_app()
    json.dump(app.openapi(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
