"""Crash-safe settlement pipeline (README section 7).

The whole mechanism in one sentence: persist the intent, persist the
PaymentIntent id the instant it exists, and determine the outcome by inspecting
the actual PaymentIntent object — never by counting create calls or searching.

Flow (``settle``):
  1. Reconcile-before-anything. If the order already SUCCEEDED, return it
     (idempotent). If it is PENDING with a persisted payment_intent_id, retrieve
     that exact object; if it verifies, mark SUCCEEDED and return.
  2. Write-ahead a PENDING row.
  3. Attempt ``create_payment`` with the mandate's stable idempotency key. The
     instant a payment_intent_id comes back, persist it (before trusting the
     rest of the response).
  4. ``ok and status == 'succeeded'`` -> run ``verify()``. Pass -> SUCCEEDED.
     A 'succeeded' that FAILS verify is a SILENT FAILURE -> FAILED
     ('silent_failure'). A 'declined' -> FAILED ('declined'), and is NOT retried.
  5. 'retryable' / exception / interrupted / any other uncertain response ->
     outcome UNKNOWN. Do NOT create a fresh charge. Reconcile: if a
     payment_intent_id was persisted, retrieve it; otherwise RE-CALL
     ``create_payment`` with the SAME idempotency key (idempotent — returns the
     same object, never a second charge). Exponential backoff with jitter, up to
     ``retries``. If still unknown, leave the ledger UNKNOWN (never SUCCEEDED,
     never a second create).
"""
from __future__ import annotations

import random
import time
from typing import Optional

from proofcart.schemas import (
    PaymentMandate,
    Rail,
    RailResponse,
    SettleStatus,
)
from proofcart.settlement.ledger import Ledger


# --------------------------------------------------------------------------- #
# Verification                                                                #
# --------------------------------------------------------------------------- #
def verify(resp: RailResponse, pm: PaymentMandate) -> list[str]:
    """Return the list of problems with a PaymentIntent object (empty == ok).

    A payment is trustworthy only if the *object* says: amount == the authorized
    amount AND currency matches AND a payment_intent_id is present. A 'succeeded'
    response that fails any of these is a silent failure.
    """
    problems: list[str] = []
    if resp is None:
        return ["no_response"]
    if not resp.payment_intent_id:
        problems.append("missing_payment_intent_id")
    if resp.amount_cents != pm.amount_cents:
        problems.append(
            f"amount_mismatch(expected={pm.amount_cents},got={resp.amount_cents})"
        )
    if resp.currency != pm.currency:
        problems.append(
            f"currency_mismatch(expected={pm.currency},got={resp.currency})"
        )
    return problems


# --------------------------------------------------------------------------- #
# Response classification                                                     #
# --------------------------------------------------------------------------- #
def _classify(resp: Optional[RailResponse]) -> str:
    """Map a rail response to one of: 'succeeded' | 'declined' | 'uncertain'.

    'uncertain' covers retryable, unknown, requires_action, not-ok, and the
    no-response (exception/interrupted) case — anything whose outcome we cannot
    trust yet and must reconcile rather than recreate.
    """
    if resp is None:
        return "uncertain"
    if resp.ok and resp.status == "succeeded":
        return "succeeded"
    if resp.status == "declined":
        return "declined"
    return "uncertain"


def _backoff(n: int) -> float:
    """Exponential backoff with jitter: 0.2 * 2**n + jitter."""
    return 0.2 * (2 ** n) + random.uniform(0.0, 0.1)


