from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _csv(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    public_base_url: str = "http://127.0.0.1:8091"
    data_dir: Path = Path("data/life-mcp")
    connector_tokens: list[str] = field(default_factory=list)
    oauth_signing_key: str = ""
    admin_secret: str = ""
    auth_required: bool = True
    read_only: bool = False
    oauth_access_token_ttl_seconds: int = 60 * 60 * 24 * 30
    allowed_origins: list[str] = field(default_factory=list)
    allowed_redirect_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        tokens = _csv("LIFE_MCP_CONNECTOR_TOKENS")
        if token := os.getenv("LIFE_MCP_CONNECTOR_TOKEN", "").strip():
            tokens.append(token)
        return cls(
            public_base_url=os.getenv(
                "LIFE_MCP_PUBLIC_BASE_URL", "http://127.0.0.1:8091"
            ).rstrip("/"),
            data_dir=Path(os.getenv("LIFE_MCP_DATA_DIR", "data/life-mcp")),
            connector_tokens=tokens,
            oauth_signing_key=os.getenv("LIFE_MCP_OAUTH_SIGNING_KEY", ""),
            admin_secret=os.getenv("LIFE_MCP_ADMIN_SECRET", ""),
            auth_required=_bool("LIFE_MCP_AUTH_REQUIRED", True),
            read_only=_bool("LIFE_MCP_READ_ONLY", False),
            oauth_access_token_ttl_seconds=int(os.getenv(
                "LIFE_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 30)
            )),
            allowed_origins=_csv(
                "LIFE_MCP_ALLOWED_ORIGINS",
                "https://claude.ai,https://*.claude.ai,https://chatgpt.com,"
                "https://*.chatgpt.com,https://openai.com,https://*.openai.com",
            ),
            allowed_redirect_hosts=_csv(
                "LIFE_MCP_OAUTH_ALLOWED_REDIRECT_HOSTS",
                "claude.ai,*.claude.ai,chatgpt.com,*.chatgpt.com,"
                "openai.com,*.openai.com",
            ),
        )

    @property
    def resource_url(self) -> str:
        return f"{self.public_base_url}/mcp"

    def validate(self) -> list[str]:
        missing: list[str] = []
        if self.auth_required and not (
            self.connector_tokens or self.oauth_signing_key
        ):
            missing.append("LIFE_MCP_CONNECTOR_TOKEN or LIFE_MCP_OAUTH_SIGNING_KEY")
        if self.oauth_signing_key and not self.admin_secret:
            missing.append("LIFE_MCP_ADMIN_SECRET")
        return missing
