"""Report writer -- render the scoreboard to Markdown + JSON.

Consumes the ``board`` dict assembled by the runner and writes:

* ``reports/scoreboard.md``   -- a human-readable, side-by-side scoreboard with a
  scenario table, the metric summary (each metric defined as numerator/denominator),
  an honest "Not measured" line, and a "Known ways to fool it" line.
* ``reports/scoreboard.json`` -- the full machine-readable board (per-assertion
  detail included) for CI / regression diffing.
"""
from __future__ import annotations

import json
import pathlib


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "n/a"
    return f"{100 * num / den:.0f}%"


def _ratio(m: dict) -> str:
    return f"{m['num']}/{m['den']}"


def _metric_lines(agent: str, m: dict) -> list[str]:
    sp = m["scenarios_passed"]
    eor = m["exactly_one_charge_rate"]
    oc = m["overclaim_rate"]
    ua = m["unauthorized_payments"]
    fb = m["false_blocks"]
    rr = m["recovery_success_rate"]
    lines = [
        f"**{agent}**",
        "",
        f"- Scenarios passed: {_ratio(sp)} ({_pct(sp['num'], sp['den'])})",
        f"- Exactly-one-charge under crash/duplicate (EOR): {_ratio(eor)} ({_pct(eor['num'], eor['den'])})",
        f"- Silent-failure over-claim rate (claimed paid but 0 real charges): {_ratio(oc)}"
        + (f" — {', '.join(oc['scenarios'])}" if oc.get("scenarios") else ""),
        f"- Unauthorized payments (target 0): {ua['num']}"
        + (f" — {', '.join(ua['scenarios'])}" if ua.get("scenarios") else ""),
        f"- False-blocking of an eligible+approved deal (target 0): {fb['num']}"
        + (f" — {', '.join(fb['scenarios'])}" if fb.get("scenarios") else ""),
        f"- Recovery success rate: {_ratio(rr)} ({_pct(rr['num'], rr['den'])})",
        "",
    ]
    return lines