# --------------------------------------------------------------------------- #
# The pipeline                                                                #
# --------------------------------------------------------------------------- #
def settle(
    pm: PaymentMandate,
    rail: Rail,
    ledger: Ledger,
    retries: int = 3,
) -> LedgerEntry:  # type: ignore[name-defined]  # noqa: F821  (see import below)
    # --- 1. reconcile-before-anything --------------------------------- #
    existing = ledger.get(pm.order_id)
    if existing is not None:
        if existing.status == SettleStatus.SUCCEEDED:
            return existing  # idempotent: already settled
        if existing.status == SettleStatus.PENDING and existing.payment_intent_id:
            resp = _safe_retrieve(rail, existing.payment_intent_id)
            if _classify(resp) == "succeeded" and not verify(resp, pm):
                return ledger.mark(pm.order_id, SettleStatus.SUCCEEDED)

    # --- 2. write-ahead (PENDING) ------------------------------------- #
    ledger.write_ahead(pm)

    # --- 3. attempt the create ---------------------------------------- #
    metadata = {
        "order_id": pm.order_id,
        "request_id": pm.request_id,
        "quote_id": pm.quote_id,
    }
    resp = None
    try:
        resp = rail.create_payment(
            pm.idempotency_key, pm.amount_cents, pm.currency, metadata
        )
    except Exception:
        resp = None  # interrupted -> outcome UNKNOWN, reconcile below

    # Persist the pid the instant it exists — before trusting the rest.
    if resp is not None and resp.payment_intent_id:
        ledger.set_payment_intent_id(pm.order_id, resp.payment_intent_id)

    # --- 4. trusted outcomes ------------------------------------------ #
    outcome = _classify(resp)
    if outcome == "succeeded":
        problems = verify(resp, pm)
        if not problems:
            return ledger.mark(pm.order_id, SettleStatus.SUCCEEDED)
        return ledger.mark(
            pm.order_id, SettleStatus.FAILED, reason="silent_failure"
        )
    if outcome == "declined":
        # A declined card is a definitive negative outcome: do NOT retry.
        return ledger.mark(pm.order_id, SettleStatus.FAILED, reason="declined")

    # --- 5. UNKNOWN -> reconcile, never recreate a fresh charge -------- #
    ledger.mark(pm.order_id, SettleStatus.UNKNOWN, reason="create_outcome_unknown")
    return _reconcile_loop(pm, rail, ledger, retries, metadata)


def _reconcile_loop(
    pm: PaymentMandate,
    rail: Rail,
    ledger: Ledger,
    retries: int,
    metadata: dict,
) -> LedgerEntry:  # type: ignore[name-defined]  # noqa: F821
    """Resolve an UNKNOWN order without ever issuing a second distinct charge."""
    for n in range(retries):
        time.sleep(_backoff(n))
        entry = ledger.get(pm.order_id)
        resp: Optional[RailResponse] = None
        try:
            if entry is not None and entry.payment_intent_id:
                # We know the id -> retrieve the exact object (never search).
                resp = rail.retrieve_payment(entry.payment_intent_id)
            else:
                # No id learned yet -> re-call create with the SAME key. This is
                # idempotent: the rail returns the same object if the charge
                # already committed, and creates it exactly once otherwise.
                resp = rail.create_payment(
                    pm.idempotency_key, pm.amount_cents, pm.currency, metadata
                )
                if resp is not None and resp.payment_intent_id:
                    ledger.set_payment_intent_id(pm.order_id, resp.payment_intent_id)
        except Exception:
            resp = None  # still uncertain -> keep reconciling

        outcome = _classify(resp)
        if outcome == "succeeded":
            problems = verify(resp, pm)
            if not problems:
                return ledger.mark(pm.order_id, SettleStatus.SUCCEEDED)
            return ledger.mark(
                pm.order_id, SettleStatus.FAILED, reason="silent_failure"
            )
        if outcome == "declined":
            return ledger.mark(pm.order_id, SettleStatus.FAILED, reason="declined")
        # else: still uncertain -> loop again

    # Exhausted retries and still uncertain: leave it UNKNOWN. Never SUCCEEDED,
    # never a second create. A restart / recovery worker will reconcile again.
    return ledger.get(pm.order_id)  # type: ignore[return-value]


