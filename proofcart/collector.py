"""Collector -- adapt Slack messages into the extractor's message contract and
compute retrieval coverage.

The integration layer yields raw Slack messages ``{user, ts, thread_ts, text}``.
The extractor wants each message tagged with ``role`` (buyer vs supplier),
the thread's ``supplier_id`` / ``supplier_name``, and the ``sku`` hint. We derive
the supplier from the first non-owner participant, and the display name from the
owner's opener ("Hi Acme, ..." / "Bolt Supply -- ...") falling back to the user id.
"""
from __future__ import annotations

import re

from proofcart.schemas import Coverage, OwnerMandate, now_iso


def _name_from_user(user_id: str) -> str:
    base = re.sub(r"^U[_-]?", "", user_id or "").replace("_", " ").strip()
    return base.title() or (user_id or "Supplier")


def _name_from_opener(text: str) -> str | None:
    text = text or ""
    m = re.match(r"\s*(?:hi|hello|hey)\s+([A-Z][\w& ]+?)\s*[,\.]", text)
    if m:
        return m.group(1).strip()
    m = re.match(r"\s*([A-Z][\w& ]+?)\s*[—-]\s", text)  # "Name — ..." / "Name - ..."
    if m:
        return m.group(1).strip()
    return None


def collect(threads: list[list[dict]], mandate: OwnerMandate, owner_id: str):
    """Return ``(enriched_threads, coverage)``.

    ``owner_id`` identifies the buyer; every other participant is a supplier.
    """
    enriched: list[list[dict]] = []
    companies = 0
    for thread in threads:
        supplier_user = next((m.get("user") for m in thread if m.get("user") != owner_id), None)
        opener = next((m for m in thread if m.get("user") == owner_id), None)
        name = (_name_from_opener(opener.get("text", "")) if opener else None) or (
            _name_from_user(supplier_user) if supplier_user else "Supplier"
        )
        sup_id = "sup_" + (re.sub(r"[^a-z0-9]+", "", (supplier_user or name).lower()) or "x")
        if supplier_user is not None:
            companies += 1
        enriched.append(
            [
                {
                    "ts": m.get("ts"),
                    "text": m.get("text", ""),
                    "role": "buyer" if m.get("user") == owner_id else "supplier",
                    "user": m.get("user"),
                    "supplier_id": sup_id,
                    "supplier_name": name,
                    "sku": mandate.sku_or_spec,
                }
                for m in thread
            ]
        )
    coverage = Coverage(
        companies_seen=companies,
        threads_seen=len(threads),
        retrieval_cutoff=now_iso(),
        gaps=[],
    )
    return enriched, coverage
