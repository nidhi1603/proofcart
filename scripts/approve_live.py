#!/usr/bin/env python3
"""Live example WITH a real Slack approval loop.

Posts the shortlist to the channel, then WAITS for the owner to reply
`approve <supplier>` (or `no`). No payment happens until the OWNER's own Slack
message authorizes it -- verified by their Slack user id. This is the honest
human-in-the-loop: the agent never approves itself.
"""
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from proofcart.config import get_settings  # noqa: E402
from proofcart.engine import OwnerDecision, run  # noqa: E402
from proofcart.integrations.notion import NotionClient  # noqa: E402
from proofcart.integrations.slack import SlackClient  # noqa: E402
from proofcart.integrations.stripe_rail import StripeRail  # noqa: E402
from proofcart.schemas import OwnerMandate  # noqa: E402
from proofcart.settlement import Ledger  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
TIMEOUT_S = 180
POLL_S = 4


def _ledger() -> Ledger:
    return Ledger(path=str(ROOT / "runs" / "ledger_live.jsonl"))


def main() -> int:
    st = get_settings()
    if not st.is_live():
        print("Run with PROOFCART_MODE=live."); return 1
    owner = st.owner_id
    ch = st.slack_channel_id
    if not owner or not ch:
        print("Need SLACK_CHANNEL_ID and PROOFCART_OWNER_ID in .env."); return 1

    m = OwnerMandate(**yaml.safe_load((ROOT / "data" / "mandate.yaml").read_text()))
    m = m.model_copy(update={"approver_id": owner})
    slack, notion = SlackClient(), NotionClient()

    # 1) Reconstruct + shortlist (no posting, no charge)
    rec = run(m, slack=slack, notion=notion, rail=StripeRail(), ledger=_ledger(), channel=ch, post=False)
    comp = rec.comparison
    if not comp.shortlist:
        slack.post_message(ch, "*ProofCart*: no eligible offers to approve.")
        print("No eligible offers."); return 0

    by_name = {it.offer.supplier_name.lower(): it.offer.supplier_id for it in comp.shortlist}
    lines = ["*ProofCart needs your approval.* Eligible offers:"]
    for it in comp.shortlist:
        t = it.offer.current
        lines.append(f"  {it.rank}. *{it.offer.supplier_name}* — ${t.total_cents/100:,.2f} by {str(t.delivery_by)[:10]}")
    lines.append(f"\nReply `approve <supplier>` (e.g. `approve {comp.shortlist[0].offer.supplier_name}`) to authorize the payment, or `no` to decline.")
    lines.append(f"Only <@{owner}> can approve. Stripe is TEST mode (no real money).")
    posted = slack.post_message(ch, "\n".join(lines))
    after_ts = float(posted.get("ts") or "0")
    print(f"Posted approval request to Slack. Waiting up to {TIMEOUT_S}s for owner {owner} to reply...")

    # 2) Wait for the OWNER's reply (identity-checked)
    chosen_sid = None
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline and chosen_sid is None:
        time.sleep(POLL_S)
        try:
            msgs = [msg for thread in slack.read_threads(ch) for msg in thread]
        except Exception as exc:
            print("  (poll error:", exc, ")"); continue
        for msg in msgs:
            if msg.get("user") != owner:
                continue  # only the configured owner can approve
            try:
                if float(msg.get("ts", "0")) <= after_ts:
                    continue  # only messages AFTER the request
            except ValueError:
                continue
            text = (msg.get("text") or "").strip().lower()
            if text in ("no", "decline", "reject", "cancel"):
                slack.post_message(ch, ":no_entry: Declined by owner. No payment made.")
                print("Owner declined."); return 0
            mm = re.search(r"approve\s+([a-z][\w .&-]*)", text)
            if mm:
                want = mm.group(1).strip()
                for sname, sid in by_name.items():
                    first = sname.split()[0]
                    if sname.startswith(want) or want.startswith(first) or first == want:
                        chosen_sid = sid
                        break
            if chosen_sid:
                break

    if chosen_sid is None:
        slack.post_message(ch, ":hourglass: No owner approval received in time. No payment made.")
        print("No approval received; no payment."); return 0

    # 3) Owner approved -> settle (posts outcome, writes Notion, real Stripe test charge)
    print("Owner approved supplier:", chosen_sid)
    r2 = run(m, slack=slack, notion=notion, rail=StripeRail(), ledger=_ledger(),
             decision=OwnerDecision(approve_supplier_id=chosen_sid, approver_id=owner),
             channel=ch, post=True)
    print("referee:", r2.verdict.verdict.value if r2.verdict else "-", "| state:", r2.final_state.value)
    if r2.ledger:
        e = r2.ledger[-1]
        print("SETTLED:", e.status.value, f"${e.amount_cents/100:.2f}", "PI:", e.payment_intent_id)
        if e.payment_intent_id and e.payment_intent_id.startswith("pi_"):
            print("STRIPE:", f"https://dashboard.stripe.com/test/payments/{e.payment_intent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
