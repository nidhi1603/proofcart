PY := python3

.PHONY: help demo demo-live seed evals replay test smoke
help:
	@echo "ProofCart make targets:"
	@echo "  make demo       - full flow in DEV mode (no keys)"
	@echo "  make demo-live  - full flow against the 3 real apps (.env required)"
	@echo "  make seed       - post the seeded supplier threads into the live Slack channel"
	@echo "  make evals      - run the scenario suite + NaiveCart baseline -> reports/scoreboard.md"
	@echo "  make replay     - re-score the referee offline (determinism check)"
	@echo "  make test       - module self-tests"
	@echo "  make smoke      - import check"

demo:
	PROOFCART_MODE=dev $(PY) scripts/demo.py

demo-live:
	PROOFCART_MODE=live $(PY) scripts/demo.py

seed:
	PROOFCART_MODE=live $(PY) scripts/seed_slack.py

evals:
	$(PY) -m evals.runner

replay:
	$(PY) -m evals.replay $(RUN)

test:
	PROOFCART_MODE=dev $(PY) -m proofcart.referee
	PROOFCART_MODE=dev $(PY) -m proofcart.settlement.pipeline
	@echo "reliability regression: run 'make evals' (14-scenario scoreboard)"

smoke:
	$(PY) -c "import proofcart.engine, proofcart.referee, proofcart.settlement; print('imports ok')"
