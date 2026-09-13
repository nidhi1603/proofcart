"""NaiveCart -- a fair, component-removal baseline.

NaiveCart shares ProofCart's EXACT extraction and comparison (same
``collect`` -> ``extract_offers`` -> ``build_comparison`` pipeline), so it is a
strong baseline on the "read the threads and shortlist correctly" task. What it
*removes* are ProofCart's three enforced guardrails, each clearly labelled:

  (a) NO owner-approval / referee gate -- it pays the top-ranked eligible offer
      directly, whoever (or nobody) "approved" it.
  (b) NO crash-safe settlement -- on an interrupted / non-succeeded response it
      RE-CREATES the charge with a FRESH idempotency key and never reconciles,
      so an interrupted response (or a duplicate settle) yields a real double
      charge at the rail.
  (c) NO settlement verification -- it reports ``paid: true`` from the fact that
      it got *a* response back, without checking the PaymentIntent actually
      succeeded, so a failed settlement is silently over-claimed.

It is scored by the SAME grader against the SAME expectations as ProofCart. We do
not hardcode "the baseline fails X"; the runner measures the actual charges at the
rail and the actual claim, and the scoreboard reports whatever happened.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from proofcart.collector import collect
from proofcart.comparator import build_comparison
from proofcart.agents.extractor import extract_offers
from proofcart.schemas import (
    LedgerEntry,
    QuoteTerms,
    Rail,
    RailResponse,
    RunRecord,
    SettleStatus,
    State,
    now_iso,
)
from proofcart.settlement.chaos import ChaosRail

from evals.assertions import RunOutcome
from evals.scenario import Scenario


# --------------------------------------------------------------------------- #
# Ground-truth charge counter (wraps the dev rail; counts distinct commits)   #
# --------------------------------------------------------------------------- #
class CountingRail:
    """Wrap a ``Rail`` and record every DISTINCT committed charge.

    A "charge" is a distinct succeeded PaymentIntent id returned by the
    underlying rail's ``create_payment``. Idempotent replays (same idempotency
    key -> same pid) are counted once; a fresh key that commits a new pid is a
    new charge. This is the arbiter of how many charges actually occurred -- it
    sits *under* any ``ChaosRail``, so a charge committed behind a dropped
    response is still counted even when the caller never learns the pid.
    """

    def __init__(self, rail: Rail) -> None:
        self.rail = rail
        self._charged_pids: set[str] = set()
        self.create_calls = 0
        self.retrieve_calls = 0

    def create_payment(
        self, idempotency_key: str, amount_cents: int, currency: str, metadata: dict[str, Any]
    ) -> RailResponse:
        self.create_calls += 1
        resp = self.rail.create_payment(idempotency_key, amount_cents, currency, metadata)
        if resp and resp.ok and resp.status == "succeeded" and resp.payment_intent_id:
            self._charged_pids.add(resp.payment_intent_id)
        return resp

    def retrieve_payment(self, payment_intent_id: str) -> RailResponse:
        self.retrieve_calls += 1
        return self.rail.retrieve_payment(payment_intent_id)

    def distinct_charges(self) -> int:
        return len(self._charged_pids)


def build_rail(scenario: Scenario) -> tuple[CountingRail, Rail]:
    """Return ``(counter, rail)`` where ``rail`` is what the agent settles
    against (a ``ChaosRail`` when faults are set) and ``counter`` is the
    ground-truth charge counter beneath it."""
    from proofcart.integrations.stripe_rail import StripeRail  # dev twin (no keys)

    counter = CountingRail(StripeRail())
    if scenario.has_faults():
        return counter, ChaosRail(counter, scenario.resolved_faults())
    return counter, counter


# --------------------------------------------------------------------------- #
# Minimal Slack stand-in that serves the scenario's threads                   #
# --------------------------------------------------------------------------- #
class FakeSlack:
    def __init__(self, threads: list[list[dict]]) -> None:
        self._threads = threads
        self.posts: list[dict] = []

    def read_threads(self, channel_id: str) -> list[list[dict]]:
        return self._threads

    def post_message(self, channel_id: str, text: Any = None, blocks: Any = None) -> dict:
        self.posts.append({"channel": channel_id, "text": text})
        return {"ok": True, "ts": "0"}


# --------------------------------------------------------------------------- #
# The baseline agent                                                          #
# --------------------------------------------------------------------------- #
class NaiveCart:
    """Pays the top-ranked eligible offer with none of ProofCart's guardrails."""

    def __init__(self, rail: Rail) -> None:
        self.rail = rail

    def _create(self, amount_cents: int, currency: str, metadata: dict) -> Optional[RailResponse]:
        # (b) a FRESH idempotency key on EVERY create -- no idempotent reuse.
        key = uuid.uuid4().hex
        try:
            return self.rail.create_payment(key, amount_cents, currency, metadata)
        except Exception:
            return None

    @staticmethod
    def _row(resp: Optional[RailResponse], amount_cents: int, currency: str) -> LedgerEntry:
        ok = bool(resp and resp.ok and resp.status == "succeeded")
        ts = now_iso()
        return LedgerEntry(
            order_id="naive_" + uuid.uuid4().hex[:8],
            idempotency_key=uuid.uuid4().hex,
            amount_cents=amount_cents,
            currency=currency,  # type: ignore[arg-type]
            payment_intent_id=(resp.payment_intent_id if resp else None),
            # (c) marks SUCCEEDED purely from the response 'status' -- no verify().
            status=SettleStatus.SUCCEEDED if ok else SettleStatus.UNKNOWN,
            reason=None,
            created_at=ts,
            updated_at=ts,
        )

    def settle_once(self, terms: QuoteTerms, metadata: dict) -> tuple[list[LedgerEntry], Optional[RailResponse]]:
        """One naive settlement: create, and on any non-success re-create with a
        fresh key (no reconcile)."""
        amount, currency = terms.total_cents, terms.currency
        rows: list[LedgerEntry] = []
        resp = self._create(amount, currency, metadata)
        rows.append(self._row(resp, amount, currency))
        if not (resp and resp.ok and resp.status == "succeeded"):
            # (b) interrupted / not-ok -> blind re-create with a FRESH key.
            resp = self._create(amount, currency, metadata)
            rows.append(self._row(resp, amount, currency))
        return rows, resp


