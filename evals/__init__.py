"""ProofCart evaluation suite.

Runs scenario fixtures through the ProofCart engine (dev mode, no keys) and a
fair component-removal baseline (``NaiveCart``), scores each run against declared
expectations, and writes a side-by-side scoreboard.

Entry point::

    python3 -m evals.runner

Everything here is honest-by-construction: assertions read the *resulting*
``RunRecord`` and the actual payment-rail charges (the arbiter of how many
charges occurred), never a hardcoded "the baseline must fail X".
"""
