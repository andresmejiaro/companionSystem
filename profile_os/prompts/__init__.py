"""Inspectable prompt seeds for companion profiles.

``<profile>_base.md`` and ``<profile>_role.md`` seed the backend store.
System-owned contracts are injected by ``start_session`` and are not managed
as part of an individual companion's identity prompt.
"""

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load(name: str) -> str:
    """Return the text of a prompt file, e.g. load('sidra_base')."""
    return (_DIR / f"{name}.md").read_text()


def tool_contract() -> str:
    return load("tool_contract")


def companion_contract() -> str:
    return load("companion_contract")
