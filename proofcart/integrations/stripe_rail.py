"""Stripe payment rail (test mode) + a dev twin, both behind schemas.Rail.

CRITICAL invariant (README §7): we NEVER decide "does a payment exist?" via
PaymentIntent.search -- search is not immediately consistent, so an empty
result could justify a wrong second charge. We only ever RETRIEVE BY ID.

LIVE mode  : real Stripe TEST API. `stripe` is imported lazily so dev runs
             without the package installed.
DEV mode   : an in-memory, idempotent twin. The SAME idempotency_key always
             returns the SAME synthetic pi id + 'succeeded'; no network.

Error mapping (create):
  card decline                         -> ok=False, status='declined'
  connection / rate-limit / 5xx (API)  -> ok=False, status='retryable'
  invalid-request / auth / idempotency -> ok=False, status='error'
  anything else / lost response        -> ok=False, status='unknown'  (reconcile)
"""
from __future__ import annotations

# Allow both `python3 -m proofcart.integrations.stripe_rail` and the plain-script
# form `python3 proofcart/integrations/stripe_rail.py`.
if __name__ == "__main__" and __package__ in (None, ""):  # pragma: no cover
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    __package__ = "proofcart.integrations"

import hashlib
from typing import Any, Optional

from ..config import Settings, get_settings
from ..schemas import RailResponse

_DASHBOARD = "https://dashboard.stripe.com/test/payments/{pid}"


def _norm_currency(currency: str | None) -> Optional[str]:
    """RailResponse.currency is Literal['USD']; only set it when it matches."""
    if currency and currency.upper() == "USD":
        return "USD"
    return None


