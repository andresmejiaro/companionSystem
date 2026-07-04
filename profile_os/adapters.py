"""Model execution interface. Slice zero ships only a deterministic fake.

Real adapters (Claude, GPT, Gemini, local) are a later slice; they implement
the same run() signature. The backend never depends on a specific provider.
"""

from __future__ import annotations


class FakeModelAdapter:
    """Deterministic canned outputs for tests. No network, no keys."""

    name = "fake"

    def run(self, system_prompt: str, user_message: str) -> str:
        return (f"[fake-model] system_chars={len(system_prompt)} "
                f"echo: {user_message[:200]}")
