"""Stable identifiers for ProofCart, plus re-exports of the hashing/time
helpers that live in the frozen schema contract.

Prefixes make ids self-describing in logs (`req_`, `ord_`, `call_`).
`new_call_id(seq)` is deterministic from a sequence number so a tool-call
log is reproducible and cheap to reference in FieldEvidence.source_ref.
"""
from __future__ import annotations

# Allow both `python3 -m proofcart.ids` and the plain-script form
# `python3 proofcart/ids.py`.
if __name__ == "__main__" and __package__ in (None, ""):  # pragma: no cover
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    __package__ = "proofcart"

import uuid

# Re-export the canonical helpers so callers depend on one place.
from .schemas import (  # noqa: F401
    canonical_json,
    idempotency_key,
    now_iso,
    terms_hash,
)


def new_request_id() -> str:
    """Logical id for one owner decision; keeps unrelated threads from mixing."""
    return "req_" + uuid.uuid4().hex[:12]


def new_order_id() -> str:
    """Unique logical order id -> one transactional ledger row."""
    return "ord_" + uuid.uuid4().hex[:12]


def new_call_id(seq: int) -> str:
    """Deterministic tool-call id from a monotonically increasing sequence."""
    return f"call_{int(seq):04d}"


__all__ = [
    "new_request_id",
    "new_order_id",
    "new_call_id",
    "now_iso",
    "terms_hash",
    "idempotency_key",
    "canonical_json",
]


if __name__ == "__main__":
    from .schemas import QuoteTerms

    rid = new_request_id()
    oid = new_order_id()
    call = new_call_id(3)
    t = QuoteTerms(
        supplier_id="S1",
        supplier_name="Acme",
        payment_recipient="acct_acme",
        quote_id="Q1",
        sku="SENSOR-KIT-A",
        product_desc="Sensor kit",
        unit_price_cents=4500,
        quantity=20,
        shipping_cents=2500,
        tax_cents=800,
    )
    th = terms_hash(t)
    ik = idempotency_key(oid, th)
    print("ProofCart ids smoke:")
    print(f"  request_id = {rid}")
    print(f"  order_id   = {oid}")
    print(f"  call_id(3) = {call}")
    print(f"  now_iso    = {now_iso()}")
    print(f"  terms_hash = {th[:16]}…  (len {len(th)})")
    print(f"  idem_key   = {ik}  (len {len(ik)})")
    assert new_call_id(3) == "call_0003"
    assert terms_hash(t) == th, "terms_hash must be deterministic"
    assert idempotency_key(oid, th) == ik, "idempotency_key must be deterministic"
    print("OK: ids + re-exported helpers are deterministic.")
