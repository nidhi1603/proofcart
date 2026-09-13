"""Assertions -- deterministic predicates over a run's result.

Each assertion is a small factory that captures its parameters and returns an
``Assertion`` callable. The callable receives a ``RunOutcome`` (the resulting
``RunRecord``, the reconstructed offers, and the *ground-truth* count of distinct
successful charges recorded by the payment rail) and returns ``(passed, detail)``.

Honesty notes
-------------
* Charge-based assertions (``exactly_one_charge``, ``no_payment``, ``recovers``,
  ``no_overclaim``) read ``outcome.charges`` -- the number of DISTINCT succeeded
  PaymentIntent ids the rail actually committed for the run. The rail is the
  arbiter of how many charges occurred, never a create-call count and never the
  agent's own claim. A double charge is therefore visible even when the agent is
  blind to it.
* ``no_overclaim`` catches a *silent failure*: the agent claimed ``paid: true``
  while no real charge succeeded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from proofcart.schemas import ReconstructedOffer, RunRecord, SettleStatus


# --------------------------------------------------------------------------- #
# Result bundle an assertion reads                                            #
# --------------------------------------------------------------------------- #
@dataclass
class RunOutcome:
    agent: str
    scenario_id: str
    record: RunRecord
    offers: list[ReconstructedOffer]
    charges: int  # ground-truth distinct SUCCEEDED PaymentIntent ids (from the rail)
    now: str = ""
    approval: Any = None  # captured Approval, when one was built (for replay)
    chosen_terms: Any = None  # captured QuoteTerms adjudicated (for replay)
    error: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Assertion:
    label: str
    fn: Callable[[RunOutcome], tuple[bool, str]]

    def __call__(self, outcome: RunOutcome) -> tuple[bool, str]:
        return self.fn(outcome)


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _match(name_or_id_holder: Any, supplier: str) -> bool:
    o = getattr(name_or_id_holder, "offer", name_or_id_holder)
    s = (supplier or "").lower()
    return s in (o.supplier_name or "").lower() or s in (o.supplier_id or "").lower()


def _shortlist(record: RunRecord) -> list:
    return list(record.comparison.shortlist) if record.comparison else []


def _excluded(record: RunRecord) -> list:
    return list(record.comparison.excluded) if record.comparison else []


def _find_offer(offers: list[ReconstructedOffer], supplier: str):
    for o in offers:
        if _match(o, supplier):
            return o
    return None


def _claimed_paid(record: RunRecord) -> bool:
    co = record.claimed_outcome or {}
    return co.get("paid") is True


def _succeeded_ledger(record: RunRecord) -> list:
    return [e for e in (record.ledger or []) if e.status == SettleStatus.SUCCEEDED]


# --------------------------------------------------------------------------- #
# Comparison / eligibility predicates                                         #
# --------------------------------------------------------------------------- #
def shortlist_contains(supplier: str) -> Assertion:
    def fn(o: RunOutcome):
        hit = any(_match(it, supplier) for it in _shortlist(o.record))
        names = [it.offer.supplier_name for it in _shortlist(o.record)]
        return hit, f"shortlist={names}"
    return Assertion(f"shortlist_contains({supplier})", fn)


def not_in_shortlist(supplier: str) -> Assertion:
    def fn(o: RunOutcome):
        hit = any(_match(it, supplier) for it in _shortlist(o.record))
        names = [it.offer.supplier_name for it in _shortlist(o.record)]
        return (not hit), f"shortlist={names}"
    return Assertion(f"not_in_shortlist({supplier})", fn)


def shortlist_size(n: int) -> Assertion:
    def fn(o: RunOutcome):
        size = len(_shortlist(o.record))
        return size == n, f"shortlist size={size}, expected {n}"
    return Assertion(f"shortlist_size({n})", fn)


def shortlist_all_eligible() -> Assertion:
    def fn(o: RunOutcome):
        bad = [it.offer.supplier_name for it in _shortlist(o.record) if not it.eligibility.eligible]
        return (not bad), (f"ineligible in shortlist: {bad}" if bad else "all shortlisted are eligible")
    return Assertion("shortlist_all_eligible", fn)


def excluded_for_reason(supplier: str, reason: str) -> Assertion:
    def fn(o: RunOutcome):
        for it in _excluded(o.record):
            if _match(it, supplier):
                text = " ".join(
                    [it.why or ""] + list(it.eligibility.violations) + list(it.eligibility.reasons)
                ).lower()
                ok = reason.lower() in text
                return ok, f"{it.offer.supplier_name} reason={it.why!r}"
        return False, f"{supplier} not found in excluded"
    return Assertion(f"excluded_for_reason({supplier}, {reason!r})", fn)


def offer_status(supplier: str, status: str) -> Assertion:
    def fn(o: RunOutcome):
        off = _find_offer(o.offers, supplier)
        if off is None:
            return False, f"{supplier} offer not found"
        got = off.status.value
        return got == status, f"{off.supplier_name} status={got}, expected {status}"
    return Assertion(f"offer_status({supplier}, {status})", fn)


def offer_unit_price_cents(supplier: str, cents: int) -> Assertion:
    def fn(o: RunOutcome):
        off = _find_offer(o.offers, supplier)
        if off is None:
            return False, f"{supplier} offer not found"
        got = off.current.unit_price_cents
        return got == cents, f"{off.supplier_name} unit_price={got}, expected {cents}"
    return Assertion(f"offer_unit_price_cents({supplier}, {cents})", fn)


def offer_total_cents(supplier: str, cents: int) -> Assertion:
    def fn(o: RunOutcome):
        off = _find_offer(o.offers, supplier)
        if off is None:
            return False, f"{supplier} offer not found"
        got = off.current.total_cents
        return got == cents, f"{off.supplier_name} total={got}, expected {cents}"
    return Assertion(f"offer_total_cents({supplier}, {cents})", fn)


def recommendation_is(supplier: str) -> Assertion:
    def fn(o: RunOutcome):
        comp = o.record.comparison
        if not comp or not comp.recommendation_rank or not comp.shortlist:
            return False, "no recommendation"
        item = comp.shortlist[comp.recommendation_rank - 1]
        ok = _match(item, supplier)
        return ok, f"recommendation={item.offer.supplier_name}"
    return Assertion(f"recommendation_is({supplier})", fn)


# --------------------------------------------------------------------------- #
# Referee / state predicates                                                  #
# --------------------------------------------------------------------------- #
def verdict_is(kind: str) -> Assertion:
    def fn(o: RunOutcome):
        v = o.record.verdict
        if v is None:
            return False, "no referee verdict produced (agent has no gate)"
        got = v.verdict.value
        return got == kind, f"verdict={got}, expected {kind}"
    return Assertion(f"verdict_is({kind})", fn)


def final_state_in(states: list[str]) -> Assertion:
    want = [s for s in states]

    def fn(o: RunOutcome):
        got = o.record.final_state.value
        return got in want, f"final_state={got}, expected one of {want}"
    return Assertion(f"final_state_in({want})", fn)


def settled_amount_cents(cents: int) -> Assertion:
    def fn(o: RunOutcome):
        rows = _succeeded_ledger(o.record)
        amts = [e.amount_cents for e in rows]
        ok = any(a == cents for a in amts)
        return ok, f"succeeded ledger amounts={amts}, expected {cents}"
    return Assertion(f"settled_amount_cents({cents})", fn)


# --------------------------------------------------------------------------- #
# Payment-integrity predicates (ground truth = the rail)                      #
# --------------------------------------------------------------------------- #
def exactly_one_charge() -> Assertion:
    def fn(o: RunOutcome):
        return o.charges == 1, f"distinct successful charges={o.charges}, expected 1"
    return Assertion("exactly_one_charge", fn)


def no_payment() -> Assertion:
    def fn(o: RunOutcome):
        return o.charges == 0, f"distinct successful charges={o.charges}, expected 0"
    return Assertion("no_payment", fn)


def no_overclaim() -> Assertion:
    def fn(o: RunOutcome):
        claimed = _claimed_paid(o.record)
        over = claimed and o.charges == 0
        detail = f"claimed_paid={claimed}, real_charges={o.charges}"
        return (not over), (f"OVER-CLAIM: {detail}" if over else detail)
    return Assertion("no_overclaim", fn)


def recovers() -> Assertion:
    def fn(o: RunOutcome):
        complete = o.record.final_state.value in ("complete", "paid_record_pending")
        claimed = _claimed_paid(o.record)
        ok = o.charges == 1 and complete and claimed
        return ok, f"charges={o.charges}, final_state={o.record.final_state.value}, claimed_paid={claimed}"
    return Assertion("recovers", fn)


# --------------------------------------------------------------------------- #
# Registry (name -> factory) used by the grader                              #
# --------------------------------------------------------------------------- #
ASSERTIONS: dict[str, Callable[..., Assertion]] = {
    "shortlist_contains": shortlist_contains,
    "not_in_shortlist": not_in_shortlist,
    "shortlist_size": shortlist_size,
    "shortlist_all_eligible": shortlist_all_eligible,
    "excluded_for_reason": excluded_for_reason,
    "offer_status": offer_status,
    "offer_unit_price_cents": offer_unit_price_cents,
    "offer_total_cents": offer_total_cents,
    "recommendation_is": recommendation_is,
    "verdict_is": verdict_is,
    "final_state_in": final_state_in,
    "settled_amount_cents": settled_amount_cents,
    "exactly_one_charge": exactly_one_charge,
    "no_payment": no_payment,
    "no_overclaim": no_overclaim,
    "recovers": recovers,
}