def run_naivecart(scenario: Scenario) -> RunOutcome:
    mandate = scenario.resolved_mandate()
    threads = scenario.resolved_threads()
    counter, rail = build_rail(scenario)
    now = now_iso()

    # SAME extraction + comparison as ProofCart.
    enriched, coverage = collect(threads, mandate, mandate.approver_id)
    offers = extract_offers(enriched, mandate)
    comparison = build_comparison(offers, mandate, coverage)

    record = RunRecord(
        request_id=mandate.request_id,
        scenario_id=scenario.id,
        final_state=State.COMPARED,
        comparison=comparison,
    )

    if not comparison.shortlist:
        # Nothing eligible -> even NaiveCart cannot pick a top offer.
        record.final_state = State.AWAITING_OWNER
        record.claimed_outcome = {"paid": False, "state": State.AWAITING_OWNER.value,
                                  "reason": "no eligible offer to auto-pay"}
        return RunOutcome("NaiveCart", scenario.id, record, offers, counter.distinct_charges(), now)

    # (a) pay the recommendation with NO approval / referee gate, whoever
    # "decided" (or nobody). The scenario's decision is ignored on purpose.
    top = comparison.shortlist[(comparison.recommendation_rank or 1) - 1]
    chosen = top.offer.current
    if scenario.tamper:
        # No terms-hash gate -> NaiveCart happily pays post-approval-mutated terms.
        chosen = chosen.model_copy(update=scenario.tamper)

    metadata = {"request_id": mandate.request_id, "supplier": top.offer.supplier_name}
    cart = NaiveCart(rail)
    rows, last = cart.settle_once(chosen, metadata)
    if scenario.duplicate_settle:
        # (b) a duplicate settle of the same order -> another fresh key -> charge.
        rows2, last2 = cart.settle_once(chosen, metadata)
        rows += rows2
        last = last2 or last

    # (c) SILENT OVER-CLAIM: reports paid:true from having *a* response back,
    # without verifying the PaymentIntent actually succeeded.
    claimed_paid = last is not None
    pid = last.payment_intent_id if last else None
    record.ledger = rows
    record.final_state = State.COMPLETE if claimed_paid else State.DECLINED
    record.claimed_outcome = {
        "paid": claimed_paid,
        "amount_cents": chosen.total_cents if claimed_paid else None,
        "payment_intent_id": pid,
        "state": record.final_state.value,
    }
    return RunOutcome("NaiveCart", scenario.id, record, offers, counter.distinct_charges(), now)
