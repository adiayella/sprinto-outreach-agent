"""Environment settings and versioned config loading."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
CONFIG_DIR = PKG / "config"
FIXTURES = PKG / "fixtures"
ON_VERCEL = bool(os.getenv("VERCEL"))
DATA_DIR = Path("/tmp/sprinto") if ON_VERCEL else PKG / "data"  # /tmp is the only writable disk on Vercel

load_dotenv(ROOT / ".env")

MODES = ("simulation", "dry_run", "real_demo")


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v.strip()
    return default


@dataclass
class Settings:
    mode: str
    gemini_key: str
    gemini_model: str
    gemini_free_tier: bool
    hunter_key: str
    gmail_address: str
    gmail_app_password: str
    demo_recipient: str
    real_sends_enabled: bool
    sender_name: str
    resend_key: str = ""
    resend_from: str = "onboarding@resend.dev"
    database_url: str = ""
    app_password: str = ""
    serverless: bool = False

    @property
    def mail_provider(self) -> str:
        return "resend" if self.resend_key else "gmail"

    def mail_ready(self) -> bool:
        return bool(self.resend_key) if self.mail_provider == "resend" else bool(self.gmail_address and self.gmail_app_password)

    @property
    def db_path(self) -> Path:
        # One DB per mode so simulated data never mixes with real data.
        return DATA_DIR / f"{self.mode}.sqlite3"

    @property
    def delivery_address(self) -> str:
        if not self.demo_recipient and self.mode == "simulation":
            return "your-inbox@simulation.example"
        return self.demo_recipient.lower()

    def readiness(self) -> dict:
        sim = self.mode == "simulation"
        return {
            "mode": self.mode,
            "gemini": "simulated" if sim else ("ready" if self.gemini_key else "missing GEMINI_API_KEY"),
            "hunter": "simulated" if sim else ("ready" if self.hunter_key else "missing HUNTER_API_KEY (manual contact entry still works)"),
            "gmail": "simulated" if sim else ("ready" if self.mail_ready() else
                                               "missing RESEND_API_KEY (or GMAIL_ADDRESS + GMAIL_APP_PASSWORD)"),
            "mail_provider": self.mail_provider,
            "recipient": self.delivery_address or "missing DEMO_RECIPIENT_EMAIL",
            "real_sends": self.mode == "real_demo" and self.real_sends_enabled,
            "storage": "postgres" if self.database_url else "sqlite",
        }


def load_settings() -> Settings:
    mode = _env("SPRINTO_MODE", default="simulation")
    if mode not in MODES:
        mode = "simulation"
    gmail = _env("GMAIL_ADDRESS", "GMAIL_SENDER_ADDRESS")
    return Settings(
        mode=mode,
        gemini_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL"),
        gemini_free_tier=_env("GEMINI_FREE_TIER", default="true").lower() == "true",
        hunter_key=_env("HUNTER_API_KEY"),
        gmail_address=gmail,
        gmail_app_password=_env("GMAIL_APP_PASSWORD").replace(" ", ""),
        demo_recipient=_env("DEMO_RECIPIENT_EMAIL", default=gmail),
        real_sends_enabled=_env("REAL_SENDS_ENABLED").lower() == "true",
        sender_name=_env("SENDER_NAME", default="Growth team, Sprinto"),
        resend_key=_env("RESEND_API_KEY"),
        resend_from=_env("RESEND_FROM", default="onboarding@resend.dev"),
        database_url=_env("DATABASE_URL", "POSTGRES_URL"),
        app_password=_env("APP_PASSWORD"),
        serverless=ON_VERCEL or _env("SERVERLESS").lower() == "true",
    )


@lru_cache(maxsize=None)
def load_config(name: str) -> dict:
    return json.loads((CONFIG_DIR / f"{name}.json").read_text())


def versions() -> dict:
    return {k: load_config(k)["version"] for k in ("rubric_v1", "capabilities_v1", "policy_v1", "sources_v1")}
