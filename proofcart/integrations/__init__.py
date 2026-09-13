"""ProofCart external integrations (Slack, Notion, Stripe).

Each client works in dev mode with NO keys, NO network, and NO optional
packages installed (SDKs are imported lazily inside methods). Live mode uses
the three real APIs; local mocks are a clearly-labelled dev mode only and do
not count as external integrations (README §8).
"""
from __future__ import annotations

from .notion import NotionClient
from .slack import SlackClient
from .stripe_rail import StripeRail

__all__ = ["SlackClient", "NotionClient", "StripeRail"]
