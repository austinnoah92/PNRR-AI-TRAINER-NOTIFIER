from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    projects_csv: Path
    history_db: Path
    log_file: Path
    sender_email: str | None
    email_password: str | None
    receiver_email: str | None
    gemini_api_key: str | None
    ai_verification_required: bool
    database_url: str | None = None
    cc_email: str | None = None
    bcc_email: str | None = None
    request_timeout: int = 25
    max_pages_per_school: int = 12

    @staticmethod
    def _parse_addresses(raw: str | None) -> list[str]:
        """Split an address list on comma or semicolon, e.g. "a@x.it; b@y.it"."""
        return [addr.strip() for addr in (raw or "").replace(";", ",").split(",") if addr.strip()]

    @property
    def receiver_emails(self) -> list[str]:
        return self._parse_addresses(self.receiver_email)

    @property
    def cc_emails(self) -> list[str]:
        return self._parse_addresses(self.cc_email)

    @property
    def bcc_emails(self) -> list[str]:
        return self._parse_addresses(self.bcc_email)

    @property
    def email_enabled(self) -> bool:
        has_recipient = bool(self.receiver_emails or self.cc_emails or self.bcc_emails)
        return bool(self.sender_email and self.email_password and has_recipient)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.gemini_api_key)


def load_settings() -> Settings:
    load_dotenv(ROOT_DIR / ".env")
    return Settings(
        projects_csv=ROOT_DIR / "data" / "projects_sample.csv",
        history_db=ROOT_DIR / "monitor_history.sqlite3",
        log_file=ROOT_DIR / "run_log.txt",
        sender_email=os.getenv("SENDER_EMAIL"),
        email_password=os.getenv("EMAIL_PASSWORD"),
        receiver_email=os.getenv("RECEIVER_EMAIL"),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        ai_verification_required=os.getenv("AI_VERIFICATION_REQUIRED", "true").lower() == "true",
        database_url=os.getenv("DATABASE_URL") or None,
        cc_email=os.getenv("CC_EMAIL"),
        bcc_email=os.getenv("BCC_EMAIL"),
    )
