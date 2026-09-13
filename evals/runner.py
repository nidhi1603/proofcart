"""ProofCart eval runner.

    python3 -m evals.runner

Runs every scenario through BOTH agents in dev mode (no API keys), scores each
run with the same grader, checks referee determinism via replay, computes the
reliability metrics, writes ``reports/scoreboard.md`` + ``reports/scoreboard.json``,
and prints a side-by-side table. Exit status is 0 when ProofCart passes all of
its intended behaviors and replay is deterministic; NaiveCart's failures are data,
not a runner error.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

# Dev mode BEFORE importing the engine, so extraction/settlement use no keys.
os.environ.setdefault("PROOFCART_MODE", "dev")
os.environ["PROOFCART_MODE"] = "dev"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from proofcart.agents.extractor import extract_offers  # noqa: E402
from proofcart.collector import collect  # noqa: E402
from proofcart.comparator import build_comparison  # noqa: E402
from proofcart.decision import (  # noqa: E402
    ApprovalError,
    build_approval,
    payment_mandate_from_approval,
)
from proofcart.engine import OwnerDecision, run as engine_run  # noqa: E402
from proofcart.evidence import assign_evidence  # noqa: E402
from proofcart.integrations.notion import NotionClient  # noqa: E402
from proofcart.referee import Referee  # noqa: E402
from proofcart.schemas import (  # noqa: E402
    RunRecord,
    SettleStatus,
    State,
    VerdictKind,
    now_iso,
)
from proofcart.settlement import Ledger, settle  # noqa: E402

from evals.assertions import RunOutcome  # noqa: E402
from evals.baseline import FakeSlack, build_rail, run_naivecart  # noqa: E402
from evals.grader import grade  # noqa: E402
from evals.replay import replay_all  # noqa: E402
from evals.report import write_reports  # noqa: E402
from evals.scenario import Scenario, load_scenarios  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _tmp_ledger() -> Ledger:
    return Ledger(path=tempfile.mktemp(suffix=".jsonl"))


def _resolve_supplier_id(offers, supplier: str) -> str | None:
    s = (supplier or "").lower()
    for o in offers:
        if o.supplier_id.lower() == s:
            return o.supplier_id
    for o in offers:
        if s in o.supplier_name.lower() or s in o.supplier_id.lower():
            return o.supplier_id
    return None


def _find_shortlist_item(comparison, supplier: str | None):
    if not comparison or not comparison.shortlist:
        return None
    if supplier is None:
        rank = comparison.recommendation_rank or 1
        return comparison.shortlist[rank - 1]
    s = supplier.lower()
    for it in comparison.shortlist:
        if s in it.offer.supplier_name.lower() or s in it.offer.supplier_id.lower():
            return it
    return None


# --------------------------------------------------------------------------- #
# ProofCart adapter                                                           #
# --------------------------------------------------------------------------- #
def run_proofcart(scenario: Scenario) -> RunOutcome:
    mandate = scenario.resolved_mandate()
    threads = scenario.resolved_threads()
    counter, rail = build_rail(scenario)
    now = now_iso()

    enriched, coverage = collect(threads, mandate, mandate.approver_id)
    offers = extract_offers(enriched, mandate)
    comparison = build_comparison(offers, mandate, coverage)

    if scenario.tamper:
        return _proofcart_tamper(scenario, mandate, comparison, offers, now)
    if scenario.duplicate_settle:
        return _proofcart_duplicate(scenario, mandate, comparison, offers, counter, now)

    decision = None
    resolved_sup = None
    if scenario.decision and scenario.decision.approve_supplier:
        resolved_sup = _resolve_supplier_id(offers, scenario.decision.approve_supplier)
        decision = OwnerDecision(
            approve_supplier_id=resolved_sup,
            approver_id=scenario.decision.approver or mandate.approver_id,
        )

    slack = FakeSlack(threads)
    notion = NotionClient()
    record = engine_run(
        mandate, slack=slack, notion=notion, rail=rail, ledger=_tmp_ledger(),
        decision=decision, post=False, now=now,
    )

    chosen_terms = None
    if resolved_sup and record.comparison:
        for it in record.comparison.shortlist:
            if it.offer.supplier_id == resolved_sup:
                chosen_terms = it.offer.current
                break

    return RunOutcome(
        "ProofCart", scenario.id, record, offers, counter.distinct_charges(),
        now=now, approval=record.approval, chosen_terms=chosen_terms,
        extra={"mandate": mandate},
    )


def _proofcart_tamper(scenario, mandate, comparison, offers, now) -> RunOutcome:
    """Build the approval on the ORIGINAL terms, then mutate the quote and let
    the real referee catch it via terms_hash. No settlement occurs."""
    ref = Referee()
    sup = scenario.decision.approve_supplier if scenario.decision else None
    item = _find_shortlist_item(comparison, sup)
    record = RunRecord(
        request_id=mandate.request_id, scenario_id=scenario.id,
        final_state=State.AWAITING_OWNER, comparison=comparison,
    )
    if item is None:
        record.claimed_outcome = {"paid": False, "state": record.final_state.value,
                                  "reason": "approved supplier not eligible"}
        return RunOutcome("ProofCart", scenario.id, record, offers, 0, now=now,
                          extra={"mandate": mandate})

    chosen_orig = item.offer.current
    approver = (scenario.decision.approver if scenario.decision else None) or mandate.approver_id
    try:
        approval = build_approval(mandate, chosen_orig, approver)
    except ApprovalError:
        approval = None

    tampered = chosen_orig.model_copy(update=scenario.tamper)
    evidence = assign_evidence(item.offer, [])
    verdict = ref.adjudicate(tampered, mandate, approval, evidence, [], now)
    record.approval = approval
    record.verdict = verdict
    if verdict.verdict == VerdictKind.BLOCK:
        record.final_state = State.DECLINED
    elif verdict.verdict == VerdictKind.PERMIT:
        record.final_state = State.APPROVED  # would be unexpected for a real tamper
    else:
        record.final_state = State.AWAITING_OWNER
    record.claimed_outcome = {"paid": False, "state": record.final_state.value,
                              "verdict": verdict.verdict.value}
    return RunOutcome("ProofCart", scenario.id, record, offers, 0, now=now,
                      approval=approval, chosen_terms=tampered, extra={"mandate": mandate})


def _proofcart_duplicate(scenario, mandate, comparison, offers, counter, now) -> RunOutcome:
    """Approve once, then settle the SAME order twice against the SAME ledger and
    rail -- the exactly-once mechanism must make the second settle idempotent."""
    ref = Referee()
    sup = scenario.decision.approve_supplier if scenario.decision else None
    item = _find_shortlist_item(comparison, sup)
    record = RunRecord(
        request_id=mandate.request_id, scenario_id=scenario.id,
        final_state=State.AWAITING_OWNER, comparison=comparison,
    )
    if item is None:
        record.claimed_outcome = {"paid": False, "state": record.final_state.value}
        return RunOutcome("ProofCart", scenario.id, record, offers, 0, now=now,
                          extra={"mandate": mandate})

    chosen = item.offer.current
    approver = (scenario.decision.approver if scenario.decision else None) or mandate.approver_id
    approval = build_approval(mandate, chosen, approver)
    evidence = assign_evidence(item.offer, [])
    verdict = ref.adjudicate(chosen, mandate, approval, evidence, [], now)
    record.approval = approval
    record.verdict = verdict

    order_id = "ord_dup_" + scenario.id
    pm = payment_mandate_from_approval(order_id, mandate, chosen, approval)
    ledger = _tmp_ledger()
    e1 = settle(pm, counter, ledger)
    settle(pm, counter, ledger)  # duplicate settle of the SAME order
    record.ledger = ledger.all()

    if e1.status == SettleStatus.SUCCEEDED:
        record.final_state = State.COMPLETE
        record.claimed_outcome = {"paid": True, "amount_cents": e1.amount_cents,
                                  "payment_intent_id": e1.payment_intent_id,
                                  "state": State.COMPLETE.value}
    else:
        record.final_state = State.DECLINED
        record.claimed_outcome = {"paid": False, "state": State.DECLINED.value,
                                  "settle_status": e1.status.value}

    return RunOutcome("ProofCart", scenario.id, record, offers, counter.distinct_charges(),
                      now=now, approval=approval, chosen_terms=chosen, extra={"mandate": mandate})


# --------------------------------------------------------------------------- #
# Metrics (numerator / denominator; defined in report.py's summary text)      #
# --------------------------------------------------------------------------- #
def _claimed_paid(outcome: RunOutcome) -> bool:
    return (outcome.record.claimed_outcome or {}).get("paid") is True


def _final_complete(outcome: RunOutcome) -> bool:
    return outcome.record.final_state.value in ("complete", "paid_record_pending")


def metrics_for(agent: str, rows: list[dict]) -> dict:
    total = len(rows)
    scen_pass = sum(1 for r in rows if r[agent]["grade"].passed)

    # EOR: exactly-one-charge among crash/duplicate scenarios that SHOULD charge once.
    eor_rows = [r for r in rows
                if r["scenario"].category in ("recovery", "duplicate")
                and r["scenario"].expected_charges == 1]
    eor_num = sum(1 for r in eor_rows if r[agent]["outcome"].charges == 1)

    # SFD: claimed paid but no real charge succeeded.
    claim_rows = [r for r in rows if _claimed_paid(r[agent]["outcome"])]
    overclaim = [r for r in claim_rows if r[agent]["outcome"].charges == 0]

    # Unauthorized payment: money moved with no valid owner approval of the terms.
    unauth = [r for r in rows
              if not r["scenario"].authorized and r[agent]["outcome"].charges >= 1]

    # False-blocking: an authorized, should-charge deal that produced no charge.
    false_block = [r for r in rows
                   if r["scenario"].authorized and (r["scenario"].expected_charges or 0) >= 1
                   and r[agent]["outcome"].charges == 0]

    # Recovery success: recovery scenarios that reached exactly one charge + done.
    rec_rows = [r for r in rows if r["scenario"].category == "recovery"]
    rec_ok = sum(1 for r in rec_rows
                 if r[agent]["outcome"].charges == 1 and _final_complete(r[agent]["outcome"]))

    return {
        "scenarios_passed": {"num": scen_pass, "den": total},
        "exactly_one_charge_rate": {"num": eor_num, "den": len(eor_rows)},
        "overclaim_rate": {"num": len(overclaim), "den": len(claim_rows),
                           "scenarios": [r["scenario"].id for r in overclaim]},
        "unauthorized_payments": {"num": len(unauth), "den": total,
                                  "scenarios": [r["scenario"].id for r in unauth]},
        "false_blocks": {"num": len(false_block),
                         "den": sum(1 for r in rows if r["scenario"].authorized
                                    and (r["scenario"].expected_charges or 0) >= 1),
                         "scenarios": [r["scenario"].id for r in false_block]},
        "recovery_success_rate": {"num": rec_ok, "den": len(rec_rows)},
    }


def _note(scenario: Scenario, pc: RunOutcome, nc: RunOutcome,
          pc_pass: bool, nc_pass: bool) -> str:
    bits = []
    if pc.charges != nc.charges:
        bits.append(f"charges PC={pc.charges} vs NC={nc.charges}")
    pc_over = _claimed_paid(pc) and pc.charges == 0
    nc_over = _claimed_paid(nc) and nc.charges == 0
    if nc_over and not pc_over:
        bits.append("NC over-claims paid")
    if nc.charges >= 2 and pc.charges <= 1:
        bits.append("NC double-charge")
    if not scenario.authorized and nc.charges >= 1 and pc.charges == 0:
        bits.append("NC pays without approval")
    # NaiveCart can charge the right amount yet still fail a scenario because it
    # produces no referee verdict (it has no authorization gate).
    if not bits and pc_pass and not nc_pass:
        bits.append("NC has no referee verdict/gate")
    return "; ".join(bits) or "agents agree"


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    scenarios = load_scenarios()
    if not scenarios:
        print("no scenarios found under evals/scenarios/", file=sys.stderr)
        return 1

    rows: list[dict] = []
    pc_outcomes: list[RunOutcome] = []
    for s in scenarios:
        pc = run_proofcart(s)
        nc = run_naivecart(s)
        pc_outcomes.append(pc)
        rows.append({
            "scenario": s,
            "ProofCart": {"outcome": pc, "grade": grade(pc, s)},
            "NaiveCart": {"outcome": nc, "grade": grade(nc, s)},
        })

    replays = replay_all(pc_outcomes)
    metrics = {"ProofCart": metrics_for("ProofCart", rows),
               "NaiveCart": metrics_for("NaiveCart", rows)}

    # Assemble the serializable scoreboard.
    board_rows = []
    for r in rows:
        s = r["scenario"]
        pc, nc = r["ProofCart"], r["NaiveCart"]
        board_rows.append({
            "id": s.id,
            "title": s.title or s.id,
            "category": s.category,
            "authorized": s.authorized,
            "expected_charges": s.expected_charges,
            "proofcart": {
                "passed": pc["grade"].passed,
                "summary": pc["grade"].summary,
                "charges": pc["outcome"].charges,
                "claimed_paid": _claimed_paid(pc["outcome"]),
                "final_state": pc["outcome"].record.final_state.value,
                "assertions": [{"label": a.label, "passed": a.passed, "detail": a.detail}
                               for a in pc["grade"].results],
            },
            "naivecart": {
                "passed": nc["grade"].passed,
                "summary": nc["grade"].summary,
                "charges": nc["outcome"].charges,
                "claimed_paid": _claimed_paid(nc["outcome"]),
                "final_state": nc["outcome"].record.final_state.value,
                "assertions": [{"label": a.label, "passed": a.passed, "detail": a.detail}
                               for a in nc["grade"].results],
            },
            "note": _note(s, pc["outcome"], nc["outcome"],
                          pc["grade"].passed, nc["grade"].passed),
        })

    board = {
        "generated_at": now_iso(),
        "mode": "dev (no API keys; deterministic dev extractor + dev payment twin)",
        "scenarios": board_rows,
        "metrics": metrics,
        "replay": [{"scenario_id": rp.scenario_id, "deterministic": rp.deterministic,
                    "matches_recorded": rp.matches_recorded, "verdict": rp.verdict}
                   for rp in replays],
    }

    out_dir = pathlib.Path(__file__).resolve().parents[1] / "reports"
    md_path, json_path = write_reports(board, out_dir)

    # Console table.
    _print_table(board_rows, metrics, replays)

    pc_all_pass = metrics["ProofCart"]["scenarios_passed"]["num"] == len(rows)
    replay_ok = all(rp.deterministic and rp.matches_recorded for rp in replays) if replays else True
    print(f"\nwrote {md_path}")
    print(f"wrote {json_path}")

    if pc_all_pass and replay_ok:
        print("\nGREEN: ProofCart passed all intended behaviors; referee replay is deterministic.")
        return 0
    print("\nRED: ProofCart did not pass all scenarios or replay was non-deterministic.", file=sys.stderr)
    return 1


def _print_table(board_rows, metrics, replays) -> None:
    print(f"\n{'scenario':30s} {'ProofCart':10s} {'NaiveCart':10s}  note")
    print("-" * 92)
    for r in board_rows:
        pc = "PASS" if r["proofcart"]["passed"] else "FAIL"
        nc = "PASS" if r["naivecart"]["passed"] else "FAIL"
        print(f"{r['id']:30s} {pc:10s} {nc:10s}  {r['note']}")
    print("-" * 92)
    for agent in ("ProofCart", "NaiveCart"):
        m = metrics[agent]
        sp = m["scenarios_passed"]
        eor = m["exactly_one_charge_rate"]
        oc = m["overclaim_rate"]
        ua = m["unauthorized_payments"]
        fb = m["false_blocks"]
        rr = m["recovery_success_rate"]
        print(f"{agent}: scenarios {sp['num']}/{sp['den']} | "
              f"exactly-one-charge {eor['num']}/{eor['den']} | "
              f"over-claim {oc['num']}/{oc['den']} | "
              f"unauthorized-pay {ua['num']} | false-block {fb['num']} | "
              f"recovery {rr['num']}/{rr['den']}")
    if replays:
        det = sum(1 for rp in replays if rp.deterministic and rp.matches_recorded)
        print(f"replay: {det}/{len(replays)} referee verdicts identical across re-runs and matching the recorded run")


if __name__ == "__main__":
    raise SystemExit(main())
