#!/usr/bin/env python3
"""ProofCart demo -- runs the full owner-controlled flow and prints the narrative,
then the crash-safe settlement proof. Works in dev mode with no keys
(`PROOFCART_MODE=dev`), or against the three real apps in live mode.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from proofcart.config import get_settings  # noqa: E402
from proofcart.decision import build_approval, payment_mandate_from_approval  # noqa: E402
from proofcart.engine import OwnerDecision, run  # noqa: E402
from proofcart.ids import new_order_id  # noqa: E402
from proofcart.integrations.notion import NotionClient  # noqa: E402
from proofcart.integrations.slack import SlackClient  # noqa: E402
from proofcart.integrations.stripe_rail import StripeRail  # noqa: E402
from proofcart.schemas import OwnerMandate, SettleStatus  # noqa: E402
from proofcart.settlement import Ledger, settle  # noqa: E402
from proofcart.settlement.chaos import ChaosRail, FaultConfig  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _mandate() -> OwnerMandate:
    return OwnerMandate(**yaml.safe_load((ROOT / "data" / "mandate.yaml").read_text()))


def _ledger() -> Ledger:
    return Ledger(path=tempfile.mktemp(suffix=".jsonl"))


def main() -> None:
    st = get_settings()
    mandate = _mandate()
    # In live mode the approver is the real Slack owner id from .env.
    if st.is_live() and st.owner_id:
        mandate = mandate.model_copy(update={"approver_id": st.owner_id})
    channel = st.slack_channel_id or "C_DEV"
    print(f"=== ProofCart demo (mode={st.mode}) ===")
    for line in st.status_banner():
        print("   ", line)

    slack, notion = SlackClient(), NotionClient()

    # 1) Look only -- the shortlist
    rec = run(mandate, slack=slack, notion=notion, rail=StripeRail(), ledger=_ledger(),
              channel=channel, post=False)
    comp = rec.comparison
    print("\n--- SHORTLIST (owner sees this) ---")
    for it in comp.shortlist:
        t = it.offer.current
        star = "  <- recommended" if it.rank == comp.recommendation_rank else ""
        print(f"  #{it.rank} {it.offer.supplier_name:11s} ${t.total_cents/100:8,.2f}  by {t.delivery_by[:10]}"
              f"  [{it.evidence_quality.value}]{star}")
    print("  not eligible:")
    for it in comp.excluded:
        reason = it.why or "; ".join(it.eligibility.violations + it.eligibility.reasons) or "pending clarification"
        print(f"    {it.offer.supplier_name:11s} ${it.offer.current.total_cents/100:8,.2f}  -- {reason}")

    rec_item = comp.shortlist[(comp.recommendation_rank or 1) - 1]
    rec_sup = rec_item.offer.supplier_id

    # 2) Owner approves the recommendation -> referee -> settle
    print(f"\n--- OWNER APPROVES {rec_item.offer.supplier_name} ---")
    # In live mode, post the shortlist + outcome to Slack, write the Notion
    # record, and fire the real Stripe test charge; in dev, stay side-effect-free.
    r2 = run(mandate, slack=slack, notion=notion, rail=StripeRail(), ledger=_ledger(),
             decision=OwnerDecision(approve_supplier_id=rec_sup, approver_id=mandate.approver_id),
             channel=channel, post=st.is_live())
    warns = [f.code.value for f in (r2.verdict.flags if r2.verdict else []) if f.severity == "WARN"]
    print(f"  referee: {r2.verdict.verdict.value.upper()}  (surfaced WARNs: {warns})")
    if r2.ledger:
        e = r2.ledger[-1]
        print(f"  settled: {e.status.value}  ${e.amount_cents/100:,.2f}  PI {e.payment_intent_id}  -> state {r2.final_state.value}")
        if e.payment_intent_id and e.payment_intent_id.startswith("pi_") and st.is_live():
            print(f"  stripe:  https://dashboard.stripe.com/test/payments/{e.payment_intent_id}")

    # 3) Owner tries to approve the cheapest-but-late offer -> no payment
    bolt = next((it for it in comp.excluded if "Bolt" in it.offer.supplier_name), None)
    if bolt:
        print(f"\n--- OWNER TRIES TO APPROVE {bolt.offer.supplier_name} (cheapest ${bolt.offer.current.total_cents/100:,.2f}, but late) ---")
        r3 = run(mandate, slack=slack, notion=notion, rail=StripeRail(), ledger=_ledger(),
                 decision=OwnerDecision(approve_supplier_id=bolt.offer.supplier_id, approver_id=mandate.approver_id),
                 channel=channel, post=False)
        print(f"  outcome: {r3.final_state.value}  -- not an eligible option, no payment")

    # 4) A non-owner tries to approve -> no payment
    print("\n--- A NON-OWNER (U_INTRUDER) TRIES TO APPROVE THE RECOMMENDATION ---")
    r4 = run(mandate, slack=slack, notion=notion, rail=StripeRail(), ledger=_ledger(),
             decision=OwnerDecision(approve_supplier_id=rec_sup, approver_id="U_INTRUDER"),
             channel=channel, post=False)
    print(f"  referee: {r4.verdict.verdict.value.upper()}  -> state {r4.final_state.value}  -- identity check held")

    # 5) Reliability: kill the payment mid-flight -> exactly one charge
    print("\n--- RELIABILITY: process killed mid-payment (response dropped after charge) ---")
    chosen = rec_item.offer.current
    ap = build_approval(mandate, chosen, mandate.approver_id)
    pm = payment_mandate_from_approval(new_order_id(), mandate, chosen, ap)
    chaos = ChaosRail(StripeRail(), FaultConfig(drop_response_after_create=True))
    led = _ledger()
    entry = settle(pm, chaos, led)
    succeeded = [e for e in led.all() if e.status == SettleStatus.SUCCEEDED]
    print(f"  create attempts: {chaos.create_calls} (first response dropped, then reconciled by idempotency key)")
    print(f"  result: {entry.status.value}  ${entry.amount_cents/100:,.2f}  PI {entry.payment_intent_id}")
    print(f"  SUCCEEDED ledger rows: {len(succeeded)} (expect 1)  ->  no double charge")

    print("\n=== demo complete ===")


if __name__ == "__main__":
    main()
