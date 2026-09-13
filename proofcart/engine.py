"""ProofCart engine -- orchestrates the owner-controlled purchasing flow.

    collect (Slack) -> extract offers -> evidence -> compare/shortlist
      -> present (Slack + Notion) -> owner decides -> referee gate
      -> settle (crash-safe) -> record (Notion) + outcome (Slack)

Produces a `RunRecord`. The owner's decision is *injected* (`OwnerDecision`) --
in live mode it comes from an authenticated Slack approval; in eval it comes from
a scenario. Spending permission never comes from the engine or the eval.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from proofcart.agents.extractor import extract_offers
from proofcart.collector import collect
from proofcart.comparator import build_comparison
from proofcart.decision import ApprovalError, build_approval, payment_mandate_from_approval
from proofcart.evidence import assign_evidence
from proofcart.ids import new_order_id
from proofcart.referee import Referee
from proofcart.schemas import (
    Comparison,
    OwnerMandate,
    QuoteTerms,
    Rail,
    RunRecord,
    SettleStatus,
    State,
    VerdictKind,
    now_iso,
)
from proofcart.settlement import Ledger, settle


@dataclass
class OwnerDecision:
    """What the owner does after seeing the shortlist."""

    approve_supplier_id: Optional[str] = None  # supplier to approve; None = look only
    approver_id: Optional[str] = None  # identity approving (checked against the mandate)


def _shortlist_text(mandate: OwnerMandate, comp: Comparison) -> str:
    lines = [
        f"*ProofCart* — request `{mandate.request_id}`: {mandate.quantity}× {mandate.sku_or_spec}, "
        f"all-in ≤ ${mandate.budget_cents/100:,.0f}, by {mandate.must_haves.get('delivery_by','?')}.",
        f"Inspected {comp.coverage.companies_seen} companies / {comp.coverage.threads_seen} threads.",
        "*Eligible offers:*",
    ]
    for it in comp.shortlist:
        t = it.offer.current
        lines.append(
            f"  {it.rank}. {it.offer.supplier_name} — ${t.total_cents/100:,.2f} by {t.delivery_by[:10]} "
            f"[{it.evidence_quality.value}] — {it.why}"
        )
    if comp.excluded:
        lines.append("*Not eligible:*")
        for it in comp.excluded:
            reason = it.why or "; ".join(it.eligibility.violations + it.eligibility.reasons) or (
                "pending clarification" if it.eligibility.pending_clarification else "excluded"
            )
            lines.append(f"  • {it.offer.supplier_name} — ${it.offer.current.total_cents/100:,.2f} — {reason}")
    lines.append("_Reply to approve an exact quote. No payment happens without your approval._")
    return "\n".join(lines)


def _safe(fn, *a, **k):
    """Side effects (Slack/Notion) must never crash the core flow."""
    try:
        return fn(*a, **k)
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)}


def run(
    mandate: OwnerMandate,
    *,
    slack,
    notion,
    rail: Rail,
    ledger: Optional[Ledger] = None,
    decision: Optional[OwnerDecision] = None,
    channel: Optional[str] = None,
    tool_log: Optional[list[dict]] = None,
    now: Optional[str] = None,
    post: bool = True,
) -> RunRecord:
    now = now or now_iso()
    tool_log = tool_log or []
    ledger = ledger if ledger is not None else Ledger()
    ref = Referee()

    # 1-3. collect -> extract -> compare
    raw_threads = slack.read_threads(channel or "C_DEV")
    enriched, coverage = collect(raw_threads, mandate, mandate.approver_id)
    offers = extract_offers(enriched, mandate)
    comparison = build_comparison(offers, mandate, coverage)

    record = RunRecord(
        request_id=mandate.request_id,
        final_state=State.AWAITING_OWNER,
        comparison=comparison,
    )

    # 4. present the shortlist (Slack) + durable comparison (Notion)
    if post:
        _safe(slack.post_message, channel or "C_DEV", _shortlist_text(mandate, comparison))
        _safe(
            notion.upsert_order_record,
            mandate.request_id,
            {
                "state": State.AWAITING_OWNER.value,
                "shortlist": [
                    {"rank": it.rank, "supplier": it.offer.supplier_name,
                     "total_cents": it.offer.current.total_cents,
                     "delivery_by": it.offer.current.delivery_by,
                     "evidence": it.evidence_quality.value}
                    for it in comparison.shortlist
                ],
                "excluded": [
                    {"supplier": it.offer.supplier_name, "reason": it.why or
                     "; ".join(it.eligibility.violations + it.eligibility.reasons)}
                    for it in comparison.excluded
                ],
            },
        )

    # 5. owner decision
    if not decision or not decision.approve_supplier_id:
        record.claimed_outcome = {"paid": False, "state": State.AWAITING_OWNER.value}
        return record

    chosen_item = next(
        (it for it in comparison.shortlist if it.offer.supplier_id == decision.approve_supplier_id),
        None,
    )
    if chosen_item is None:
        # The owner tried to approve something that is not an eligible option.
        record.final_state = State.DECLINED
        record.claimed_outcome = {"paid": False, "state": State.DECLINED.value,
                                  "reason": "selection is not an eligible offer"}
        return record

    chosen: QuoteTerms = chosen_item.offer.current
    evidence = assign_evidence(chosen_item.offer, tool_log)

    # 6. authenticated approval -> referee gate
    approver = decision.approver_id or mandate.approver_id
    try:
        approval = build_approval(mandate, chosen, approver)
    except ApprovalError:
        approval = None  # wrong identity -> no valid approval; referee escalates
    verdict = ref.adjudicate(chosen, mandate, approval, evidence, tool_log, now)
    record.approval = approval
    record.verdict = verdict

    if verdict.verdict != VerdictKind.PERMIT:
        record.final_state = (
            State.DECLINED if verdict.verdict == VerdictKind.BLOCK else State.AWAITING_OWNER
        )
        record.claimed_outcome = {"paid": False, "state": record.final_state.value,
                                  "verdict": verdict.verdict.value}
        return record

    # 7. settle (crash-safe) -> record + outcome
    order_id = new_order_id()
    pm = payment_mandate_from_approval(order_id, mandate, chosen, approval)
    record.final_state = State.EXECUTING
    entry = settle(pm, rail, ledger)
    record.ledger = ledger.all()

    if entry.status == SettleStatus.SUCCEEDED:
        record.final_state = State.PAID_RECORD_PENDING
        if post:
            wrote = _safe(
                notion.upsert_order_record,
                order_id,
                {"state": State.COMPLETE.value, "supplier": chosen_item.offer.supplier_name,
                 "amount_cents": entry.amount_cents, "payment_intent_id": entry.payment_intent_id,
                 "request_id": mandate.request_id, "quote_id": chosen.quote_id},
            )
            _safe(
                slack.post_message,
                channel or "C_DEV",
                f":white_check_mark: Approved & paid: {chosen_item.offer.supplier_name} "
                f"${entry.amount_cents/100:,.2f} (order `{order_id}`, PI `{entry.payment_intent_id}`). "
                f"Notion record updated.",
            )
            if not wrote.get("ok", True):  # record repair pending; do not re-pay
                record.claimed_outcome = {"paid": True, "amount_cents": entry.amount_cents,
                                          "state": State.PAID_RECORD_PENDING.value,
                                          "payment_intent_id": entry.payment_intent_id}
                return record
        record.final_state = State.COMPLETE
        record.claimed_outcome = {"paid": True, "amount_cents": entry.amount_cents,
                                  "payment_intent_id": entry.payment_intent_id,
                                  "state": State.COMPLETE.value}
    else:
        # UNKNOWN/PENDING -> stay recoverable; FAILED/declined -> declined. Never claim paid.
        record.final_state = (
            State.PAYMENT_UNKNOWN if entry.status in (SettleStatus.UNKNOWN, SettleStatus.PENDING)
            else State.DECLINED
        )
        record.claimed_outcome = {"paid": False, "state": record.final_state.value,
                                  "settle_status": entry.status.value}
    return record
