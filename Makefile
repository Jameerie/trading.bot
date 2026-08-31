.PHONY: help setup start test lint demo scan clean

PY := python3
export PYTHONPATH := src

help:
	@echo "make setup  - one-time setup: environment, token, verification"
	@echo "make start  - run the web app (after setup)"
	@echo "make test   - run the test suite"
	@echo "make lint   - byte-compile everything and check the core imports cleanly"
	@echo "make demo   - end-to-end run on the bundled sample data"
	@echo "make scan   - show what to do right now"
	@echo "make clean  - remove caches and generated reports"

setup:
	./setup.sh

start:
	./start.sh

test:
	$(PY) -m pytest tests/

lint:
	$(PY) -m compileall -q src tests
	@# The core must import with no third-party packages available. If this ever
	@# fails, a dependency crept in and the tool stopped being portable.
	$(PY) -c "import trading_bot; print('core imports clean:', trading_bot.__version__)"

demo:
	$(PY) -m trading_bot --config config/default.toml scan --no-journal
	$(PY) -m trading_bot --config config/default.toml backtest --csv data/samples/EURUSD_H1.csv --split 0.7
	$(PY) -m trading_bot --config config/default.toml calibrate --csv data/samples/EURUSD_H1.csv

scan:
	$(PY) -m trading_bot --config config/default.toml scan

clean:
	rm -rf .pytest_cache reports/*.jsonl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
