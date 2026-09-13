"""Comparator -- hard eligibility rules (before ranking), then ranking.

Three separate concerns, kept separate (see README §5):

* **Eligibility** -- hard rules applied BEFORE ranking: exact product/spec,
  quantity, currency, all-in budget (``total_cents`` already includes shipping +
  tax - discount), deadline, no-substitution, no-partial. A known violation ->
  ineligible; a missing critical field -> ``pending_clarification`` (not
  eligible, but not a hard violation either -- it is not purchase-ready).
* **Ranking** -- order eligible offers by ``mandate.ranking_priorities``.
* **Evidence quality** -- carried through from the offer (see ``evidence.py``);
  ranking never uses it as a tiebreaker silently.

Ranking is not authorization.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

try:
    from proofcart.schemas import (
        Comparison,
        Coverage,
        Eligibility,
        EvidenceLabel,
        NegotiationStatus,
        OwnerMandate,
        QuoteTerms,
        ReconstructedOffer,
        ShortlistItem,
    )
    from proofcart.evidence import overall_label
except ModuleNotFoundError:  # pragma: no cover - path bootstrap
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from proofcart.schemas import (
        Comparison,
        Coverage,
        Eligibility,
        EvidenceLabel,
        NegotiationStatus,
        OwnerMandate,
        QuoteTerms,
        ReconstructedOffer,
        ShortlistItem,
    )
    from proofcart.evidence import overall_label

# Critical fields whose absence makes an offer not purchase-ready (pending).
_CRITICAL_FIELDS = ("unit_price_cents", "shipping_cents", "delivery_by")

_MAX_SHORTLIST = 5


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


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _missing_critical(offer: ReconstructedOffer) -> list[str]:
    """Critical fields that are unresolved / unknown for this offer."""
    missing: list[str] = []
    unresolved = set(offer.unresolved_fields or [])
    t = offer.current
    if "unit_price_cents" in unresolved or t.unit_price_cents <= 0:
        missing.append("unit_price_cents")
    if "shipping_cents" in unresolved:
        missing.append("shipping_cents")
    if "delivery_by" in unresolved or t.delivery_by is None:
        missing.append("delivery_by")
    return missing


# --------------------------------------------------------------------------- #
# Eligibility                                                                  #
# --------------------------------------------------------------------------- #
def check_eligibility(offer: ReconstructedOffer, mandate: OwnerMandate) -> Eligibility:
    """Apply the owner's hard requirements. Runs BEFORE any ranking."""
    t = offer.current
    violations: list[str] = []
    reasons: list[str] = []
    pending = False

    # A buyer counteroffer the supplier never accepted is not a real offer.
    if offer.status == NegotiationStatus.BUYER_COUNTER:
        pending = True
        reasons.append("only an unaccepted buyer counteroffer exists; no confirmed supplier price")

    # Exact product / spec (+ explicit no-substitution).
    if t.sku != mandate.sku_or_spec:
        if mandate.must_haves.get("no_substitution"):
            violations.append(
                f"substitution: offered {t.sku!r}, owner requires {mandate.sku_or_spec!r} (no substitutions)"
            )
        else:
            violations.append(f"wrong product: offered {t.sku!r}, owner requires {mandate.sku_or_spec!r}")

    # Exact quantity (+ no partial orders).
    if t.quantity != mandate.quantity:
        if mandate.must_haves.get("no_partial") and t.quantity < mandate.quantity:
            violations.append(
                f"partial order: offered {t.quantity}, owner requires {mandate.quantity} (no partial)"
            )
        else:
            violations.append(f"wrong quantity: offered {t.quantity}, owner requires {mandate.quantity}")

    # Currency.
    if t.currency != "USD":
        violations.append(f"wrong currency: {t.currency} (owner budget is USD)")

    # Missing critical fields -> pending clarification (not a hard violation).
    missing = _missing_critical(offer)

    # All-in budget. Even with an unknown field, if the *known* lower bound
    # already exceeds the cap it is a hard violation.
    if t.total_cents > mandate.budget_cents:
        violations.append(
            f"over budget: all-in {_dollars(t.total_cents)} > cap {_dollars(mandate.budget_cents)}"
        )
    elif "shipping_cents" in missing or "unit_price_cents" in missing:
        pending = True

    # Deadline.
    deadline = mandate.must_haves.get("delivery_by")
    if deadline is not None:
        if t.delivery_by is None or "delivery_by" in missing:
            pending = True
            reasons.append("delivery date not stated; needs clarification")
        else:
            d_offer, d_req = _parse_iso(t.delivery_by), _parse_iso(deadline)
            if d_offer and d_req and d_offer > d_req:
                violations.append(
                    f"delivery {t.delivery_by} is after the deadline {deadline}"
                )
            elif d_offer is None:
                pending = True
                reasons.append("delivery date unparseable; needs clarification")

    if missing:
        pending = True
        reasons.append("missing critical field(s): " + ", ".join(sorted(set(missing))))

    eligible = not violations and not pending
    if eligible:
        reasons.append("meets all hard requirements")
    return Eligibility(
        eligible=eligible,
        pending_clarification=pending and not violations,
        violations=violations,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Ranking                                                                      #
# --------------------------------------------------------------------------- #
def _rank_key(offer: ReconstructedOffer, mandate: OwnerMandate) -> tuple:
    t = offer.current
    key: list[Any] = []
    for pri in mandate.ranking_priorities or ["total", "delivery"]:
        p = pri.strip().lower()
        if p in ("total", "total_cents", "all_in", "price_total"):
            key.append(t.total_cents)
        elif p in ("delivery", "delivery_by", "deadline", "speed"):
            d = _parse_iso(t.delivery_by)
            key.append(d.timestamp() if d else float("inf"))
        elif p in ("unit_price", "unit", "price"):
            key.append(t.unit_price_cents)
        elif p in ("target", "target_cents") and mandate.target_cents is not None:
            key.append(abs(t.total_cents - mandate.target_cents))
        else:
            key.append(0)
    # Final deterministic tiebreaker.
    key.append(t.supplier_id)
    return tuple(key)


def _why(offer: ReconstructedOffer, rank: int, mandate: OwnerMandate, eq: EvidenceLabel) -> str:
    t = offer.current
    lead = "lowest confirmed total" if rank == 1 else f"#{rank} by {', '.join(mandate.ranking_priorities)}"
    bits = [f"{lead}: all-in {_dollars(t.total_cents)}"]
    if t.delivery_by:
        bits.append(f"delivery by {t.delivery_by}")
    if eq != EvidenceLabel.SYSTEM_CHECKED:
        bits.append(f"evidence: {eq.value}")
    return "; ".join(bits)


def _tradeoffs(offer: ReconstructedOffer) -> list[str]:
    t = offer.current
    out: list[str] = []
    if t.return_policy:
        out.append(f"return policy: {t.return_policy}")
    if t.shipping_cents:
        out.append(f"shipping {_dollars(t.shipping_cents)} of the all-in total")
    if t.expires_at:
        out.append(f"quote expires {t.expires_at}")
    if offer.unresolved_fields:
        out.append("unresolved: " + ", ".join(offer.unresolved_fields))
    return out


def build_comparison(
    offers: list[ReconstructedOffer],
    mandate: OwnerMandate,
    coverage: Coverage,
) -> Comparison:
    """Eligibility gate -> rank eligible offers -> up to five, with excluded
    offers carrying their reason. Explanations are tied to facts."""
    eligible: list[tuple[ReconstructedOffer, Eligibility]] = []
    excluded_items: list[ShortlistItem] = []

    for offer in offers:
        elig = check_eligibility(offer, mandate)
        eq = overall_label(offer.evidence)
        if elig.eligible:
            eligible.append((offer, elig))
        else:
            reason = "; ".join(elig.violations) if elig.violations else "; ".join(elig.reasons)
            excluded_items.append(
                ShortlistItem(
                    rank=0,
                    offer=offer,
                    eligibility=elig,
                    why=reason or "ineligible",
                    tradeoffs=_tradeoffs(offer),
                    evidence_quality=eq,
                )
            )

    eligible.sort(key=lambda pair: _rank_key(pair[0], mandate))

    shortlist: list[ShortlistItem] = []
    for i, (offer, elig) in enumerate(eligible[:_MAX_SHORTLIST], start=1):
        eq = overall_label(offer.evidence)
        shortlist.append(
            ShortlistItem(
                rank=i,
                offer=offer,
                eligibility=elig,
                why=_why(offer, i, mandate, eq),
                tradeoffs=_tradeoffs(offer),
                evidence_quality=eq,
            )
        )

    # Eligible offers beyond the top five are still recorded as excluded (with a
    # reason), so the full comparison is preserved.
    for offer, elig in eligible[_MAX_SHORTLIST:]:
        excluded_items.append(
            ShortlistItem(
                rank=0,
                offer=offer,
                eligibility=elig,
                why="eligible but outside the top 5",
                tradeoffs=_tradeoffs(offer),
                evidence_quality=overall_label(offer.evidence),
            )
        )

    return Comparison(
        request_id=mandate.request_id,
        shortlist=shortlist,
        excluded=excluded_items,
        coverage=coverage,
        recommendation_rank=1 if shortlist else None,
    )


# --------------------------------------------------------------------------- #
# Self-tests                                                                   #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    def _offer(sid, name, unit, qty=20, ship=0, tax=0, deliver="2026-09-16T17:00:00Z",
               sku="SENSOR-KIT-A", status=NegotiationStatus.SUPPLIER_OFFER,
               unresolved=None) -> ReconstructedOffer:
        t = QuoteTerms(
            supplier_id=sid, supplier_name=name, payment_recipient=name,
            quote_id=f"{sid}-q1", sku=sku, product_desc=sku,
            unit_price_cents=unit, quantity=qty, shipping_cents=ship, tax_cents=tax,
            delivery_by=deliver,
        )
        return ReconstructedOffer(
            supplier_id=sid, supplier_name=name, current=t, status=status,
            unresolved_fields=unresolved or [],
        )

    mandate = OwnerMandate(
        request_id="req-1", approver_id="U_OWNER", sku_or_spec="SENSOR-KIT-A",
        quantity=20, budget_cents=100_000,
        must_haves={"delivery_by": "2026-09-18T17:00:00Z", "no_substitution": True, "no_partial": True},
        ranking_priorities=["total", "delivery"],
    )
    coverage = Coverage(companies_seen=6, threads_seen=6, retrieval_cutoff="2026-09-13T12:00:00Z")

    cheap_ok = _offer("sup_a", "Acme", 4500, deliver="2026-09-17T17:00:00Z")          # $900 total
    mid_ok = _offer("sup_b", "Beta", 4700, ship=1000)                                  # $950 total
    over = _offer("sup_c", "Cost", 5200)                                               # $1040 > cap
    late = _offer("sup_d", "Late", 4000, deliver="2026-09-20T17:00:00Z")               # misses deadline
    missing_ship = _offer("sup_e", "Miss", 4200, unresolved=["shipping_cents"])        # pending
    wrong_sku = _offer("sup_f", "Sub", 3000, sku="SENSOR-KIT-B")                       # substitution
    buyer_only = _offer("sup_g", "Buy", 4000, status=NegotiationStatus.BUYER_COUNTER)  # not a real offer

    # Eligibility spot checks.
    assert check_eligibility(cheap_ok, mandate).eligible
    assert not check_eligibility(over, mandate).eligible
    assert any("over budget" in v for v in check_eligibility(over, mandate).violations)
    assert any("after the deadline" in v for v in check_eligibility(late, mandate).violations)
    e_miss = check_eligibility(missing_ship, mandate)
    assert e_miss.pending_clarification and not e_miss.eligible and not e_miss.violations
    assert any("substitution" in v for v in check_eligibility(wrong_sku, mandate).violations)
    assert check_eligibility(buyer_only, mandate).pending_clarification

    comp = build_comparison(
        [over, mid_ok, cheap_ok, late, missing_ship, wrong_sku, buyer_only], mandate, coverage
    )
    ranks = [(s.rank, s.offer.supplier_id, s.offer.current.total_cents) for s in comp.shortlist]
    assert [r[1] for r in ranks] == ["sup_a", "sup_b"], ranks  # total-then-delivery order
    assert comp.recommendation_rank == 1
    excluded_ids = {s.offer.supplier_id for s in comp.excluded}
    assert {"sup_c", "sup_d", "sup_e", "sup_f", "sup_g"} <= excluded_ids, excluded_ids

    print("comparator.py self-tests passed.")
    print("  shortlist:", ranks)
    for s in comp.excluded:
        print(f"  excluded {s.offer.supplier_id}: {s.why}")
