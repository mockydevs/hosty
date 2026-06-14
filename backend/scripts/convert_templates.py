#!/usr/bin/env python3
"""Convert Coolify compose templates to Hosty x-hosty format.

Usage:
    python scripts/convert_coolify_templates.py [--overwrite] [--dry-run]

Reads all *.yaml files from COOLIFY_DIR, converts each to Hosty's x-hosty
format, and writes *.yml files to HOSTY_TEMPLATES_DIR. Existing files are
skipped unless --overwrite is passed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

COOLIFY_DIR = Path(r"C:\Users\Master\Code\coolify\templates\compose")
HOSTY_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# ── Category / icon mappings ───────────────────────────────────────────────

CATEGORY_MAP: dict[str, str] = {
    "cms": "CMS",
    "automation": "Automation",
    "storage": "Storage",
    "monitoring": "Monitoring",
    "observability": "Monitoring",
    "ai": "AI",
    "backend": "Applications",
    "git": "Developer Tools",
    "database": "Databases",
    "databases": "Databases",
    "analytics": "Analytics",
    "helpdesk": "Applications",
    "messaging": "Applications",
    "communication": "Applications",
    "security": "Security",
    "auth": "Security",
    "email": "Applications",
    "productivity": "Applications",
    "finance": "Applications",
    "media": "Media",
    "devtools": "Developer Tools",
    "developer tools": "Developer Tools",
    "developer-tools": "Developer Tools",
    "development": "Developer Tools",
    "api": "Developer Tools",
    "ci": "Developer Tools",
    "cicd": "Developer Tools",
    "ci/cd": "Developer Tools",
    "networking": "Applications",
    "network": "Applications",
    "proxy": "Applications",
    "vpn": "Security",
    "games": "Applications",
    "gaming": "Applications",
    "documentation": "Applications",
    "search": "Search",
    "mcp": "AI",
    "rss": "Applications",
    "health": "Applications",
    "other": "Applications",
    "queues": "Queues",
    "queue": "Queues",
    "web servers": "Web Servers",
    "web-servers": "Web Servers",
    "web": "Web Servers",
}

ICON_MAP: dict[str, str] = {
    "CMS": "file-text",
    "Automation": "zap",
    "Storage": "hard-drive",
    "Monitoring": "activity",
    "AI": "brain-circuit",
    "Applications": "box",
    "Developer Tools": "code-2",
    "Databases": "database",
    "Analytics": "bar-chart-2",
    "Security": "shield",
    "Media": "film",
    "Search": "search",
    "Queues": "message-square",
    "Web Servers": "globe",
}

# ── Regex patterns ─────────────────────────────────────────────────────────

# Matches SERVICE_URL_NAME_PORT or SERVICE_FQDN_NAME_PORT as standalone env
# entries (no `=` means it is a Coolify marker, not a value assignment).
MARKER_LINE_RE = re.compile(
    r"^([ \t]*-[ \t]+)(SERVICE_(?:URL|FQDN)_[A-Z0-9_]+(?:_\d+)?)([ \t]*)$",
    re.MULTILINE,
)

# Matches $SERVICE_TYPE_NAME or ${SERVICE_TYPE_NAME} in value contexts.
# Order of alternation matters: PASSWORD_64 before PASSWORD.
MAGIC_RE = re.compile(
    r"\$\{?SERVICE_(PASSWORD_64|PASSWORD|USER|FQDN|URL)_([A-Z0-9]+)\}?"
)


# ── Helper data types ──────────────────────────────────────────────────────

class ConversionResult(NamedTuple):
    web_service: str | None
    web_port: int | None
    secrets: list[str]
    inputs: dict[str, dict]
    converted_yaml: str  # YAML body after substitution + marker removal


# ── Core logic ─────────────────────────────────────────────────────────────

def parse_coolify_comments(content: str) -> dict[str, str]:
    """Extract key: value metadata from leading comment lines."""
    meta: dict[str, str] = {}
    for line in content.splitlines():
        if not line.startswith("#"):
            break
        m = re.match(r"^#\s*(\w+):\s*(.+)", line)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip()
    return meta


def strip_comment_lines(content: str) -> str:
    """Remove leading # comment lines (metadata header only)."""
    lines = content.splitlines(keepends=True)
    result = []
    in_header = True
    for line in lines:
        if in_header and line.startswith("#"):
            continue
        in_header = False
        result.append(line)
    return "".join(result)


