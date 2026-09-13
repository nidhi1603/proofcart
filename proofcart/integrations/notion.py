"""Notion integration: the durable comparison/decision/settlement record.

Idempotent by an ``order_id`` property: find-then-update, else create. This is
what makes record repair safe -- a retry after a successful charge updates the
SAME row rather than creating a duplicate (README §7, PAID_RECORD_PENDING).

LIVE mode uses notion-client against NOTION_DB_ID. notion_client is imported
lazily so dev runs without it.

DEV mode persists to runs/notion_dev.json.

LIVE schema assumption (other streams must honor): the target database's
*title* property is named ``order_id`` and every other field in ``fields`` maps
to a ``rich_text`` property of the same name. Values are stringified. Adjust
_to_properties/_from_page if your database uses typed properties.
"""
from __future__ import annotations

# Allow both `python3 -m proofcart.integrations.notion` and the plain-script
# form `python3 proofcart/integrations/notion.py`.
if __name__ == "__main__" and __package__ in (None, ""):  # pragma: no cover
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    __package__ = "proofcart.integrations"

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from ..config import Settings, get_settings, project_root
from ..ids import now_iso

_TITLE_PROP = "order_id"


def _stringify(v: Any) -> str:
    if isinstance(v, str):
        s = v
    elif isinstance(v, (int, float, bool)) or v is None:
        s = str(v)
    else:
        try:
            s = json.dumps(v, sort_keys=True, default=str)
        except Exception:
            s = str(v)
    return s[:1900]  # Notion hard-limits a single rich_text run to 2000 chars


