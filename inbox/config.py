"""Settings from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    database_path: str = "inbox.db"
    rules_path: str = "routing.yaml"
    anthropic_api_key: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "low"
    alert_webhook_url: str = ""
    public_url: str = "http://127.0.0.1:8030"
    demo_mode: bool = True
    admin_password: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=os.getenv("DATABASE_PATH", "inbox.db"),
            rules_path=os.getenv("RULES_PATH", "routing.yaml"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            model=os.getenv("INBOX_MODEL", "claude-opus-5-5"),
            effort=os.getenv("INBOX_EFFORT", "low"),
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
            public_url=os.getenv("PUBLIC_URL", "http://127.0.0.1:8030"),
            demo_mode=os.getenv("DEMO_MODE", "true").strip().lower() in ("1", "true", "yes", "on"),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
        )

    def validate(self) -> None:
        if not self.demo_mode and len(self.admin_password) < 12:
            raise RuntimeError("Set ADMIN_PASSWORD (12+ characters) when DEMO_MODE=false.")
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise RuntimeError("INBOX_EFFORT must be low, medium, high, xhigh or max.")
        if self.alert_webhook_url and not self.alert_webhook_url.startswith("https://"):
            raise RuntimeError("ALERT_WEBHOOK_URL must start with https://")
