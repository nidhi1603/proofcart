"""Evidence Checker -- field -> source support, freshness, contradiction.

Assigns each load-bearing field of a reconstructed offer an ``EvidenceLabel``:

* ``SYSTEM_CHECKED``  -- ONLY when a cited tool call in the log matches the SAME
  supplier AND the SAME product AND a fresh-enough timestamp AND the returned
  value equals the claimed value. The cited call's args are recorded in
  ``checked_args`` so the reference can actually be validated. A bare
  ``call_id`` existing is NOT sufficient.
* ``SUPPLIER_STATED`` -- the supplier asserted it; no matching trusted call.
* ``UNRESOLVED``      -- missing, stale, or contradicted by a trusted call.

Labels describe *support*, not truth or probability. No invented percentages.

Tool-log entry contract (flexible; each ``dict`` in ``tool_log``)::

    {
      "call_id":     "call_123",
      "tool":        "verify_price",
      "supplier_id": "sup_acme",              # must match the offer's supplier
      "sku":         "SENSOR-KIT-A",          # or "product"; must match the offer
      "field":       "unit_price_cents",      # which field this call checked
      "value":       4500,                    # OR "result": {"unit_price_cents": 4500}
      "ts":          "2026-09-13T10:00:00Z",  # freshness
      "trusted":     true,                    # default True; a supplier-run call is not trusted
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

try:
    from proofcart.schemas import (
        EvidenceLabel,
        FieldEvidence,
        QuoteTerms,
        ReconstructedOffer,
        now_iso,
    )
except ModuleNotFoundError:  # pragma: no cover - path bootstrap
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from proofcart.schemas import (
        EvidenceLabel,
        FieldEvidence,
        QuoteTerms,
        ReconstructedOffer,
        now_iso,
    )

# Fields whose support actually matters for the deal / eligibility.
LOAD_BEARING_FIELDS: tuple[str, ...] = (
    "unit_price_cents",
    "quantity",
    "shipping_cents",
    "tax_cents",
    "discount_cents",
    "delivery_by",
    "sku",
)

# A cited tool call older than this (relative to `now`) is stale -> cannot verify.
FRESHNESS_MAX_AGE_SECONDS: int = 30 * 24 * 3600  # 30 days

_LABEL_RANK = {
    EvidenceLabel.UNRESOLVED: 0,
    EvidenceLabel.SUPPLIER_STATED: 1,
    EvidenceLabel.SYSTEM_CHECKED: 2,
}


def _parse_iso(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    txt = str(s).strip()
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _entry_supplier(entry: dict) -> Optional[str]:
    return entry.get("supplier_id") or entry.get("supplier")


def _entry_product(entry: dict) -> Optional[str]:
    return entry.get("sku") or entry.get("product") or entry.get("product_sku")


def _entry_returned_value(entry: dict, field: str) -> Any:
    """The value the trusted call actually returned for ``field``."""
    if "value" in entry:
        return entry["value"]
    for key in ("result", "returned", "return", "response", "output"):
        blob = entry.get(key)
        if isinstance(blob, dict):
            if field in blob:
                return blob[field]
            if entry.get("field") == field and "value" in blob:
                return blob["value"]
    return None


def _values_match(claimed: Any, returned: Any) -> bool:
    if claimed is None or returned is None:
        return False
    # Numeric comparison (money / quantity) tolerant of int/float/str.
    try:
        return int(round(float(claimed))) == int(round(float(returned)))
    except (TypeError, ValueError):
        pass
    # Timestamp comparison.
    cd, rd = _parse_iso(claimed), _parse_iso(returned)
    if cd and rd:
        return cd == rd
    return str(claimed).strip().casefold() == str(returned).strip().casefold()


def _match_for_field(
    offer: ReconstructedOffer,
    field: str,
    claimed: Any,
    tool_log: list[dict],
    now_dt: datetime,
) -> tuple[EvidenceLabel, Optional[dict], Optional[str], Optional[str]]:
    """Return (label, checked_args, source_ref, note) for a single field by
    scanning the tool log. A call must match supplier + product + freshness +
    value before it can upgrade the label to SYSTEM_CHECKED."""
    best_note: Optional[str] = None
    for entry in tool_log:
        if not isinstance(entry, dict):
            continue
        if (entry.get("field") or _field_from_tool(entry)) != field:
            continue
        # Same supplier?
        if _entry_supplier(entry) != offer.supplier_id:
            best_note = best_note or "cited call is for a different supplier"
            continue
        # Same product?
        if _entry_product(entry) not in (None, offer.current.sku):
            best_note = best_note or "cited call is for a different product"
            continue
        # Trusted source? (a supplier-run 'check' is not independent)
        if entry.get("trusted") is False:
            best_note = best_note or "cited call is not from a trusted source"
            continue
        # Fresh enough?
        ts = _parse_iso(entry.get("ts"))
        if ts is None:
            best_note = best_note or "cited call has no usable timestamp"
            continue
        age = (now_dt - ts).total_seconds()
        if age > FRESHNESS_MAX_AGE_SECONDS:
            best_note = "cited call is stale"
            continue
        # Value equals claim?
        returned = _entry_returned_value(entry, field)
        checked_args = {
            "call_id": entry.get("call_id"),
            "tool": entry.get("tool"),
            "supplier_id": _entry_supplier(entry),
            "sku": _entry_product(entry),
            "ts": entry.get("ts"),
            "returned_value": returned,
        }
        if _values_match(claimed, returned):
            return EvidenceLabel.SYSTEM_CHECKED, checked_args, entry.get("call_id"), None
        # Trusted call contradicts the claim -> unresolved (a real red flag).
        return (
            EvidenceLabel.UNRESOLVED,
            checked_args,
            entry.get("call_id"),
            f"trusted call returned {returned!r}, claim is {claimed!r}",
        )
    return EvidenceLabel.SUPPLIER_STATED, None, None, best_note


def _field_from_tool(entry: dict) -> Optional[str]:
    """Best-effort field inference from the tool name when 'field' is absent."""
    tool = (entry.get("tool") or "").lower()
    mapping = {
        "price": "unit_price_cents",
        "unit_price": "unit_price_cents",
        "shipping": "shipping_cents",
        "tax": "tax_cents",
        "discount": "discount_cents",
        "delivery": "delivery_by",
        "lead_time": "delivery_by",
        "inventory": "quantity",
        "stock": "quantity",
        "sku": "sku",
        "catalog": "sku",
    }
    for token, field in mapping.items():
        if token in tool:
            return field
    return None


def assign_evidence(
    offer: ReconstructedOffer, tool_log: list[dict]
) -> list[FieldEvidence]:
    """Label every load-bearing field of ``offer.current`` against ``tool_log``."""
    now_dt = _parse_iso(now_iso()) or datetime.now(timezone.utc)
    tool_log = tool_log or []

    # Fields the extractor already flagged unresolved stay unresolved unless a
    # trusted call actually supplies a matching value.
    extractor_unresolved = set(offer.unresolved_fields or [])

    out: list[FieldEvidence] = []
    for field in LOAD_BEARING_FIELDS:
        claimed = getattr(offer.current, field, None)
        label, checked_args, source_ref, note = _match_for_field(
            offer, field, claimed, tool_log, now_dt
        )

        if field in extractor_unresolved and label != EvidenceLabel.SYSTEM_CHECKED:
            # Missing/conflicting per the extractor, and no trusted call rescued it.
            out.append(
                FieldEvidence(
                    field=field,
                    value=None,
                    label=EvidenceLabel.UNRESOLVED,
                    source_ref=source_ref,
                    checked_args=checked_args,
                    note=note or "unresolved in source thread",
                )
            )
            continue

        if claimed is None and label != EvidenceLabel.SYSTEM_CHECKED:
            out.append(
                FieldEvidence(
                    field=field,
                    value=None,
                    label=EvidenceLabel.UNRESOLVED,
                    source_ref=source_ref,
                    checked_args=checked_args,
                    note=note or "no value stated",
                )
            )
            continue

        out.append(
            FieldEvidence(
                field=field,
                value=(claimed if label != EvidenceLabel.UNRESOLVED else None),
                label=label,
                source_ref=source_ref,
                checked_args=checked_args,
                note=note,
            )
        )
    return out


def overall_label(evidence: list[FieldEvidence]) -> EvidenceLabel:
    """Weakest-link aggregate over the field labels.

    UNRESOLVED beats SUPPLIER_STATED beats SYSTEM_CHECKED -- so a shortlist item
    is only labelled ``system_checked`` when every load-bearing field is."""
    if not evidence:
        return EvidenceLabel.UNRESOLVED
    return min(evidence, key=lambda e: _LABEL_RANK[e.label]).label


# --------------------------------------------------------------------------- #
# Self-tests                                                                   #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from proofcart.schemas import NegotiationStatus, now_iso

    def _offer(unit=4500, ship=0, deliver="2026-09-16T17:00:00Z", unresolved=None) -> ReconstructedOffer:
        t = QuoteTerms(
            supplier_id="sup_acme",
            supplier_name="Acme Sensors",
            payment_recipient="Acme Sensors",
            quote_id="sup_acme-q1",
            sku="SENSOR-KIT-A",
            product_desc="SENSOR-KIT-A",
            unit_price_cents=unit,
            quantity=20,
            shipping_cents=ship,
            delivery_by=deliver,
        )
        return ReconstructedOffer(
            supplier_id="sup_acme",
            supplier_name="Acme Sensors",
            current=t,
            status=NegotiationStatus.SUPPLIER_OFFER,
            unresolved_fields=unresolved or [],
        )

    fresh = now_iso()

    # 1) A matching trusted call -> SYSTEM_CHECKED with checked_args recorded.
    ev = assign_evidence(
        _offer(),
        [{"call_id": "c1", "tool": "verify_price", "supplier_id": "sup_acme",
          "sku": "SENSOR-KIT-A", "field": "unit_price_cents", "value": 4500, "ts": fresh}],
    )
    price = next(e for e in ev if e.field == "unit_price_cents")
    assert price.label == EvidenceLabel.SYSTEM_CHECKED, price
    assert price.checked_args and price.checked_args["call_id"] == "c1"

    # 2) A bare call_id with the WRONG supplier does NOT verify.
    ev = assign_evidence(
        _offer(),
        [{"call_id": "c2", "tool": "verify_price", "supplier_id": "sup_other",
          "sku": "SENSOR-KIT-A", "field": "unit_price_cents", "value": 4500, "ts": fresh}],
    )
    price = next(e for e in ev if e.field == "unit_price_cents")
    assert price.label == EvidenceLabel.SUPPLIER_STATED, price

    # 3) A trusted call that CONTRADICTS the claim -> UNRESOLVED.
    ev = assign_evidence(
        _offer(unit=4500),
        [{"call_id": "c3", "tool": "verify_price", "supplier_id": "sup_acme",
          "sku": "SENSOR-KIT-A", "field": "unit_price_cents", "value": 9900, "ts": fresh}],
    )
    price = next(e for e in ev if e.field == "unit_price_cents")
    assert price.label == EvidenceLabel.UNRESOLVED, price

    # 4) Stale call -> falls back to SUPPLIER_STATED.
    ev = assign_evidence(
        _offer(),
        [{"call_id": "c4", "tool": "verify_price", "supplier_id": "sup_acme",
          "sku": "SENSOR-KIT-A", "field": "unit_price_cents", "value": 4500,
          "ts": "2020-01-01T00:00:00Z"}],
    )
    price = next(e for e in ev if e.field == "unit_price_cents")
    assert price.label == EvidenceLabel.SUPPLIER_STATED, price

    # 5) Extractor-unresolved shipping stays UNRESOLVED with no trusted call.
    ev = assign_evidence(_offer(unresolved=["shipping_cents"]), [])
    ship = next(e for e in ev if e.field == "shipping_cents")
    assert ship.label == EvidenceLabel.UNRESOLVED, ship

    # 6) overall_label is the weakest link.
    assert overall_label(ev) == EvidenceLabel.UNRESOLVED
    all_checked = assign_evidence(
        _offer(),
        [
            {"tool": "verify_price", "supplier_id": "sup_acme", "sku": "SENSOR-KIT-A",
             "field": f, "value": getattr(_offer().current, f), "ts": fresh}
            for f in LOAD_BEARING_FIELDS
        ],
    )
    assert overall_label(all_checked) == EvidenceLabel.SYSTEM_CHECKED, [e.label for e in all_checked]

    print("evidence.py self-tests passed.")
    for e in ev:
        print(f"  {e.field:18s} {e.label.value:15s} note={e.note}")
