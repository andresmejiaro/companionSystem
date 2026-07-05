"""Profile prompt files, kept as inspectable markdown next to this module.

Layers (assembled in `profile_os.openai_assistant.build_system_prompt`):
  tool_contract.md      shared tool-use contract, same for every profile
  <profile>_base.md     identity prompt, seeded into the backend store
  <profile>_role.md     role/lane prompt, seeded into the backend store

The base/role files are only *seeds*: at runtime the backend store is the
source of truth and serves them back through the boot payload. The tool
contract is not per-profile and is read directly from this package.
"""

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load(name: str) -> str:
    """Return the text of a prompt file, e.g. load('sidra_base')."""
    return (_DIR / f"{name}.md").read_text()


def tool_contract() -> str:
    return load("tool_contract")
