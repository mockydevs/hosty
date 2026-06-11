"""System layer: the ONLY part of the codebase that touches the host OS.

Rules (see docs/ARCHITECTURE.md ADR-005):
- every command goes through `runner.run` (argv lists, never a shell)
- command builders are pure functions with exact-argv unit tests
- all identifiers (unit names, usernames) are validated against strict allowlists
"""
