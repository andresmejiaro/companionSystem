"""Inspectable prompt seeds for companion profiles.

``<profile>_base.md`` and ``<profile>_role.md`` seed the backend store;
the backend's boot payload is the runtime source of truth. The shared tool
contract is retained for optional provider-validation experiments, but is not
part of the product runtime.
"""

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load(name: str) -> str:
    """Return the text of a prompt file, e.g. load('sidra_base')."""
    return (_DIR / f"{name}.md").read_text()


def tool_contract() -> str:
    return load("tool_contract")
