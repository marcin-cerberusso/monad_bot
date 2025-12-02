.PHONY: check build test fmt clean compile-python

check: fmt build compile-python
	@echo "✅ All checks passed"

fmt:
	cargo fmt --all

build:
	cargo build --release

test:
	cargo test

compile-python:
	python -m compileall agent_swarm file_utils.py

clean:
	cargo clean
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -delete
