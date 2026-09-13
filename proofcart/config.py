"""ProofCart configuration: env loading + dev/live mode + status banner.

Dev mode runs with EVERY value blank -- no keys, no network, no optional
packages. Live mode uses the three real APIs (Slack / Notion / Stripe test)
and is what the submission demo runs.

Nothing here raises on a missing key: `status_banner()` describes, per
integration, whether it will run live or fall back to a clearly-labelled
dev mock, so a misconfiguration is visible rather than a crash mid-run.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

# Project root (…/, the dir that holds README.md, data/, runs/).
_ROOT = Path(__file__).resolve().parents[1]

# Load .env if present. Never crash if the file or the package is absent.
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:  # dotenv not installed, or unreadable file
    pass


def _pkg_installed(name: str) -> bool:
    """True if an importable module exists, without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _mask(value: str | None, keep: int = 8) -> str:
    if not value:
        return "<missing>"
    if len(value) <= keep:
        return value[:keep] + "…"
    return value[:keep] + "…"


class Settings:
    """Reads process env on each access, so tests can monkeypatch os.environ
    and callers see the change without re-instantiating."""

    # ---- mode / model ----------------------------------------------------- #
    @property
    def mode(self) -> str:
        m = (os.environ.get("PROOFCART_MODE") or "dev").strip().lower()
        return "live" if m == "live" else "dev"

    @property
    def model(self) -> str:
        return os.environ.get("PROOFCART_MODEL") or "claude-sonnet-5"

    def is_live(self) -> bool:
        return self.mode == "live"

    def is_dev(self) -> bool:
        return self.mode == "dev"

    # ---- raw key accessors (None when blank/absent) ----------------------- #
    @staticmethod
    def _get(key: str) -> str | None:
        v = os.environ.get(key)
        return v if v else None

    @property
    def anthropic_api_key(self) -> str | None:
        return self._get("ANTHROPIC_API_KEY")

    @property
    def stripe_secret_key(self) -> str | None:
        return self._get("STRIPE_SECRET_KEY")

    @property
    def slack_bot_token(self) -> str | None:
        return self._get("SLACK_BOT_TOKEN")

    @property
    def slack_channel_id(self) -> str | None:
        return self._get("SLACK_CHANNEL_ID")

    @property
    def owner_id(self) -> str | None:
        return self._get("PROOFCART_OWNER_ID")

    @property
    def notion_token(self) -> str | None:
        return self._get("NOTION_TOKEN")

    @property
    def notion_db_id(self) -> str | None:
        return self._get("NOTION_DB_ID")

    # ---- per-integration "can it go live?" -------------------------------- #
    def stripe_live(self) -> bool:
        return self.is_live() and bool(self.stripe_secret_key)

    def slack_live(self) -> bool:
        return self.is_live() and bool(self.slack_bot_token)

    def notion_live(self) -> bool:
        return self.is_live() and bool(self.notion_token) and bool(self.notion_db_id)

    # ---- human-readable banner (never raises) ----------------------------- #
    def status_banner(self) -> list[str]:
        lines: list[str] = []
        lines.append(
            "Mode: LIVE (real external APIs)"
            if self.is_live()
            else "Mode: DEV (clearly-labelled local mocks; not counted as external integrations)"
        )
        lines.append(f"Model: {self.model}")

        # Anthropic (used by the extractor/agent stream, not by integrations).
        if self.anthropic_api_key:
            lines.append(f"Anthropic: key set ({_mask(self.anthropic_api_key)})")
        else:
            lines.append("Anthropic: key MISSING (extractor/agent needs it in live)")

        # Stripe
        if self.stripe_live():
            key = self.stripe_secret_key or ""
            warn = "" if key.startswith("sk_test_") else "  [WARN: not an sk_test_ key!]"
            pkg = "" if _pkg_installed("stripe") else "  [WARN: `stripe` package not installed]"
            lines.append(f"Stripe: LIVE test-mode API ({_mask(key)}){warn}{pkg}")
        else:
            why = "mode=dev" if self.is_dev() else "STRIPE_SECRET_KEY missing"
            lines.append(f"Stripe: DEV twin (in-memory, idempotent, no network) [{why}]")

        # Slack
        if self.slack_live():
            pkg = "" if _pkg_installed("slack_sdk") else "  [WARN: `slack_sdk` not installed]"
            lines.append(f"Slack: LIVE WebClient ({_mask(self.slack_bot_token)}){pkg}")
        else:
            why = "mode=dev" if self.is_dev() else "SLACK_BOT_TOKEN missing"
            lines.append(f"Slack: DEV seed threads + in-memory posts [{why}]")

        # Notion
        if self.notion_live():
            pkg = "" if _pkg_installed("notion_client") else "  [WARN: `notion-client` not installed]"
            lines.append(
                f"Notion: LIVE db={_mask(self.notion_db_id, 6)} ({_mask(self.notion_token)}){pkg}"
            )
        else:
            missing = []
            if self.is_live():
                if not self.notion_token:
                    missing.append("NOTION_TOKEN")
                if not self.notion_db_id:
                    missing.append("NOTION_DB_ID")
            why = "mode=dev" if self.is_dev() else ", ".join(missing) + " missing"
            lines.append(f"Notion: DEV json store (runs/notion_dev.json) [{why}]")

        # Owner identity (approval gate)
        if self.owner_id:
            lines.append(f"Owner (approver) id: {self.owner_id}")
        else:
            lines.append("Owner (approver) id: MISSING (no identity can approve payment)")

        return lines


# Module-level singleton + accessor.
settings = Settings()


def get_settings() -> Settings:
    return settings


# Path helpers other streams may reuse.
def project_root() -> Path:
    return _ROOT


if __name__ == "__main__":
    print("ProofCart config smoke:")
    for line in get_settings().status_banner():
        print("  " + line)
    print("OK: config loaded, status_banner rendered without crashing.")
