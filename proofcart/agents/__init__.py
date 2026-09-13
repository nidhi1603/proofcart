"""ProofCart agents.

The only components in ProofCart that are allowed to read supplier natural
language. Everything downstream (comparator, evidence, referee) operates on the
typed `QuoteTerms` these agents emit -- so the deterministic gate never depends
on trusting supplier prose.
"""

from proofcart.agents.extractor import extract_offers

__all__ = ["extract_offers"]
