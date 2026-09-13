"""Append-only, crash-safe ledger for the settlement layer.

One transactional row per logical ``order_id`` (unique key), so concurrent
requests for the same order can't both execute. The row is persisted to an
append-only JSONL file *before* any charge is attempted (write-ahead), and every
mutation is flushed + fsynced immediately so an interrupted process can be
reconciled from durable state on restart.

Storage format: one JSON object per line, each line the *full* current state of
an entry. On load we replay the file and the last line per ``order_id`` wins, so
the file is an append-only log whose folded projection is the current ledger.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from proofcart.schemas import (
    LedgerEntry,
    PaymentMandate,
    SettleStatus,
    now_iso,
)

DEFAULT_LEDGER_PATH = "runs/ledger.jsonl"


class Ledger:
    """Durable, append-only ledger keyed by unique ``order_id``.

    Every mutation persists immediately (append + flush + fsync) and bumps
    ``updated_at``. In-memory the latest state per order is authoritative; the
    JSONL file is the durable log it is rebuilt from.
    """

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_LEDGER_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: dict[str, LedgerEntry] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Load / persist                                                     #
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        """Rebuild the folded projection from the append-only log."""
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = LedgerEntry.model_validate_json(line)
                except Exception:
                    # A torn final line from a crash mid-write: ignore it; the
                    # prior complete line for that order remains authoritative.
                    continue
                self._entries[entry.order_id] = entry

    def _persist(self, entry: LedgerEntry) -> None:
        """Append the entry's full state and force it to stable storage."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.model_dump(mode="json"), separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ------------------------------------------------------------------ #
    # Mutations                                                          #
    # ------------------------------------------------------------------ #
    def write_ahead(self, pm: PaymentMandate) -> LedgerEntry:
        """Ensure a PENDING transactional row exists for ``pm.order_id``.

        If the row already exists it is returned unchanged (write-ahead is a
        no-op for an already-logged order) so a persisted ``payment_intent_id``
        and ``created_at`` are never clobbered on re-entry / restart. A row that
        exists under a *different* idempotency key is a changed deal reusing an
        order id — that must get a fresh order id, so we refuse it.
        """
        with self._lock:
            existing = self._entries.get(pm.order_id)
            if existing is not None:
                if existing.idempotency_key != pm.idempotency_key:
                    raise ValueError(
                        f"order_id {pm.order_id!r} already logged under a "
                        f"different idempotency_key (terms changed -> new order "
                        f"id required)"
                    )
                return existing
            ts = now_iso()
            entry = LedgerEntry(
                order_id=pm.order_id,
                idempotency_key=pm.idempotency_key,
                amount_cents=pm.amount_cents,
                currency=pm.currency,
                payment_intent_id=None,
                status=SettleStatus.PENDING,
                reason=None,
                created_at=ts,
                updated_at=ts,
            )
            self._entries[pm.order_id] = entry
            self._persist(entry)
            return entry

    def set_payment_intent_id(self, order_id: str, pid: str) -> LedgerEntry:
        """Persist the PaymentIntent id (called the instant a create returns one).

        If a *different* pid is already recorded, that would mean a second,
        distinct charge — the exact thing this layer exists to prevent — so we
        refuse. Re-recording the same pid is an idempotent no-op (still bumps
        ``updated_at`` and re-persists for durability).
        """
        with self._lock:
            entry = self._entries.get(order_id)
            if entry is None:
                raise KeyError(f"no ledger row for order_id {order_id!r}")
            if entry.payment_intent_id and entry.payment_intent_id != pid:
                raise ValueError(
                    f"order_id {order_id!r} already bound to payment_intent "
                    f"{entry.payment_intent_id!r}; refusing to rebind to {pid!r} "
                    f"(would imply a second charge)"
                )
            entry.payment_intent_id = pid
            entry.updated_at = now_iso()
            self._persist(entry)
            return entry

    def mark(
        self,
        order_id: str,
        status: SettleStatus,
        reason: Optional[str] = None,
    ) -> LedgerEntry:
        """Set the terminal/interim status (+ optional reason) and persist."""
        with self._lock:
            entry = self._entries.get(order_id)
            if entry is None:
                raise KeyError(f"no ledger row for order_id {order_id!r}")
            entry.status = status
            entry.reason = reason
            entry.updated_at = now_iso()
            self._persist(entry)
            return entry

    # ------------------------------------------------------------------ #
    # Reads                                                              #
    # ------------------------------------------------------------------ #
    def get(self, order_id: str) -> Optional[LedgerEntry]:
        with self._lock:
            return self._entries.get(order_id)

    def by_idempotency(self, key: str) -> Optional[LedgerEntry]:
        with self._lock:
            for entry in self._entries.values():
                if entry.idempotency_key == key:
                    return entry
            return None

    def all(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries.values())
