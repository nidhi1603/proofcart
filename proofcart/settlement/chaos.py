"""Fault injection for the settlement layer (tests + labelled ablations only).

``ChaosRail`` wraps any ``Rail`` and injects faults per a ``FaultConfig`` so the
crash-safe pipeline can be exercised against a stated fault set. It is NEVER used
in the real submission path — the demo runs the real Stripe test rail.

The load-bearing fault is ``drop_response_after_create``: the underlying create
actually runs and COMMITS the charge, but ChaosRail hides the response (returns
retryable with no payment_intent_id) so the caller never learns the pid. That is
precisely the interrupted-response condition — it must drive reconcile-before-
retry and prove NO double charge, because the reconciling re-create reuses the
same idempotency key and the underlying rail returns the same, single charge.

Fault ordering inside ``create_payment`` and whether the charge commits:
  * ``http_503_first_n`` : first N calls raise a transient 5xx BEFORE the
    underlying create runs -> NO charge commits (pure transient failure).
  * ``decline``          : returns a declined PaymentIntent BEFORE the create
    runs -> NO charge commits (card declined, no money moves).
  * underlying create runs here -> the charge COMMITS (idempotent by key).
  * ``drop_response_after_create`` (one-shot) : charge committed, response
    hidden (retryable, no pid) so the caller can't learn the id.
  * ``timeout`` (one-shot) : charge committed, then raise TimeoutError
    (interrupted response surfaced as an exception).
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from proofcart.schemas import Rail, RailResponse


class FaultConfig(BaseModel):
    """Which faults to inject. All off by default.

    Shape: ``{drop_response_after_create: bool, timeout: bool,
    http_503_first_n: int, decline: bool}``.
    """

    drop_response_after_create: bool = False
    timeout: bool = False
    http_503_first_n: int = 0
    decline: bool = False


class ChaosRail:
    """Wrap a ``Rail`` and inject faults per ``FaultConfig``.

    Structurally satisfies the ``Rail`` Protocol (``create_payment`` +
    ``retrieve_payment``). Exposes ``create_calls`` / ``retrieve_calls`` counters
    so tests can assert behavior (e.g. a declined card is NOT retried) — note
    those counters describe call volume, never charge count; the underlying rail
    is the arbiter of how many charges actually occurred.
    """

    def __init__(self, rail: Rail, faults: Optional[FaultConfig] = None) -> None:
        self.rail = rail
        self.faults = faults or FaultConfig()
        self.create_calls = 0
        self.retrieve_calls = 0
        self._dropped = False
        self._timed_out = False
        self._http_503_remaining = self.faults.http_503_first_n

    def create_payment(
        self,
        idempotency_key: str,
        amount_cents: int,
        currency: str,
        metadata: dict[str, Any],
    ) -> RailResponse:
        self.create_calls += 1
        f = self.faults

        # Transient 5xx before the charge — no money moves. Raising exercises the
        # pipeline's exception -> UNKNOWN -> reconcile-by-recreate path.
        if self._http_503_remaining > 0:
            self._http_503_remaining -= 1
            raise ConnectionError("chaos: HTTP 503 (transient, no charge committed)")

        # Declined card before the charge — no money moves.
        if f.decline:
            return RailResponse(
                ok=False,
                payment_intent_id=f"pi_declined_{self.create_calls}",
                status="declined",
                amount_cents=amount_cents,
                currency=currency,  # type: ignore[arg-type]
                raw={"fault": "decline", "decline_code": "card_declined"},
            )

        # The real create runs here: the charge COMMITS (idempotent by key).
        resp = self.rail.create_payment(idempotency_key, amount_cents, currency, metadata)

        # Response dropped after commit: caller never learns the pid (one-shot).
        if f.drop_response_after_create and not self._dropped:
            self._dropped = True
            return RailResponse(
                ok=False,
                payment_intent_id=None,
                status="retryable",
                raw={"fault": "dropped_response_after_create", "committed": True},
            )

        # Timeout after commit: interrupted response as an exception (one-shot).
        if f.timeout and not self._timed_out:
            self._timed_out = True
            raise TimeoutError("chaos: response timed out after commit")

        return resp

    def retrieve_payment(self, payment_intent_id: str) -> RailResponse:
        # Retrieve is the trusted reconciliation channel; pass it through so the
        # pipeline can always learn the true state of a committed charge.
        self.retrieve_calls += 1
        return self.rail.retrieve_payment(payment_intent_id)
