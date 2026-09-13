"""Replay -- prove the referee is deterministic.

The referee is the authorization gate; its verdict must be a pure function of
(terms, mandate, approval, evidence, tool_log, now). This module re-runs
``Referee.adjudicate`` twice over the exact inputs a recorded run adjudicated and
asserts the two verdicts are byte-identical (and identical to the one recorded in
the run). It reconstructs the inputs from the ``RunOutcome`` (the adjudicated
QuoteTerms and the built Approval are captured there), so the replay uses the
same evidence and the same ``now`` the run used -- no hidden clock dependence.
"""
from __future__ import annotations

from dataclasses import dataclass

from proofcart.evidence import assign_evidence
from proofcart.referee import Referee
from proofcart.schemas import canonical_json

from evals.assertions import RunOutcome


@dataclass
class ReplayResult:
    scenario_id: str
    deterministic: bool
    matches_recorded: bool
    verdict: str
    detail: str


def _find_offer_for_terms(outcome: RunOutcome):
    """The reconstructed offer whose current terms were adjudicated."""
    terms = outcome.chosen_terms
    if terms is None:
        return None
    for off in outcome.offers:
        if off.supplier_id == terms.supplier_id:
            return off
    return None


def replay_outcome(outcome: RunOutcome) -> ReplayResult | None:
    """Re-adjudicate twice; return None if this run had nothing to adjudicate."""
    terms = outcome.chosen_terms
    approval = outcome.approval
    if terms is None or outcome.record.verdict is None:
        return None

    mandate = outcome.extra.get("mandate")
    if mandate is None:
        return None

    offer = _find_offer_for_terms(outcome)
    evidence = assign_evidence(offer, []) if offer is not None else []
    now = outcome.now

    ref = Referee()
    v1 = ref.adjudicate(terms, mandate, approval, evidence, [], now)
    v2 = ref.adjudicate(terms, mandate, approval, evidence, [], now)

    j1 = canonical_json(v1)
    j2 = canonical_json(v2)
    deterministic = j1 == j2
    matches_recorded = canonical_json(outcome.record.verdict) == j1

    return ReplayResult(
        scenario_id=outcome.scenario_id,
        deterministic=deterministic,
        matches_recorded=matches_recorded,
        verdict=v1.verdict.value,
        detail=(
            f"twice-identical={deterministic}, matches_recorded_run={matches_recorded}, "
            f"verdict={v1.verdict.value}"
        ),
    )


def replay_all(outcomes: list[RunOutcome]) -> list[ReplayResult]:
    out: list[ReplayResult] = []
    for oc in outcomes:
        if oc.agent != "ProofCart":
            continue
        r = replay_outcome(oc)
        if r is not None:
            out.append(r)
    return out
