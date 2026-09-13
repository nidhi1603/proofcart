"""ProofCart settlement layer — crash-safe execution + recovery (README section 7).

Public API:
    Ledger                 append-only, crash-safe ledger (one row per order_id)
    settle                 persist -> create -> verify -> reconcile pipeline
    reconcile              resolve one order on restart / recovery
    verify                 check a PaymentIntent object against the mandate
    ChaosRail, FaultConfig fault injection for tests + labelled ablations only
"""
from proofcart.settlement.chaos import ChaosRail, FaultConfig
from proofcart.settlement.ledger import Ledger
from proofcart.settlement.pipeline import reconcile, settle, verify

__all__ = [
    "Ledger",
    "settle",
    "reconcile",
    "verify",
    "ChaosRail",
    "FaultConfig",
]