class NotionClient:
    """Pass ``client`` to inject a fake/real notion Client for tests (forces the
    live code path against the injected object)."""

    def __init__(self, settings: Optional[Settings] = None, client: Any = None) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self.live = client is not None or self._settings.notion_live()
        self._dev_path: Path = project_root() / "runs" / "notion_dev.json"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def upsert_order_record(self, order_id: str, fields: dict) -> dict:
        if self.live:
            return self._upsert_live(order_id, fields)
        return self._upsert_dev(order_id, fields)

    def read_order_record(self, order_id: str) -> Optional[dict]:
        if self.live:
            return self._read_live(order_id)
        return self._read_dev(order_id)

    # ------------------------------------------------------------------ #
    # DEV json store
    # ------------------------------------------------------------------ #
    def _load_dev(self) -> dict:
        try:
            if self._dev_path.exists():
                data = json.loads(self._dev_path.read_text())
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_dev(self, store: dict) -> None:
        self._dev_path.parent.mkdir(parents=True, exist_ok=True)
        self._dev_path.write_text(json.dumps(store, indent=2, default=str))

    def _upsert_dev(self, order_id: str, fields: dict) -> dict:
        store = self._load_dev()
        now = now_iso()
        rec = store.get(order_id)
        if rec is None:
            page_id = "page_dev_" + hashlib.sha256(order_id.encode()).hexdigest()[:24]
            rec = {
                "page_id": page_id,
                "order_id": order_id,
                "fields": dict(fields or {}),
                "created_at": now,
                "updated_at": now,
            }
        else:
            merged = dict(rec.get("fields") or {})
            merged.update(fields or {})
            rec["fields"] = merged
            rec["updated_at"] = now
        store[order_id] = rec
        self._save_dev(store)
        return {"ok": True, "page_id": rec["page_id"]}

    def _read_dev(self, order_id: str) -> Optional[dict]:
        rec = self._load_dev().get(order_id)
        if rec is None:
            return None
        return {
            "order_id": rec.get("order_id", order_id),
            "page_id": rec.get("page_id"),
            "fields": dict(rec.get("fields") or {}),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
        }

    # ------------------------------------------------------------------ #
    # LIVE (notion-client)
    # ------------------------------------------------------------------ #
    def _notion(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from notion_client import Client  # type: ignore
        except ImportError as e:  # pragma: no cover - only live w/o pkg
            raise RuntimeError(
                "live mode requires the `notion-client` package (pip install notion-client)"
            ) from e
        self._client = Client(auth=self._settings.notion_token)
        return self._client

    def _to_properties(self, order_id: str, fields: dict) -> dict:
        props: dict[str, Any] = {
            _TITLE_PROP: {"title": [{"text": {"content": str(order_id)}}]}
        }
        for k, v in (fields or {}).items():
            if k == _TITLE_PROP:
                continue
            props[k] = {"rich_text": [{"text": {"content": _stringify(v)}}]}
        return props

    def _find_page_id(self, client: Any, order_id: str) -> Optional[str]:
        db = self._settings.notion_db_id
        resp = client.databases.query(
            database_id=db,
            filter={"property": _TITLE_PROP, "title": {"equals": order_id}},
        )
        results = resp.get("results", []) if isinstance(resp, dict) else getattr(resp, "results", [])
        if results:
            first = results[0]
            return first.get("id") if isinstance(first, dict) else getattr(first, "id", None)
        return None

    def _ensure_properties(self, client: Any, field_names: list) -> None:
        """Add any missing fields as rich_text columns, so a page write can't fail
        on a schema mismatch (the demo DB may start with only the order_id title)."""
        db = self._settings.notion_db_id
        try:
            info = client.databases.retrieve(database_id=db)
            existing = set((info.get("properties") or {}).keys()) if isinstance(info, dict) \
                else set(getattr(info, "properties", {}) or {})
        except Exception:
            existing = set()
        missing = {
            f: {"rich_text": {}}
            for f in field_names
            if f and f != _TITLE_PROP and f not in existing
        }
        if missing:
            client.databases.update(database_id=db, properties=missing)

    def _upsert_live(self, order_id: str, fields: dict) -> dict:
        client = self._notion()
        self._ensure_properties(client, list((fields or {}).keys()))
        props = self._to_properties(order_id, fields)
        page_id = self._find_page_id(client, order_id)
        if page_id:
            client.pages.update(page_id=page_id, properties=props)
            return {"ok": True, "page_id": page_id}
        db = self._settings.notion_db_id
        created = client.pages.create(parent={"database_id": db}, properties=props)
        pid = created.get("id") if isinstance(created, dict) else getattr(created, "id", None)
        return {"ok": True, "page_id": pid}

    def _read_live(self, order_id: str) -> Optional[dict]:
        client = self._notion()
        db = self._settings.notion_db_id
        resp = client.databases.query(
            database_id=db,
            filter={"property": _TITLE_PROP, "title": {"equals": order_id}},
        )
        results = resp.get("results", []) if isinstance(resp, dict) else getattr(resp, "results", [])
        if not results:
            return None
        page = results[0]
        page_id = page.get("id") if isinstance(page, dict) else getattr(page, "id", None)
        props = page.get("properties", {}) if isinstance(page, dict) else getattr(page, "properties", {})
        return {
            "order_id": order_id,
            "page_id": page_id,
            "fields": self._from_page(props),
        }

    @staticmethod
    def _from_page(props: dict) -> dict:
        """Best-effort flatten of Notion property objects back to plain values."""
        out: dict[str, Any] = {}
        for name, prop in (props or {}).items():
            if not isinstance(prop, dict):
                continue
            ptype = prop.get("type")
            if ptype is None:  # some payloads omit "type"; infer from keys present
                for cand in ("title", "rich_text", "number", "checkbox", "select", "date"):
                    if cand in prop:
                        ptype = cand
                        break
            if ptype in ("title", "rich_text"):
                parts = prop.get(ptype) or []
                out[name] = "".join(
                    (p.get("plain_text") or (p.get("text") or {}).get("content") or "")
                    for p in parts
                    if isinstance(p, dict)
                )
            elif ptype == "number":
                out[name] = prop.get("number")
            elif ptype == "checkbox":
                out[name] = prop.get("checkbox")
            elif ptype == "select":
                sel = prop.get("select") or {}
                out[name] = sel.get("name")
            elif ptype == "date":
                d = prop.get("date") or {}
                out[name] = d.get("start")
            else:
                out[name] = prop.get(ptype) if ptype else None
        return out


if __name__ == "__main__":
    import os
    import tempfile

    nc = NotionClient()
    assert not nc.live, "smoke expects dev mode (no keys)"
    # Isolate the smoke run from any real runs/notion_dev.json.
    nc._dev_path = Path(tempfile.gettempdir()) / f"proofcart_notion_smoke_{os.getpid()}.json"
    if nc._dev_path.exists():
        nc._dev_path.unlink()

    oid = "ord_demo123"
    r1 = nc.upsert_order_record(oid, {"status": "pending", "amount_cents": 99900})
    r2 = nc.upsert_order_record(oid, {"status": "succeeded", "payment_intent_id": "pi_dev_x"})
    assert r1["ok"] and r2["ok"]
    assert r1["page_id"] == r2["page_id"], "upsert must be idempotent by order_id"
    rec = nc.read_order_record(oid)
    assert rec is not None
    assert rec["fields"]["status"] == "succeeded", "second upsert should merge/overwrite"
    assert rec["fields"]["amount_cents"] == 99900, "first fields should persist"
    assert rec["fields"]["payment_intent_id"] == "pi_dev_x"
    assert nc.read_order_record("ord_missing") is None

    print("ProofCart notion smoke:")
    print(f"  upsert -> page_id {r1['page_id']} (stable across updates: {r1['page_id'] == r2['page_id']})")
    print(f"  read back fields: {rec['fields']}")
    print("OK: idempotent upsert-by-order_id + merge + read; json store, no network.")
    nc._dev_path.unlink(missing_ok=True)