def _markdown(board: dict) -> str:
    L: list[str] = []
    L.append("# ProofCart reliability scoreboard")
    L.append("")
    L.append(f"_Generated {board['generated_at']} · mode: {board['mode']}_")
    L.append("")
    L.append(
        "ProofCart is scored against NaiveCart, a fair **component-removal** baseline "
        "that shares ProofCart's exact extraction and comparison but removes the "
        "enforced guardrails: (a) the owner-approval / referee gate, (b) crash-safe "
        "exactly-once settlement, and (c) settlement verification. Both agents are "
        "scored by the same grader against the same expectations. Charge counts are "
        "measured at the payment rail (the arbiter of how many charges occurred), "
        "never asserted a priori."
    )
    L.append("")

    # Scenario table.
    L.append("## Scenarios")
    L.append("")
    L.append("| Scenario | ProofCart | NaiveCart | Note |")
    L.append("|---|---|---|---|")
    for r in board["scenarios"]:
        pc = "✅ PASS" if r["proofcart"]["passed"] else "❌ FAIL"
        nc = "✅ PASS" if r["naivecart"]["passed"] else "❌ FAIL"
        title = r["title"].replace("|", "\\|")
        note = r["note"].replace("|", "\\|")
        L.append(f"| **{r['id']}** — {title} | {pc} | {nc} | {note} |")
    L.append("")

    # Charge / claim ledger (ground truth), for transparency.
    L.append("### Measured charges & claims (ground truth = rail)")
    L.append("")
    L.append("| Scenario | authorized | expected charges | PC charges | PC claims paid | NC charges | NC claims paid |")
    L.append("|---|---|---|---|---|---|---|")
    for r in board["scenarios"]:
        L.append(
            f"| {r['id']} | {r['authorized']} | {r['expected_charges']} | "
            f"{r['proofcart']['charges']} | {r['proofcart']['claimed_paid']} | "
            f"{r['naivecart']['charges']} | {r['naivecart']['claimed_paid']} |"
        )
    L.append("")

    # Metrics.
    L.append("## Metrics")
    L.append("")
    L.append(
        "Each metric is numerator/denominator with the denominator scoped to the "
        "relevant scenarios. A \"charge\" is a distinct SUCCEEDED PaymentIntent id "
        "for an order; a double charge is >1."
    )
    L.append("")
    for agent in ("ProofCart", "NaiveCart"):
        L.extend(_metric_lines(agent, board["metrics"][agent]))

    # Replay / determinism.
    L.append("## Determinism (referee replay)")
    L.append("")
    if board["replay"]:
        det = sum(1 for rp in board["replay"] if rp["deterministic"] and rp["matches_recorded"])
        L.append(
            f"Re-ran `Referee.adjudicate` twice over each recorded run's exact inputs: "
            f"**{det}/{len(board['replay'])}** produced verdicts identical across both "
            f"re-runs AND identical to the verdict recorded during the run."
        )
        L.append("")
        L.append("| Scenario | verdict | identical across re-runs | matches recorded run |")
        L.append("|---|---|---|---|")
        for rp in board["replay"]:
            L.append(
                f"| {rp['scenario_id']} | {rp['verdict']} | {rp['deterministic']} | {rp['matches_recorded']} |"
            )
    else:
        L.append("No adjudicated runs to replay.")
    L.append("")

    # Honesty: not measured.
    L.append("## Not measured (honest scope)")
    L.append("")
    L.append(
        "- **No live external calls.** This suite runs entirely in dev mode: no live "
        "DeepSeek/Anthropic extraction, no live Slack, Notion, or Stripe. The dev "
        "payment twin is idempotent and in-memory; the Slack/Notion sides are stubbed."
    )
    L.append(
        "- **Deterministic extraction.** Offers are parsed by the deterministic dev "
        "extractor, not an LLM, so this does not measure LLM extraction accuracy, "
        "prompt-injection robustness of a live model, or model variance."
    )
    L.append(
        "- **Regression suite, not an unseen benchmark.** Scenarios are hand-authored "
        "fixtures the system was built against; they measure that known behaviors hold, "
        "not generalization to unseen supplier language or novel adversarial inputs."
    )
    L.append(
        "- **Settlement faults are simulated.** Crash/interruption is injected via "
        "`ChaosRail`, not by killing a real process against a real gateway; wall-clock "
        "backoff timing and true concurrency are not exercised."
    )
    L.append("")

    # Honesty: known ways to fool it.
    L.append("## Known ways to fool it")
    L.append("")
    L.append(
        "- A live LLM extractor could still mis-parse adversarial supplier text "
        "(wrong price/date/quantity) upstream of every downstream check; the referee "
        "gates authorization and arithmetic, not extraction truth."
    )
    L.append(
        "- Evidence here is `supplier_stated` (no trusted verification tool calls in the "
        "eval), so the referee surfaces WARNs but the owner's approval still authorizes; "
        "a scenario with a forged-but-consistent quote would pass the deterministic checks."
    )
    L.append(
        "- The rail is the charge arbiter; a real gateway that reports success but has "
        "not truly captured funds, or that mutates the idempotency contract, would defeat "
        "the exactly-once assumption."
    )
    L.append(
        "- The owner-identity gate trusts the injected approver id; a compromised owner "
        "identity (upstream auth breach) would pass the referee."
    )
    L.append(
        "- Scenarios are the spec; a behavior no scenario exercises is, by definition, "
        "unmeasured here."
    )
    L.append("")
    return "\n".join(L)


def write_reports(board: dict, out_dir: str | pathlib.Path) -> tuple[str, str]:
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "scoreboard.md"
    json_path = out / "scoreboard.json"
    md_path.write_text(_markdown(board), encoding="utf-8")
    json_path.write_text(json.dumps(board, indent=2, default=str), encoding="utf-8")
    return str(md_path), str(json_path)
