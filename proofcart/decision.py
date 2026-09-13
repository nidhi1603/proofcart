"""Decision service.

Turns an *authenticated* owner selection into an `Approval` bound to the exact
quote (id + version + terms hash + recipient + amount + expiry), then into a
`PaymentMandate`. Identity is verified here; the Referee independently re-checks
identity, expiry, and terms-hash before any payment. A selection is a preference;
only `build_approval` (with the right owner) authorizes spending.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from proofcart.schemas import (
    Approval,
    OwnerMandate,
    PaymentMandate,
    QuoteTerms,
    idempotency_key,
    terms_hash,
)


class ApprovalError(Exception):
    """Raised when a non-owner identity attempts to approve, or terms are unusable."""


def build_approval(
    mandate: OwnerMandate,
    terms: QuoteTerms,
    approver_id: str,
    ttl_seconds: int = 600,
) -> Approval:
    """Create an owner-authorized approval bound to *these exact* terms.

    Rejects any approver that is not the mandate's configured owner. A supplier
    message, an LLM recommendation, or a generic reaction can never reach here
    with the right ``approver_id``.
    """
    if approver_id != mandate.approver_id:
        raise ApprovalError(
            f"approver {approver_id!r} is not the authorized owner "
            f"({mandate.approver_id!r}); no payment authorized"
        )
    now = datetime.now(timezone.utc)
    return Approval(
        request_id=mandate.request_id,
        approver_id=approver_id,
        supplier_id=terms.supplier_id,
        quote_id=terms.quote_id,
        quote_version=terms.quote_version,
        payment_recipient=terms.payment_recipient,
        amount_cents=terms.total_cents,
        terms_hash=terms_hash(terms),
        approved_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
    )


def payment_mandate_from_approval(
    order_id: str,
    mandate: OwnerMandate,
    terms: QuoteTerms,
    approval: Approval,
) -> PaymentMandate:
    """Build the `PaymentMandate` the settlement pipeline executes.

    The idempotency key is derived from ``order_id`` + the terms hash, so a
    retry of the same approved deal is idempotent and a changed deal is a
    different key (and would fail the Referee's terms-hash check first).
    """
    th = approval.terms_hash
    return PaymentMandate(
        order_id=order_id,
        request_id=mandate.request_id,
        quote_id=terms.quote_id,
        quote_version=terms.quote_version,
        payment_recipient=terms.payment_recipient,
        amount_cents=terms.total_cents,
        idempotency_key=idempotency_key(order_id, th),
        terms_hash=th,
        authorized_by=approval.approver_id,
    )


if __name__ == "__main__":  # dev smoke
    t = QuoteTerms(
        supplier_id="acme",
        supplier_name="Acme",
        payment_recipient="acct_acme",
        quote_id="ACME-Q2",
        quote_version=2,
        sku="SENSOR-KIT-A",
        product_desc="Sensor Kit A",
        unit_price_cents=4500,
        quantity=20,
        shipping_cents=3000,
        tax_cents=3600,
        delivery_by="2026-09-16T00:00:00Z",
    )
    m = OwnerMandate(
        request_id="req_demo",
        approver_id="U_OWNER",
        sku_or_spec="SENSOR-KIT-A",
        quantity=20,
        budget_cents=100000,
        must_haves={"delivery_by": "2026-09-18T17:00:00Z"},
    )
    ap = build_approval(m, t, "U_OWNER")
    assert ap.amount_cents == 96600, ap.amount_cents
    pm = payment_mandate_from_approval("ord_x", m, t, ap)
    assert pm.amount_cents == 96600 and pm.terms_hash == ap.terms_hash
    try:
        build_approval(m, t, "U_INTRUDER")
        raise SystemExit("FAIL: non-owner approval should have raised")
    except ApprovalError:
        pass
    print("decision.py dev smoke OK: approval bound, amount", ap.amount_cents, "non-owner rejected")