def reconcile(order_id: str, rail: Rail, ledger: Ledger) -> LedgerEntry:  # type: ignore[name-defined]  # noqa: F821
    """Reconcile a single order on restart / recovery — no mandate needed.

    Uses the durable ledger row: if a payment_intent_id was persisted, retrieve
    that exact object; otherwise re-call create with the stored idempotency key
    (idempotent — never a second charge). Resolves to SUCCEEDED / FAILED when the
    object is conclusive, and otherwise leaves the row untouched.
    """
    entry = ledger.get(order_id)
    if entry is None:
        raise KeyError(f"no ledger row for order_id {order_id!r}")
    if entry.status == SettleStatus.SUCCEEDED:
        return entry

    resp: Optional[RailResponse] = None
    try:
        if entry.payment_intent_id:
            resp = rail.retrieve_payment(entry.payment_intent_id)
        else:
            resp = rail.create_payment(
                entry.idempotency_key,
                entry.amount_cents,
                entry.currency,
                {"order_id": order_id},
            )
            if resp is not None and resp.payment_intent_id:
                ledger.set_payment_intent_id(order_id, resp.payment_intent_id)
    except Exception:
        return ledger.get(order_id)  # type: ignore[return-value]

    outcome = _classify(resp)
    if outcome == "succeeded":
        problems = _verify_fields(resp, entry.amount_cents, entry.currency)
        if not problems:
            return ledger.mark(order_id, SettleStatus.SUCCEEDED)
        return ledger.mark(order_id, SettleStatus.FAILED, reason="silent_failure")
    if outcome == "declined":
        return ledger.mark(order_id, SettleStatus.FAILED, reason="declined")
    return ledger.get(order_id)  # type: ignore[return-value]


def _safe_retrieve(rail: Rail, pid: str) -> Optional[RailResponse]:
    try:
        return rail.retrieve_payment(pid)
    except Exception:
        return None


def _verify_fields(
    resp: RailResponse, amount_cents: int, currency: str
) -> list[str]:
    problems: list[str] = []
    if resp is None:
        return ["no_response"]
    if not resp.payment_intent_id:
        problems.append("missing_payment_intent_id")
    if resp.amount_cents != amount_cents:
        problems.append("amount_mismatch")
    if resp.currency != currency:
        problems.append("currency_mismatch")
    return problems


# LedgerEntry is only needed for annotations; import here to avoid confusing the
# module-level Rail/RailResponse imports above.
from proofcart.schemas import LedgerEntry  # noqa: E402


