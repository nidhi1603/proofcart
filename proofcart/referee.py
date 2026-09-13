"""Referee -- the narrow, deterministic authorization gate.

Pure Python. It sees the typed offer, the owner policy, an optional owner
approval, the field-evidence labels, and the tool-call log. It **never**
interprets supplier natural language and **never** sees ground truth. It emits
only PERMIT / ESCALATE / BLOCK.

Checks (each may append a ``Flag``):

* arithmetic     -- ``total_cents == subtotal + shipping + tax - discount``
* budget         -- all-in ``total_cents <= mandate.budget_cents``
* deadline       -- ``delivery_by <= must_haves['delivery_by']``
* no-substitution -- exact sku when required
* missing-required-field
* recipient present (and, with an approval, matching)
* quote not expired
* if an approval is supplied:
    - ``approval.approver_id == mandate.approver_id``     else APPROVAL_INVALID
    - approval not past ``expires_at``                     else APPROVAL_INVALID
    - ``approval.terms_hash == terms_hash(terms)``         else TERMS_CHANGED_AFTER_APPROVAL
* claim-provenance -- any load-bearing field that is UNRESOLVED or only
  SUPPLIER_STATED -> UNVERIFIED_CLAIM (WARN)

Verdict policy:

* any BLOCK-severity flag                                     -> BLOCK
* else if no VALID owner approval, or a load-bearing field is
  genuinely UNRESOLVED / missing                              -> ESCALATE
* else                                                        -> PERMIT

**PERMIT requires a valid owner approval.** Spending permission comes from the
owner, never from the evaluation. A *supplier-stated* load-bearing field is
surfaced as a WARN but does not block a PERMIT once the owner has explicitly
approved the exact terms (the owner saw the evidence quality and chose to
proceed). A field with no evidence, or UNRESOLVED evidence, is a real gap and
still escalates -- you cannot pay on a missing field.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

try:
    from proofcart.schemas import (
        Approval,
        EvidenceLabel,
        FieldEvidence,
        Flag,
        FlagCode,
        OwnerMandate,
        QuoteTerms,
        Verdict,
        VerdictKind,
        terms_hash,
    )
except ModuleNotFoundError:  # pragma: no cover - path bootstrap
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from proofcart.schemas import (
        Approval,
        EvidenceLabel,
        FieldEvidence,
        Flag,
        FlagCode,
        OwnerMandate,
        QuoteTerms,
        Verdict,
        VerdictKind,
        terms_hash,
    )


def _parse_iso(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Referee:
    """Deterministic authorization gate. Construct once, reuse across offers."""

    # Fields whose support must be independently verified before a PERMIT.
    LOAD_BEARING_FIELDS: tuple[str, ...] = (
        "unit_price_cents",
        "shipping_cents",
        "delivery_by",
    )

    def adjudicate(
        self,
        terms: QuoteTerms,
        mandate: OwnerMandate,
        approval: Optional[Approval],
        evidence: list[FieldEvidence],
        tool_log: list[dict],
        now: str,
    ) -> Verdict:
        flags: list[Flag] = []
        checks: list[str] = []
        now_dt = _parse_iso(now) or datetime.now(timezone.utc)
        thash = terms_hash(terms)

        # --- arithmetic ----------------------------------------------------- #
        checks.append("arithmetic")
        expected_total = (
            terms.unit_price_cents * terms.quantity
            + terms.shipping_cents
            + terms.tax_cents
            - terms.discount_cents
        )
        if terms.total_cents != expected_total:
            flags.append(
                Flag(
                    code=FlagCode.ARITHMETIC_MISMATCH,
                    severity="BLOCK",
                    detail=f"total {terms.total_cents} != subtotal+shipping+tax-discount {expected_total}",
                    evidence={"total_cents": terms.total_cents, "expected": expected_total},
                )
            )

        # --- budget (all-in) ------------------------------------------------ #
        checks.append("budget")
        if terms.total_cents > mandate.budget_cents:
            flags.append(
                Flag(
                    code=FlagCode.OVER_BUDGET,
                    severity="BLOCK",
                    detail=f"all-in {terms.total_cents} > cap {mandate.budget_cents}",
                    evidence={"total_cents": terms.total_cents, "budget_cents": mandate.budget_cents},
                )
            )

        # --- deadline ------------------------------------------------------- #
        checks.append("deadline")
        deadline = mandate.must_haves.get("delivery_by")
        if deadline is not None:
            if terms.delivery_by is None:
                flags.append(
                    Flag(
                        code=FlagCode.MISSING_REQUIRED_FIELD,
                        severity="BLOCK",
                        detail="delivery_by required by mandate but not present on the quote",
                        evidence={"field": "delivery_by"},
                    )
                )
            else:
                d_offer, d_req = _parse_iso(terms.delivery_by), _parse_iso(deadline)
                if d_offer is None:
                    flags.append(
                        Flag(
                            code=FlagCode.MISSING_REQUIRED_FIELD,
                            severity="BLOCK",
                            detail=f"delivery_by {terms.delivery_by!r} is unparseable",
                            evidence={"field": "delivery_by", "value": terms.delivery_by},
                        )
                    )
                elif d_req and d_offer > d_req:
                    flags.append(
                        Flag(
                            code=FlagCode.DEADLINE_MISSED,
                            severity="BLOCK",
                            detail=f"delivery {terms.delivery_by} is after deadline {deadline}",
                            evidence={"delivery_by": terms.delivery_by, "deadline": deadline},
                        )
                    )

        # --- no-substitution / exact product ------------------------------- #
        checks.append("no_substitution")
        if terms.sku != mandate.sku_or_spec:
            flags.append(
                Flag(
                    code=FlagCode.SUBSTITUTION,
                    severity="BLOCK",
                    detail=f"quote sku {terms.sku!r} != required {mandate.sku_or_spec!r}",
                    evidence={"sku": terms.sku, "required": mandate.sku_or_spec},
                )
            )

        # --- missing required fields --------------------------------------- #
        checks.append("required_fields")
        if terms.unit_price_cents <= 0:
            flags.append(
                Flag(
                    code=FlagCode.MISSING_REQUIRED_FIELD,
                    severity="BLOCK",
                    detail="unit_price_cents is missing/zero",
                    evidence={"field": "unit_price_cents", "value": terms.unit_price_cents},
                )
            )
        if terms.quantity != mandate.quantity:
            flags.append(
                Flag(
                    code=FlagCode.MISSING_REQUIRED_FIELD,
                    severity="BLOCK",
                    detail=f"quantity {terms.quantity} != required {mandate.quantity}",
                    evidence={"field": "quantity", "value": terms.quantity, "required": mandate.quantity},
                )
            )

        # --- recipient present (and matching the approval) ----------------- #
        checks.append("recipient")
        if not (terms.payment_recipient or "").strip():
            flags.append(
                Flag(
                    code=FlagCode.RECIPIENT_MISMATCH,
                    severity="BLOCK",
                    detail="payment_recipient is empty",
                    evidence={"field": "payment_recipient"},
                )
            )
        elif approval is not None and approval.payment_recipient != terms.payment_recipient:
            flags.append(
                Flag(
                    code=FlagCode.RECIPIENT_MISMATCH,
                    severity="BLOCK",
                    detail=(
                        f"approval recipient {approval.payment_recipient!r} != "
                        f"quote recipient {terms.payment_recipient!r}"
                    ),
                    evidence={
                        "approval_recipient": approval.payment_recipient,
                        "quote_recipient": terms.payment_recipient,
                    },
                )
            )

        # --- quote expiry --------------------------------------------------- #
        checks.append("quote_expiry")
        if terms.expires_at is not None:
            exp = _parse_iso(terms.expires_at)
            if exp is not None and exp < now_dt:
                flags.append(
                    Flag(
                        code=FlagCode.QUOTE_EXPIRED,
                        severity="BLOCK",
                        detail=f"quote expired at {terms.expires_at} (now {now})",
                        evidence={"expires_at": terms.expires_at, "now": now},
                    )
                )

        # --- approval validity --------------------------------------------- #
        approval_valid = False
        if approval is not None:
            checks.append("approval_identity")
            checks.append("approval_expiry")
            checks.append("approval_terms_binding")
            identity_ok = approval.approver_id == mandate.approver_id
            exp = _parse_iso(approval.expires_at)
            not_expired = exp is not None and exp >= now_dt
            terms_ok = approval.terms_hash == thash
            amount_ok = approval.amount_cents == terms.total_cents

            if not identity_ok:
                flags.append(
                    Flag(
                        code=FlagCode.APPROVAL_INVALID,
                        severity="BLOCK",
                        detail=(
                            f"approver {approval.approver_id!r} is not the mandate approver "
                            f"{mandate.approver_id!r}"
                        ),
                        evidence={"approver_id": approval.approver_id, "expected": mandate.approver_id},
                    )
                )
            if exp is None or not not_expired:
                flags.append(
                    Flag(
                        code=FlagCode.APPROVAL_INVALID,
                        severity="BLOCK",
                        detail=f"approval expired/invalid expiry {approval.expires_at!r} (now {now})",
                        evidence={"expires_at": approval.expires_at, "now": now},
                    )
                )
            if not terms_ok:
                flags.append(
                    Flag(
                        code=FlagCode.TERMS_CHANGED_AFTER_APPROVAL,
                        severity="BLOCK",
                        detail="approval.terms_hash does not match the current terms",
                        evidence={"approval_terms_hash": approval.terms_hash, "terms_hash": thash},
                    )
                )
            elif not amount_ok:
                # Hash matched but the amount field disagrees -- treat as tamper.
                flags.append(
                    Flag(
                        code=FlagCode.TERMS_CHANGED_AFTER_APPROVAL,
                        severity="BLOCK",
                        detail=f"approval amount {approval.amount_cents} != all-in total {terms.total_cents}",
                        evidence={"amount_cents": approval.amount_cents, "total_cents": terms.total_cents},
                    )
                )
            approval_valid = identity_ok and not_expired and terms_ok and amount_ok

        # --- claim provenance ---------------------------------------------- #
        # A supplier-stated load-bearing field is surfaced as a WARN, but the
        # owner's explicit approval may still authorize it. A field that is
        # UNRESOLVED or has no evidence is a genuine gap -- it forces an
        # escalation even with an approval (you cannot pay on a missing field).
        checks.append("claim_provenance")
        by_field = {e.field: e for e in (evidence or [])}
        unresolved_fields: list[str] = []
        supplier_only_fields: list[str] = []
        for field in self.LOAD_BEARING_FIELDS:
            ev = by_field.get(field)
            if ev is None or ev.label == EvidenceLabel.UNRESOLVED:
                unresolved_fields.append(field)
                label = ev.label.value if ev else "no_evidence"
            elif ev.label == EvidenceLabel.SUPPLIER_STATED:
                supplier_only_fields.append(field)
                label = ev.label.value
            else:  # SYSTEM_CHECKED -> nothing to flag
                continue
            flags.append(
                Flag(
                    code=FlagCode.UNVERIFIED_CLAIM,
                    severity="WARN",
                    detail=f"load-bearing field {field!r} is unverified ({label})",
                    evidence={"field": field, "label": label},
                )
            )

        # --- verdict policy ------------------------------------------------- #
        # BLOCK on any hard violation. Otherwise ESCALATE when there is no valid
        # owner approval or a load-bearing field is genuinely unresolved. A valid
        # owner approval clears supplier-stated WARNs -> PERMIT.
        has_block = any(f.severity == "BLOCK" for f in flags)
        if has_block:
            verdict = VerdictKind.BLOCK
        elif approval is None or not approval_valid or unresolved_fields:
            verdict = VerdictKind.ESCALATE
        else:
            verdict = VerdictKind.PERMIT

        return Verdict(verdict=verdict, flags=flags, checks_run=checks, terms_hash=thash)


# --------------------------------------------------------------------------- #
# Self-tests                                                                   #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from proofcart.schemas import now_iso

    NOW = "2026-09-13T12:00:00Z"

    mandate = OwnerMandate(
        request_id="req-1",
        approver_id="U_OWNER",
        sku_or_spec="SENSOR-KIT-A",
        quantity=20,
        budget_cents=100_000,
        must_haves={"delivery_by": "2026-09-18T17:00:00Z", "no_substitution": True, "no_partial": True},
    )

    def clean_terms(unit=4500, ship=1000, tax=0, deliver="2026-09-16T17:00:00Z",
                    expires="2026-09-20T00:00:00Z", sku="SENSOR-KIT-A") -> QuoteTerms:
        return QuoteTerms(
            supplier_id="sup_a", supplier_name="Acme", payment_recipient="Acme",
            quote_id="sup_a-q1", quote_version=1, sku=sku, product_desc=sku,
            unit_price_cents=unit, quantity=20, shipping_cents=ship, tax_cents=tax,
            delivery_by=deliver, expires_at=expires,
        )

    def checked_evidence(terms: QuoteTerms) -> list[FieldEvidence]:
        return [
            FieldEvidence(field=f, value=getattr(terms, f), label=EvidenceLabel.SYSTEM_CHECKED,
                          source_ref="call_x", checked_args={"call_id": "call_x"})
            for f in Referee.LOAD_BEARING_FIELDS
        ]

    def valid_approval(terms: QuoteTerms) -> Approval:
        return Approval(
            request_id="req-1", approver_id="U_OWNER", supplier_id=terms.supplier_id,
            quote_id=terms.quote_id, quote_version=terms.quote_version,
            payment_recipient=terms.payment_recipient, amount_cents=terms.total_cents,
            terms_hash=terms_hash(terms), approved_at=NOW, expires_at="2026-09-14T12:00:00Z",
        )

    ref = Referee()

    # 1) Valid approval + all checks pass + all claims verified -> PERMIT.
    t = clean_terms()
    v = ref.adjudicate(t, mandate, valid_approval(t), checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.PERMIT, (v.verdict, [f.code for f in v.flags])

    # 2) No approval (clean, verified) -> ESCALATE (never PERMIT without owner).
    t = clean_terms()
    v = ref.adjudicate(t, mandate, None, checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.ESCALATE, (v.verdict, [f.code for f in v.flags])

    # 3) Over budget -> BLOCK.
    t = clean_terms(unit=5200)  # $1040 + $10 ship = $1050
    v = ref.adjudicate(t, mandate, valid_approval(t), checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.BLOCK and any(f.code == FlagCode.OVER_BUDGET for f in v.flags)

    # 4) Missing deadline -> BLOCK (MISSING_REQUIRED_FIELD).
    t = clean_terms(deliver=None)
    v = ref.adjudicate(t, mandate, valid_approval(t), checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.BLOCK and any(
        f.code == FlagCode.MISSING_REQUIRED_FIELD for f in v.flags
    ), [f.code for f in v.flags]

    # 5) Supplier-stated load-bearing claims + a VALID owner approval -> PERMIT,
    #    with the unverified fields surfaced as WARN flags.
    t = clean_terms()
    weak = [FieldEvidence(field=f, value=getattr(t, f), label=EvidenceLabel.SUPPLIER_STATED)
            for f in Referee.LOAD_BEARING_FIELDS]
    v = ref.adjudicate(t, mandate, valid_approval(t), weak, [], NOW)
    assert v.verdict == VerdictKind.PERMIT, (v.verdict, [f.code for f in v.flags])
    assert any(f.code == FlagCode.UNVERIFIED_CLAIM and f.severity == "WARN" for f in v.flags)

    # 5b) A genuinely UNRESOLVED / missing load-bearing field -> ESCALATE even
    #     with a valid approval (cannot pay on a missing field).
    t = clean_terms()
    v = ref.adjudicate(t, mandate, valid_approval(t), [], [], NOW)  # no evidence at all
    assert v.verdict == VerdictKind.ESCALATE, (v.verdict, [f.code for f in v.flags])

    # 6) Wrong approver -> BLOCK (APPROVAL_INVALID).
    t = clean_terms()
    bad = valid_approval(t)
    bad = bad.model_copy(update={"approver_id": "U_INTRUDER"})
    v = ref.adjudicate(t, mandate, bad, checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.BLOCK and any(f.code == FlagCode.APPROVAL_INVALID for f in v.flags)

    # 7) Terms changed after approval -> BLOCK (TERMS_CHANGED_AFTER_APPROVAL).
    t = clean_terms(unit=4500)
    appr = valid_approval(t)                       # bound to the $4500 terms
    t2 = clean_terms(unit=4600)                    # price changed after approval
    v = ref.adjudicate(t2, mandate, appr, checked_evidence(t2), [], NOW)
    assert v.verdict == VerdictKind.BLOCK and any(
        f.code == FlagCode.TERMS_CHANGED_AFTER_APPROVAL for f in v.flags
    )

    # 8) Expired quote -> BLOCK.
    t = clean_terms(expires="2026-09-01T00:00:00Z")
    v = ref.adjudicate(t, mandate, valid_approval(t), checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.BLOCK and any(f.code == FlagCode.QUOTE_EXPIRED for f in v.flags)

    # 9) Substitution -> BLOCK.
    t = clean_terms(sku="SENSOR-KIT-B")
    v = ref.adjudicate(t, mandate, valid_approval(t), checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.BLOCK and any(f.code == FlagCode.SUBSTITUTION for f in v.flags)

    # 10) Expired approval -> BLOCK.
    t = clean_terms()
    appr = valid_approval(t).model_copy(update={"expires_at": "2026-09-12T00:00:00Z"})
    v = ref.adjudicate(t, mandate, appr, checked_evidence(t), [], NOW)
    assert v.verdict == VerdictKind.BLOCK and any(f.code == FlagCode.APPROVAL_INVALID for f in v.flags)

    print("referee.py self-tests passed.")
    t = clean_terms()
    demo = ref.adjudicate(t, mandate, valid_approval(t), checked_evidence(t), [], NOW)
    print(f"  clean valid-approval verdict: {demo.verdict.value}; checks_run={demo.checks_run}")
