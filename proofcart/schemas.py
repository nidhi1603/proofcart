"""ProofCart typed contracts.

Conventions
-----------
* Money is ALWAYS integer cents. Never floats.
* Timestamps are UTC ISO-8601 strings.
* Every cross-module data flow goes through these models so the referee,
  grader, UI, and eval all read the same shapes.

Design notes tied to the review findings this spec absorbed:
* QuoteTerms carries subtotal/shipping/tax/discount/total AND supplier
  identity, payment recipient, quote id + version, and expiry -- so the
  budget test (which includes shipping + tax) is representable and approval
  can bind to the exact terms and the exact recipient.
* Evidence is labelled supplier_stated / system_checked / unresolved. A bare
  tool-call reference is NOT proof; SYSTEM_CHECKED requires the cited call's
  args (supplier, product, timestamp) to match the claim.
* Settlement persists the PaymentIntent id before confirmation and models an
  UNKNOWN outcome so an uncertain creation is reconciled, never blindly
  recreated. exactly-once is a *mechanism*, reported after measurement.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, computed_field

Currency = Literal["USD"]


# --------------------------------------------------------------------------- #
# Owner mandate                                                               #
# --------------------------------------------------------------------------- #
class OwnerMandate(BaseModel):
    """What the owner wants. Loaded from data/mandate.yaml (or Notion)."""

    request_id: str
    approver_id: str  # the ONLY identity permitted to approve payment
    sku_or_spec: str
    quantity: int = Field(ge=1)
    budget_cents: int = Field(ge=0)  # HARD cap, all-in (subtotal + shipping + tax - discount)
    target_cents: Optional[int] = None  # "worthwhile" anchor; informs ranking, never auto-authorizes
    # e.g. {"delivery_by": "2026-09-18T17:00:00Z", "no_substitution": true, "no_partial": true}
    must_haves: dict[str, Any] = Field(default_factory=dict)
    ranking_priorities: list[str] = Field(default_factory=lambda: ["total", "delivery"])
    permitted_channels: list[str] = Field(default_factory=list)  # Slack channel ids to read


# --------------------------------------------------------------------------- #
# Offer terms                                                                 #
# --------------------------------------------------------------------------- #
class QuoteTerms(BaseModel):
    supplier_id: str
    supplier_name: str
    payment_recipient: str  # who actually gets paid; bound at approval time
    quote_id: str
    quote_version: int = 1
    sku: str
    product_desc: str
    unit_price_cents: int = Field(ge=0)
    quantity: int = Field(ge=1)
    shipping_cents: int = Field(ge=0, default=0)
    tax_cents: int = Field(ge=0, default=0)
    discount_cents: int = Field(ge=0, default=0)
    delivery_by: Optional[str] = None  # UTC ISO; None => unknown => pending clarification
    return_policy: Optional[str] = None
    currency: Currency = "USD"
    expires_at: Optional[str] = None  # quote validity window

    @computed_field  # type: ignore[misc]
    @property
    def subtotal_cents(self) -> int:
        return self.unit_price_cents * self.quantity

    @computed_field  # type: ignore[misc]
    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.shipping_cents + self.tax_cents - self.discount_cents


# --------------------------------------------------------------------------- #
# Evidence  (a call_id existing is NOT proof; see FieldEvidence.checked_args)  #
# --------------------------------------------------------------------------- #
class EvidenceLabel(str, Enum):
    SUPPLIER_STATED = "supplier_stated"  # asserted by the supplier; not independently checked
    SYSTEM_CHECKED = "system_checked"  # verified against a trusted tool call / API object
    UNRESOLVED = "unresolved"  # missing, stale, or contradictory


class FieldEvidence(BaseModel):
    field: str
    value: Any = None
    label: EvidenceLabel
    source_ref: Optional[str] = None  # slack message ts / tool call id / API object id
    # For SYSTEM_CHECKED: the cited call's args/return, so we can confirm it is
    # about the SAME supplier, product, and a fresh-enough timestamp.
    checked_args: Optional[dict[str, Any]] = None
    note: Optional[str] = None


# --------------------------------------------------------------------------- #
# Reconstructed offer                                                         #
# --------------------------------------------------------------------------- #
class NegotiationStatus(str, Enum):
    SUPPLIER_OFFER = "supplier_offer"
    BUYER_COUNTER = "buyer_counter"  # a buyer ask the supplier NEVER accepted -- not a real offer
    SUPPLIER_REVISION = "supplier_revision"
    ACCEPTED = "accepted"
    UNRESOLVED = "unresolved"


class ReconstructedOffer(BaseModel):
    supplier_id: str
    supplier_name: str
    current: QuoteTerms
    status: NegotiationStatus
    version_history: list[QuoteTerms] = Field(default_factory=list)
    evidence: list[FieldEvidence] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    source_message_refs: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Eligibility, coverage, shortlist                                            #
# --------------------------------------------------------------------------- #
class Eligibility(BaseModel):
    eligible: bool
    pending_clarification: bool = False
    violations: list[str] = Field(default_factory=list)  # hard-constraint failures
    reasons: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    companies_seen: int
    threads_seen: int
    retrieval_cutoff: str  # iso
    gaps: list[str] = Field(default_factory=list)  # permission failures, unread attachments, pagination gaps


class ShortlistItem(BaseModel):
    rank: int
    offer: ReconstructedOffer
    eligibility: Eligibility
    why: str  # tied to facts, e.g. "lowest confirmed total meeting the deadline"
    tradeoffs: list[str] = Field(default_factory=list)
    evidence_quality: EvidenceLabel  # overall label (business fit is separate, see `why`)


class Comparison(BaseModel):
    request_id: str
    shortlist: list[ShortlistItem]  # up to 5 eligible
    excluded: list[ShortlistItem] = Field(default_factory=list)  # with reasons
    coverage: Coverage
    recommendation_rank: Optional[int] = None


# --------------------------------------------------------------------------- #
# Approval  (binds to the exact quote, recipient, amount, and identity)        #
# --------------------------------------------------------------------------- #
class Approval(BaseModel):
    request_id: str
    approver_id: str  # must equal OwnerMandate.approver_id
    supplier_id: str
    quote_id: str
    quote_version: int
    payment_recipient: str
    amount_cents: int
    terms_hash: str  # binds to the exact QuoteTerms
    approved_at: str
    expires_at: str


# --------------------------------------------------------------------------- #
# Settlement                                                                  #
# --------------------------------------------------------------------------- #
class SettleStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"  # creation outcome uncertain -> reconcile the SAME op, never blind-recreate


class PaymentMandate(BaseModel):
    order_id: str  # unique logical order id (one transactional row)
    request_id: str
    quote_id: str
    quote_version: int
    payment_recipient: str
    amount_cents: int
    currency: Currency = "USD"
    idempotency_key: str  # stable per (order_id, terms_hash)
    terms_hash: str
    authorized_by: str  # the approver_id (a human). Never "referee" in the default workflow.


class LedgerEntry(BaseModel):
    order_id: str
    idempotency_key: str
    amount_cents: int
    currency: Currency = "USD"
    payment_intent_id: Optional[str] = None  # persisted BEFORE confirm, so we can retrieve the exact object
    status: SettleStatus = SettleStatus.PENDING
    reason: Optional[str] = None
    created_at: str
    updated_at: str


class RailResponse(BaseModel):
    """Uniform result from a payment rail (real Stripe test mode, or a dev twin)."""

    ok: bool
    payment_intent_id: Optional[str] = None
    # 'succeeded' | 'declined' | 'retryable' | 'requires_action' | 'unknown' | ...
    status: Optional[str] = None
    amount_cents: Optional[int] = None
    currency: Optional[Currency] = None
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Rail(Protocol):
    """Payment rail interface. StripeRail (integrations) implements this; the
    settlement pipeline depends only on this Protocol so faults/dev twins swap in.
    Never resolve 'does a payment exist?' via search -- retrieve by id only."""

    def create_payment(
        self, idempotency_key: str, amount_cents: int, currency: str, metadata: dict[str, Any]
    ) -> "RailResponse": ...

    def retrieve_payment(self, payment_intent_id: str) -> "RailResponse": ...


# --------------------------------------------------------------------------- #
# Referee  (narrow, deterministic; sees policy + typed offer + tool log;       #
#           never interprets supplier free-text and never sees ground truth)   #
# --------------------------------------------------------------------------- #
class FlagCode(str, Enum):
    OVER_BUDGET = "over_budget"
    ARITHMETIC_MISMATCH = "arithmetic_mismatch"
    DEADLINE_MISSED = "deadline_missed"
    SUBSTITUTION = "substitution"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    UNVERIFIED_CLAIM = "unverified_claim"  # cited call doesn't match field/value/supplier/product/timestamp
    RECIPIENT_MISMATCH = "recipient_mismatch"
    QUOTE_EXPIRED = "quote_expired"
    APPROVAL_INVALID = "approval_invalid"
    TERMS_CHANGED_AFTER_APPROVAL = "terms_changed_after_approval"


class Flag(BaseModel):
    code: FlagCode
    severity: Literal["BLOCK", "WARN"]
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class VerdictKind(str, Enum):
    PERMIT = "permit"  # ONLY after a valid owner approval + all deterministic checks pass
    ESCALATE = "escalate"  # needs owner decision / clarification
    BLOCK = "block"  # a hard constraint failed


class Verdict(BaseModel):
    verdict: VerdictKind
    flags: list[Flag] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    terms_hash: str


# --------------------------------------------------------------------------- #
# Recovery state machine                                                      #
# --------------------------------------------------------------------------- #
class State(str, Enum):
    COLLECTING = "collecting"
    COMPARED = "compared"
    AWAITING_OWNER = "awaiting_owner"
    SELECTED = "selected"
    APPROVED = "approved"
    EXECUTING = "executing"
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_UNKNOWN = "payment_unknown"  # interrupted response -> reconcile before any retry
    PAID_RECORD_PENDING = "paid_record_pending"  # charged, but Notion/Slack record incomplete
    COMPLETE = "complete"
    REJECTED = "rejected"
    DECLINED = "declined"


# --------------------------------------------------------------------------- #
# Run record (what the eval grader scores and replay reads)                    #
# --------------------------------------------------------------------------- #
class RunRecord(BaseModel):
    request_id: str
    scenario_id: Optional[str] = None
    run_no: int = 0
    final_state: State
    comparison: Optional[Comparison] = None
    approval: Optional[Approval] = None
    verdict: Optional[Verdict] = None
    ledger: list[LedgerEntry] = Field(default_factory=list)
    trajectory_path: Optional[str] = None
    # What the agent SAYS happened, for silent-failure divergence vs the verified ledger:
    claimed_outcome: Optional[dict[str, Any]] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# Fields that define "the deal" for hashing/idempotency. Changing any of these
# is a different deal and requires fresh approval.
_TERMS_KEYS = (
    "supplier_id",
    "payment_recipient",
    "quote_id",
    "quote_version",
    "sku",
    "unit_price_cents",
    "quantity",
    "shipping_cents",
    "tax_cents",
    "discount_cents",
    "delivery_by",
    "currency",
)


def terms_hash(t: QuoteTerms) -> str:
    payload = {k: getattr(t, k) for k in _TERMS_KEYS}
    payload["total_cents"] = t.total_cents
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def idempotency_key(order_id: str, t_hash: str) -> str:
    return hashlib.sha256(f"{order_id}:{t_hash}".encode()).hexdigest()[:40]
