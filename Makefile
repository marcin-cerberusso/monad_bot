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

# ============================================================================
# Bot management
# ============================================================================
.PHONY: run run-bg stop logs status

run:
	.venv/bin/python3 run_agents.py

run-bg:
	mkdir -p logs
	nohup .venv/bin/python3 run_agents.py > logs/bot.log 2>&1 & echo $$! > bot.pid

stop:
	@if [ -f bot.pid ]; then kill $$(cat bot.pid) 2>/dev/null; rm -f bot.pid; echo "Bot stopped"; fi

logs:
	tail -f logs/monad_bot.log

status:
	@if [ -f bot.pid ] && kill -0 $$(cat bot.pid) 2>/dev/null; then \
		echo "✅ Bot running (PID: $$(cat bot.pid))"; \
	else \
		echo "❌ Bot not running"; rm -f bot.pid 2>/dev/null; \
	fi