class StripeRail:
    """Implements schemas.Rail. Pass ``client`` to inject a fake/real stripe
    module for tests (forces the live code path against the injected object)."""

    def __init__(self, settings: Optional[Settings] = None, client: Any = None) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self.live = client is not None or self._settings.stripe_live()
        # Dev twin state (in-memory only; process-lifetime idempotency).
        self._twin_by_key: dict[str, RailResponse] = {}
        self._twin_by_pid: dict[str, RailResponse] = {}

    # ------------------------------------------------------------------ #
    # Public API (schemas.Rail)
    # ------------------------------------------------------------------ #
    def create_payment(
        self,
        idempotency_key: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, Any],
    ) -> RailResponse:
        if self.live:
            return self._create_live(idempotency_key, amount_cents, currency, metadata)
        return self._create_dev(idempotency_key, amount_cents, currency, metadata)

    def retrieve_payment(self, payment_intent_id: str) -> RailResponse:
        if self.live:
            return self._retrieve_live(payment_intent_id)
        return self._retrieve_dev(payment_intent_id)

    # ------------------------------------------------------------------ #
    # DEV twin (idempotent, no network)
    # ------------------------------------------------------------------ #
    def _create_dev(
        self, idempotency_key: str, amount_cents: int, currency: str, metadata: dict[str, Any]
    ) -> RailResponse:
        existing = self._twin_by_key.get(idempotency_key)
        if existing is not None:
            # Same key -> same result, exactly like Stripe's idempotency.
            return existing
        pid = "pi_dev_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        resp = RailResponse(
            ok=True,
            payment_intent_id=pid,
            status="succeeded",
            amount_cents=amount_cents,
            currency=_norm_currency(currency),
            raw={
                "dev_twin": True,
                "idempotency_key": idempotency_key,
                "currency": (currency or "").lower(),
                "metadata": dict(metadata or {}),
                "dashboard_url": _DASHBOARD.format(pid=pid),
            },
        )
        self._twin_by_key[idempotency_key] = resp
        self._twin_by_pid[pid] = resp
        return resp

    def _retrieve_dev(self, payment_intent_id: str) -> RailResponse:
        found = self._twin_by_pid.get(payment_intent_id)
        if found is not None:
            return found
        # Never fabricate success for an id we didn't create.
        return RailResponse(
            ok=False,
            payment_intent_id=payment_intent_id,
            status="unknown",
            raw={"dev_twin": True, "reason": "no such payment_intent in dev twin"},
        )

    # ------------------------------------------------------------------ #
    # LIVE (real Stripe test API)
    # ------------------------------------------------------------------ #
    def _stripe(self) -> Any:
        """Lazy import + api key. Called OUTSIDE the try/except so a config
        error surfaces instead of being swallowed as 'unknown'."""
        if self._client is not None:
            return self._client
        try:
            import stripe  # type: ignore
        except ImportError as e:  # pragma: no cover - only in live w/o pkg
            raise RuntimeError(
                "live mode requires the `stripe` package (pip install stripe)"
            ) from e
        key = self._settings.stripe_secret_key
        if key:
            stripe.api_key = key
        self._client = stripe
        return stripe

    def _create_live(
        self, idempotency_key: str, amount_cents: int, currency: str, metadata: dict[str, Any]
    ) -> RailResponse:
        stripe = self._stripe()
        try:
            pi = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=currency.lower(),
                confirm=True,
                payment_method="pm_card_visa",
                automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                metadata=metadata,
                idempotency_key=idempotency_key,
            )
        except Exception as e:  # noqa: BLE001 - classify below
            return self._classify_create(e, stripe)
        return self._map_pi(pi)

    def _retrieve_live(self, payment_intent_id: str) -> RailResponse:
        stripe = self._stripe()
        try:
            pi = stripe.PaymentIntent.retrieve(payment_intent_id)
        except Exception as e:  # noqa: BLE001 - classify below
            return self._classify_retrieve(e, stripe, payment_intent_id)
        return self._map_pi(pi)

    # ------------------------------------------------------------------ #
    # Mapping + error classification
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pi_get(pi: Any, key: str) -> Any:
        if isinstance(pi, dict):
            return pi.get(key)
        try:
            return pi[key]  # stripe objects support mapping access
        except Exception:
            return getattr(pi, key, None)

    def _map_pi(self, pi: Any) -> RailResponse:
        pid = self._pi_get(pi, "id")
        status = self._pi_get(pi, "status")
        amount = self._pi_get(pi, "amount")
        currency = self._pi_get(pi, "currency")
        ok = status == "succeeded"
        # Preserve Stripe's own status verbatim (succeeded / requires_action /
        # processing / requires_payment_method / canceled / ...).
        return RailResponse(
            ok=ok,
            payment_intent_id=pid,
            status=status,
            amount_cents=amount if isinstance(amount, int) else None,
            currency=_norm_currency(currency),
            raw={
                "stripe_status": status,
                "dashboard_url": _DASHBOARD.format(pid=pid) if pid else None,
            },
        )

    @staticmethod
    def _err_classes(stripe: Any) -> dict[str, type]:
        err_mod = getattr(stripe, "error", stripe)

        class _Never(Exception):
            pass

        def cls(name: str) -> type:
            c = getattr(err_mod, name, None) or getattr(stripe, name, None)
            return c if isinstance(c, type) and issubclass(c, BaseException) else _Never

        return {
            "card": cls("CardError"),
            "rate": cls("RateLimitError"),
            "conn": cls("APIConnectionError"),
            "api": cls("APIError"),
            "invalid": cls("InvalidRequestError"),
            "auth": cls("AuthenticationError"),
            "perm": cls("PermissionError"),
            "idem": cls("IdempotencyError"),
        }

    def _classify_create(self, e: Exception, stripe: Any) -> RailResponse:
        ec = self._err_classes(stripe)
        raw = {"error": str(e), "error_type": type(e).__name__}
        if isinstance(e, ec["card"]):
            return RailResponse(ok=False, status="declined", raw=raw)
        if isinstance(e, (ec["rate"], ec["conn"])):
            return RailResponse(ok=False, status="retryable", raw=raw)
        if isinstance(e, (ec["invalid"], ec["auth"], ec["perm"], ec["idem"])):
            return RailResponse(ok=False, status="error", raw=raw)
        if isinstance(e, ec["api"]):
            # Generic API error (incl. 5xx) -> safe to retry same idem key.
            return RailResponse(ok=False, status="retryable", raw=raw)
        # Unknown/lost response: outcome uncertain -> reconcile, never blind-recreate.
        return RailResponse(ok=False, status="unknown", raw=raw)

    def _classify_retrieve(self, e: Exception, stripe: Any, pid: str) -> RailResponse:
        ec = self._err_classes(stripe)
        raw = {"error": str(e), "error_type": type(e).__name__}
        if isinstance(e, ec["invalid"]):
            # Stripe authoritatively has no such PI (retrieve by id, not search).
            return RailResponse(ok=False, payment_intent_id=pid, status="not_found", raw=raw)
        if isinstance(e, (ec["rate"], ec["conn"], ec["api"])):
            return RailResponse(ok=False, payment_intent_id=pid, status="retryable", raw=raw)
        return RailResponse(ok=False, payment_intent_id=pid, status="unknown", raw=raw)


if __name__ == "__main__":
    rail = StripeRail()
    assert not rail.live, "smoke expects dev mode (no keys)"
    meta = {"order_id": "ord_demo", "request_id": "req_demo"}
    r1 = rail.create_payment("idem-abc", 99900, "USD", meta)
    r2 = rail.create_payment("idem-abc", 99900, "USD", meta)  # same key
    r3 = rail.create_payment("idem-xyz", 12300, "USD", meta)  # different key
    assert r1.ok and r1.status == "succeeded"
    assert r1.payment_intent_id == r2.payment_intent_id, "same idem key -> same pi"
    assert r1.payment_intent_id != r3.payment_intent_id, "diff idem key -> diff pi"
    got = rail.retrieve_payment(r1.payment_intent_id)
    assert got.ok and got.payment_intent_id == r1.payment_intent_id
    miss = rail.retrieve_payment("pi_dev_nope")
    assert not miss.ok and miss.status == "unknown"
    print("ProofCart stripe_rail smoke:")
    print(f"  created  {r1.payment_intent_id} status={r1.status} amount={r1.amount_cents}")
    print(f"  idempotent replay -> same id: {r1.payment_intent_id == r2.payment_intent_id}")
    print(f"  dashboard_url: {r1.raw.get('dashboard_url')}")
    print("OK: dev twin is idempotent; retrieve-by-id only; no network.")
