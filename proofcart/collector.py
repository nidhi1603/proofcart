"""Collector -- adapt Slack messages into the extractor's message contract and
compute retrieval coverage.

Two message shapes, detected per thread:

* **user-id mode** (dev, or any real multi-user thread) -- messages carry
  distinct ``user`` ids and the owner id appears; role comes from the user id and
  the supplier name from the owner's opener ("Hi Acme, ..." / "Bolt Supply -- ...").
* **prefix mode** (live) -- a bot posts every seeded message (a bot can't
  impersonate users), each prefixed with a speaker label (``*Acme:* ...`` /
  ``*Owner:* ...``); role + supplier name come from the prefix, which is stripped
  from the text handed to the extractor.
"""
from __future__ import annotations

import re

from proofcart.schemas import Coverage, OwnerMandate, now_iso

_PREFIX = re.compile(r"^\s*\*?\s*([A-Za-z][\w& ]{0,40}?)\s*:\*?\s*")
_BUYER_LABELS = {"owner", "buyer", "me", "us"}


def _name_from_user(user_id: str) -> str:
    base = re.sub(r"^U[_-]?", "", user_id or "").replace("_", " ").strip()
    return base.title() or (user_id or "Supplier")


def _name_from_opener(text: str) -> str | None:
    text = text or ""
    m = re.match(r"\s*(?:hi|hello|hey)\s+([A-Z][\w& ]+?)\s*[,\.]", text)
    if m:
        return m.group(1).strip()
    m = re.match(r"\s*([A-Z][\w& ]+?)\s*[—-]\s", text)  # "Name -- ..." / "Name - ..."
    if m:
        return m.group(1).strip()
    return None


def _prefix_speaker(text: str) -> tuple[str, str | None, str]:
    """(role, supplier_name_or_None, clean_text) from a ``*Name:*`` prefix."""
    m = _PREFIX.match(text or "")
    if not m:
        return "supplier", None, text or ""
    label = m.group(1).strip()
    clean = (text or "")[m.end():]
    if label.lower() in _BUYER_LABELS:
        return "buyer", None, clean
    return "supplier", label, clean


def collect(threads: list[list[dict]], mandate: OwnerMandate, owner_id: str):
    """Return ``(enriched_threads, coverage)``. ``owner_id`` identifies the buyer."""
    enriched: list[list[dict]] = []
    companies = 0

    for thread in threads:
        users = {m.get("user") for m in thread if m.get("user")}
        user_mode = owner_id in users and len(users) > 1

        if user_mode:
            opener = next((m for m in thread if m.get("user") == owner_id), None)
            name = _name_from_opener(opener.get("text", "")) if opener else None
            if not name:
                sup_user = next((m.get("user") for m in thread if m.get("user") != owner_id), None)
                name = _name_from_user(sup_user) if sup_user else "Supplier"
            rows = [
                ("buyer" if m.get("user") == owner_id else "supplier", m.get("text", ""), m)
                for m in thread
            ]
        else:
            parsed = [(_prefix_speaker(m.get("text", "")), m) for m in thread]
            name = next((sp[1] for (sp, _) in parsed if sp[0] == "supplier" and sp[1]), None)
            if not name:
                opener = next((m for (sp, m) in parsed if sp[0] == "buyer"), None)
                name = (_name_from_opener(opener.get("text", "")) if opener else None) or "Supplier"
            rows = [(sp[0], sp[2], m) for (sp, m) in parsed]

        sup_id = "sup_" + (re.sub(r"[^a-z0-9]+", "", name.lower()) or "x")
        if any(role == "supplier" for (role, _t, _m) in rows):
            companies += 1

        enriched.append(
            [
                {
                    "ts": m.get("ts"),
                    "text": text,
                    "role": role,
                    "user": m.get("user"),
                    "supplier_id": sup_id,
                    "supplier_name": name,
                    "sku": mandate.sku_or_spec,
                }
                for (role, text, m) in rows
            ]
        )

    coverage = Coverage(
        companies_seen=companies,
        threads_seen=len(threads),
        retrieval_cutoff=now_iso(),
        gaps=[],
    )
    return enriched, coverage