def find_web_service(content: str) -> tuple[str | None, int | None]:
    """Find the HTTP-facing service by locating the SERVICE_URL_NAME_PORT marker.

    Returns (service_name, port) or (None, None).
    """
    yaml_str = strip_comment_lines(content)
    try:
        data = yaml.safe_load(yaml_str) or {}
    except yaml.YAMLError:
        return None, None

    services = data.get("services") or {}
    for svc_name, svc_config in services.items():
        if not isinstance(svc_config, dict):
            continue
        env = svc_config.get("environment") or []
        if not isinstance(env, list):
            continue
        for entry in env:
            if not isinstance(entry, str):
                continue
            m = re.match(r"^SERVICE_(?:URL|FQDN)_([A-Z0-9]+)_(\d+)$", entry.strip())
            if m:
                return svc_name, int(m.group(2))
    return None, None


def collect_and_substitute(yaml_body: str) -> tuple[str, list[str], dict[str, dict]]:
    """Scan for SERVICE_* magic variables, build secrets/inputs, do substitution."""
    secrets: list[str] = []
    inputs: dict[str, dict] = {}

    def replace(m: re.Match) -> str:
        kind = m.group(1)  # PASSWORD_64 | PASSWORD | USER | FQDN | URL
        name = m.group(2).lower()  # e.g. mysql, postgresql, n8n

        if kind == "PASSWORD_64":
            key = f"{name}_key"
            if key not in secrets:
                secrets.append(key)
            return f"${{{key}}}"

        if kind == "PASSWORD":
            key = f"{name}_password"
            if key not in secrets:
                secrets.append(key)
            return f"${{{key}}}"

        if kind == "USER":
            key = f"{name}_user"
            if key not in inputs:
                inputs[key] = {
                    "type": "string",
                    "default": name.split("_")[0],
                    "description": f"{name.replace('_', ' ').title()} username",
                }
            return f"${{{key}}}"

        # FQDN or URL → public domain input
        if "domain" not in inputs:
            inputs["domain"] = {
                "type": "string",
                "description": "Public domain (e.g. https://app.example.com)",
            }
        return "${domain}"

    converted = MAGIC_RE.sub(replace, yaml_body)
    return converted, secrets, inputs


def remove_marker_lines(content: str) -> str:
    """Delete standalone SERVICE_URL/FQDN_NAME[_PORT] env entries (no = value)."""
    return MARKER_LINE_RE.sub("", content)


def inject_web_port(yaml_body: str, web_service: str, port: int) -> str:
    """Insert 'ports: - PORT:PORT' into the web service block if it has no ports."""
    # Parse, add port, re-dump.  Formatting changes but content is correct.
    try:
        data = yaml.safe_load(yaml_body) or {}
    except yaml.YAMLError:
        return yaml_body  # can't parse, leave as-is

    services = data.get("services") or {}
    svc = services.get(web_service)
    if not isinstance(svc, dict):
        return yaml_body

    if not svc.get("ports"):
        svc["ports"] = [f"{port}:{port}"]
        return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return yaml_body  # already has ports


def build_display_name(stem: str) -> str:
    """Convert file stem to a human-readable display name."""
    name = stem.replace("-", " ").replace("_", " ")
    # Title-case each word, preserve known acronyms
    words = []
    acronyms = {"ai", "cms", "db", "sql", "php", "api", "ui", "url", "ide"}
    for w in name.split():
        words.append(w.upper() if w.lower() in acronyms else w.capitalize())
    return " ".join(words)


