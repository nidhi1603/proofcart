"""Grader -- score one ``RunOutcome`` against a scenario's declared expectations.

Every expectation is one assertion looked up by name in
``evals.assertions.ASSERTIONS`` and run against the outcome. A scenario passes
only when all of its assertions pass. The same grader scores ProofCart and
NaiveCart, so a pass/fail is directly comparable across the two agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from evals.assertions import ASSERTIONS, RunOutcome
from evals.scenario import Scenario


@dataclass
class AssertionResult:
    label: str
    passed: bool
    detail: str


@dataclass
class GradeResult:
    scenario_id: str
    agent: str
    passed: bool
    results: list[AssertionResult] = field(default_factory=list)

    @property
    def summary(self) -> str:
        n_pass = sum(1 for r in self.results if r.passed)
        return f"{n_pass}/{len(self.results)} assertions passed"


def grade(outcome: RunOutcome, scenario: Scenario) -> GradeResult:
    results: list[AssertionResult] = []
    for exp in scenario.expect:
        factory = ASSERTIONS.get(exp.check)
        if factory is None:
            results.append(AssertionResult(exp.check, False, "unknown assertion"))
            continue
        try:
            assertion = factory(**exp.params())
        except TypeError as e:
            results.append(AssertionResult(exp.check, False, f"bad params: {e}"))
            continue
        try:
            passed, detail = assertion(outcome)
        except Exception as e:  # a broken assertion must fail, never crash the run
            passed, detail = False, f"error: {e!r}"
        results.append(AssertionResult(assertion.label, passed, detail))

    passed = all(r.passed for r in results) if results else True
    return GradeResult(scenario.id, outcome.agent, passed, results)
