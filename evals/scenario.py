"""Scenario model + loader for the ProofCart eval suite.

A *scenario* is a self-contained fixture that drives BOTH agents (ProofCart and
NaiveCart) through identical inputs and declares the *safe/correct* expectations
they are graded against. Scenarios are product-centered: by default they run the
product's own seeded Slack threads (``data/slack_threads.json``) and mandate
(``data/mandate.yaml``) through the real engine; a scenario may override either.

Supported fields (all optional except ``id`` / ``expect``):

* ``threads``          -- inline supplier threads (list of message lists). If
  omitted, the product's seeded threads are used verbatim.
* ``mandate``          -- shallow overrides merged onto ``data/mandate.yaml``
  (``must_haves`` is merged key-wise; everything else is replaced).
* ``decision``         -- ``{approve_supplier, approver}``. ``approve_supplier``
  is matched against a supplier name or id; ``approver`` defaults to the owner.
* ``faults``           -- a ``FaultConfig`` (drop_response_after_create, timeout,
  http_503_first_n, decline) applied to the settlement rail.
* ``tamper``           -- QuoteTerms field overrides applied to the chosen quote
  AFTER the owner approval is built (exercises the referee's terms-hash gate).
* ``duplicate_settle`` -- settle the same approved order twice (exactly-once).
* ``expect``           -- a list of ``{check: <assertion>, ...params}`` items.

Metric tags (declare the *intent*; metrics compare it against measured reality):

* ``authorized``       -- a valid owner approval of the exact terms exists, so a
  resulting charge is authorized.
* ``expected_charges`` -- how many distinct successful charges *should* occur.
* ``category``         -- grouping label used by the metric summary.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from proofcart.schemas import OwnerMandate
from proofcart.settlement.chaos import FaultConfig

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCENARIO_DIR = pathlib.Path(__file__).resolve().parent / "scenarios"


# --------------------------------------------------------------------------- #
# Product data (the eval runs the product's own fixtures by default)          #
# --------------------------------------------------------------------------- #
def base_mandate_dict() -> dict:
    return dict(yaml.safe_load((_ROOT / "data" / "mandate.yaml").read_text()))


def default_threads() -> list[list[dict]]:
    return json.loads((_ROOT / "data" / "slack_threads.json").read_text())


# --------------------------------------------------------------------------- #
# Scenario model                                                              #
# --------------------------------------------------------------------------- #
class Expectation(BaseModel):
    """One assertion: ``check`` names it; any other keys are its parameters."""

    model_config = ConfigDict(extra="allow")
    check: str

    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class DecisionSpec(BaseModel):
    approve_supplier: Optional[str] = None  # supplier name or id to approve
    approver: Optional[str] = None  # identity approving (defaults to the owner)


class Scenario(BaseModel):
    id: str
    title: str = ""
    description: str = ""

    # Inputs
    threads: Optional[list[list[dict]]] = None
    mandate: dict[str, Any] = Field(default_factory=dict)
    decision: Optional[DecisionSpec] = None
    faults: dict[str, Any] = Field(default_factory=dict)
    tamper: Optional[dict[str, Any]] = None
    duplicate_settle: bool = False

    # Expectations
    expect: list[Expectation] = Field(default_factory=list)

    # Metric tags (intent; the runner measures reality and compares)
    authorized: bool = False
    expected_charges: Optional[int] = None
    category: str = "general"

    # ------------------------------------------------------------------ #
    # Resolution helpers                                                 #
    # ------------------------------------------------------------------ #
    def resolved_mandate(self) -> OwnerMandate:
        base = base_mandate_dict()
        for k, v in (self.mandate or {}).items():
            if k == "must_haves" and isinstance(v, dict):
                merged = dict(base.get("must_haves") or {})
                merged.update(v)
                base["must_haves"] = merged
            else:
                base[k] = v
        return OwnerMandate(**base)

    def resolved_threads(self) -> list[list[dict]]:
        return self.threads if self.threads is not None else default_threads()

    def resolved_faults(self) -> FaultConfig:
        return FaultConfig(**(self.faults or {}))

    def has_faults(self) -> bool:
        f = self.resolved_faults()
        return bool(
            f.drop_response_after_create or f.timeout or f.decline or f.http_503_first_n
        )


# --------------------------------------------------------------------------- #
# Loader                                                                      #
# --------------------------------------------------------------------------- #
def load_scenarios(directory: str | pathlib.Path | None = None) -> list[Scenario]:
    """Load and validate every ``*.yaml`` under ``directory`` (sorted by name)."""
    d = pathlib.Path(directory) if directory else _SCENARIO_DIR
    scenarios: list[Scenario] = []
    for path in sorted(d.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        if not data:
            continue
        scenarios.append(Scenario(**data))
    return scenarios