def build_hosty_header(
    meta: dict[str, str],
    stem: str,
    web_service: str | None,
    secrets: list[str],
    inputs: dict[str, dict],
) -> str:
    """Produce the x-hosty: YAML block as a string."""
    # Coolify sometimes uses comma-separated categories; take the first
    category_raw = meta.get("category", "other").lower().strip().split(",")[0].strip()
    category = CATEGORY_MAP.get(category_raw, CATEGORY_MAP.get(category_raw.replace("-", " "), category_raw.title()))
    icon = ICON_MAP.get(category, "box")
    name = build_display_name(stem)
    slogan = meta.get("slogan", f"Deploy {name}.")
    # Truncate and sanitize description
    if len(slogan) > 200:
        slogan = slogan[:197] + "..."
    slogan = slogan.replace('"', "'")

    lines = [
        "x-hosty:",
        f'  name: "{name}"',
        "  version: 1",
        f'  category: "{category}"',
        f'  icon: "{icon}"',
        f'  description: "{slogan}"',
    ]

    if web_service:
        lines.append(f"  web: {web_service}")
        if "domain" in inputs:
            lines.append("  domain_input: domain")

    if secrets:
        lines.append("  secrets:")
        for s in secrets:
            lines.append(f"    - {s}")

    if inputs:
        lines.append("  inputs:")
        for key, spec in inputs.items():
            lines.append(f"    {key}:")
            lines.append(f'      type: "{spec["type"]}"')
            if "default" in spec:
                default = str(spec["default"]).replace('"', "'")
                lines.append(f'      default: "{default}"')
            desc = spec.get("description", "").replace('"', "'")
            lines.append(f'      description: "{desc}"')

    return "\n".join(lines) + "\n"


def convert_template(source: Path) -> str | None:
    """Convert a Coolify YAML file to Hosty x-hosty format.

    Returns None if the template should be skipped (ignore flag or invalid YAML).
    """
    try:
        content = source.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  [READ ERROR] {source.name}: {e}", file=sys.stderr)
        return None

    meta = parse_coolify_comments(content)

    if meta.get("ignore", "").lower() == "true":
        return None  # Coolify hides this template; skip

    web_service, web_port = find_web_service(content)

    # Work on the YAML body (no comment lines)
    yaml_body = strip_comment_lines(content)

    # Quick sanity-check: must parse as valid YAML with services:
    try:
        check = yaml.safe_load(yaml_body) or {}
    except yaml.YAMLError as e:
        print(f"  [YAML ERROR] {source.name}: {e}", file=sys.stderr)
        return None

    if "services" not in check:
        print(f"  [SKIP] {source.name}: no services: block", file=sys.stderr)
        return None

    # If web service found but has no ports, inject them
    if web_service and web_port:
        yaml_body = inject_web_port(yaml_body, web_service, web_port)

    # Remove standalone SERVICE_URL/FQDN marker lines
    yaml_body = remove_marker_lines(yaml_body)

    # Substitute SERVICE_* magic variables
    yaml_body, secrets, inputs = collect_and_substitute(yaml_body)

    # Clean up extra blank lines left by removed markers
    yaml_body = re.sub(r"\n{3,}", "\n\n", yaml_body)

    header = build_hosty_header(meta, source.stem, web_service, secrets, inputs)

    return header + "\n" + yaml_body.strip() + "\n"


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing templates")
    parser.add_argument("--dry-run", action="store_true", help="Print stats without writing files")
    args = parser.parse_args()

    if not COOLIFY_DIR.exists():
        sys.exit(f"Coolify templates directory not found: {COOLIFY_DIR}")

    HOSTY_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in HOSTY_TEMPLATES_DIR.glob("*.yml")}

    sources = sorted(COOLIFY_DIR.glob("*.yaml"))
    print(f"Found {len(sources)} Coolify templates in {COOLIFY_DIR}")

    converted = skipped_existing = ignored = errors = 0

    for source in sources:
        stem = source.stem
        dest = HOSTY_TEMPLATES_DIR / f"{stem}.yml"

        if not args.overwrite and stem in existing:
            skipped_existing += 1
            continue

        result = convert_template(source)
        if result is None:
            ignored += 1
            continue

        if args.dry_run:
            converted += 1
            continue

        try:
            dest.write_text(result, encoding="utf-8")
            converted += 1
        except Exception as e:
            print(f"  [WRITE ERROR] {dest}: {e}", file=sys.stderr)
            errors += 1

    print(
        f"\nResults:"
        f"\n  Converted : {converted}"
        f"\n  Skipped (existing): {skipped_existing}"
        f"\n  Ignored (# ignore or no services): {ignored}"
        f"\n  Errors    : {errors}"
    )


if __name__ == "__main__":
    main()