# --------------------------------------------------------------------------- #
# Smoke test — runnable proof of the mechanism.                               #
#   python3 -m proofcart.settlement.pipeline                                  #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from proofcart.schemas import idempotency_key as make_key
    from proofcart.settlement.chaos import ChaosRail, FaultConfig

    class FakeStripeRail:
        """Tiny in-memory idempotent rail honoring the Rail Protocol.

        ``charges`` records one entry per *distinct* committed charge, so the
        test can assert exactly-once directly from the rail's own record — the
        way the Stripe test dashboard is the arbiter of how many charges
        occurred (never the create-call count).
        """

        def __init__(self) -> None:
            self._by_key: dict[str, dict] = {}
            self.charges: list[str] = []
            self._seq = 0

        def create_payment(self, idempotency_key, amount_cents, currency, metadata):
            if idempotency_key in self._by_key:
                pi = self._by_key[idempotency_key]  # idempotent: no new charge
            else:
                self._seq += 1
                pid = f"pi_test_{self._seq}"
                pi = {
                    "id": pid,
                    "amount": amount_cents,
                    "currency": currency,
                    "status": "succeeded",
                }
                self._by_key[idempotency_key] = pi
                self.charges.append(pid)  # exactly one charge recorded
            return RailResponse(
                ok=True,
                payment_intent_id=pi["id"],
                status="succeeded",
                amount_cents=pi["amount"],
                currency=pi["currency"],
                raw=pi,
            )

        def retrieve_payment(self, payment_intent_id):
            for pi in self._by_key.values():
                if pi["id"] == payment_intent_id:
                    return RailResponse(
                        ok=True,
                        payment_intent_id=pi["id"],
                        status=pi["status"],
                        amount_cents=pi["amount"],
                        currency=pi["currency"],
                        raw=pi,
                    )
            return RailResponse(
                ok=False, payment_intent_id=payment_intent_id, status="unknown"
            )

    tmp = Path(tempfile.mkdtemp(prefix="proofcart_smoke_"))

    def mandate(order_id: str, amount: int = 100_000) -> PaymentMandate:
        th = f"terms-hash-{order_id}"
        return PaymentMandate(
            order_id=order_id,
            request_id="req-demo",
            quote_id="quote-1",
            quote_version=1,
            payment_recipient="acct_supplier_x",
            amount_cents=amount,
            currency="USD",
            idempotency_key=make_key(order_id, th),
            terms_hash=th,
            authorized_by="owner-1",
        )

    # (a) clean settle -> one SUCCEEDED entry, one charge -------------- #
    rail_a = FakeStripeRail()
    ledger_a = Ledger(path=tmp / "clean.jsonl")
    e = settle(mandate("order-clean"), rail_a, ledger_a)
    assert e.status == SettleStatus.SUCCEEDED, e
    assert e.payment_intent_id, e
    assert len(rail_a.charges) == 1, rail_a.charges
    # re-settle is idempotent: returns the same entry, no second charge
    e_again = settle(mandate("order-clean"), rail_a, ledger_a)
    assert e_again.status == SettleStatus.SUCCEEDED
    assert len(rail_a.charges) == 1, rail_a.charges
    print(f"(a) clean          -> {e.status.value} pid={e.payment_intent_id} "
          f"charges={len(rail_a.charges)} (re-settle idempotent)")

    # (b) drop-response-after-create -> reconcile, still ONE charge ---- #
    base_b = FakeStripeRail()
    chaos_b = ChaosRail(base_b, FaultConfig(drop_response_after_create=True))
    ledger_b = Ledger(path=tmp / "drop.jsonl")
    eb = settle(mandate("order-drop"), chaos_b, ledger_b)
    assert eb.status == SettleStatus.SUCCEEDED, eb
    assert eb.payment_intent_id, eb
    assert len(base_b.charges) == 1, base_b.charges           # exactly ONE charge
    assert chaos_b.create_calls >= 2, chaos_b.create_calls    # dropped then reconciled
    print(f"(b) dropped resp   -> {eb.status.value} pid={eb.payment_intent_id} "
          f"charges={len(base_b.charges)} create_calls={chaos_b.create_calls} "
          f"(reconcile-before-retry, NO double charge)")

    # (c) declined card -> FAILED, no retry, no charge ----------------- #
    base_c = FakeStripeRail()
    chaos_c = ChaosRail(base_c, FaultConfig(decline=True))
    ledger_c = Ledger(path=tmp / "decline.jsonl")
    ec = settle(mandate("order-decline"), chaos_c, ledger_c)
    assert ec.status == SettleStatus.FAILED, ec
    assert ec.reason == "declined", ec
    assert len(base_c.charges) == 0, base_c.charges           # no charge
    assert chaos_c.create_calls == 1, chaos_c.create_calls    # NOT retried
    print(f"(c) declined       -> {ec.status.value}/{ec.reason} "
          f"charges={len(base_c.charges)} create_calls={chaos_c.create_calls} "
          f"(no retry of a declined card)")

    # bonus: durability — reload the ledger from disk and reconcile ---- #
    reloaded = Ledger(path=tmp / "drop.jsonl")
    r = reconcile("order-drop", base_b, reloaded)
    assert r.status == SettleStatus.SUCCEEDED, r
    assert len(base_b.charges) == 1, base_b.charges
    print(f"(d) reload+reconcile-> {r.status.value} charges={len(base_b.charges)} "
          f"(durable across restart)")

    print("\nALL SETTLEMENT SMOKE ASSERTIONS PASSED")
