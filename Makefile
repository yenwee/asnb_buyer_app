.PHONY: help setup run gui stop status logs tail clean

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## First time setup (venv + deps + config)
	python -m venv .venv
	.venv/bin/pip install -r requirements.txt
	@[ -f config.ini ] || cp config.ini.template config.ini
	@echo "Setup complete. Edit config.ini to add your [Profile.xxx] section, then run 'make gui' or 'make run P=xxx'."

run: ## Run automation (usage: make run P=<profile>)
	@test -d .venv || { echo "Error: Run 'make setup' first."; exit 1; }
	@test -f config.ini || { echo "Error: config.ini not found. Run 'make setup' then edit config.ini."; exit 1; }
ifeq ($(P),)
	@echo "Usage: make run P=<profile>"
	@echo ""
	@echo "Available profiles:"
	@.venv/bin/python -c "from asnb.config import load_config, get_profiles; \
		profiles = get_profiles(load_config()); \
		[print(f'  {k:15s} ({v.get(\"username\",\"?\")})') for k,v in profiles.items()]" \
		2>/dev/null || echo "  (none found - add [Profile.xxx] to config.ini)"
	@echo ""
	@echo "Example: make run P=yenwee"
else
	.venv/bin/python -m asnb.main --profile $(P)
endif

gui: ## Launch GUI (multi-account)
	@test -d .venv || { echo "Error: Run 'make setup' first."; exit 1; }
	@test -f config.ini || { echo "Error: config.ini not found. Run 'make setup' then edit config.ini."; exit 1; }
	.venv/bin/python -m asnb.gui

stop: ## Stop all running instances
	@pkill -f "asnb.main" 2>/dev/null; \
	pkill -f "asnb.gui" 2>/dev/null; \
	pkill -f chromedriver 2>/dev/null; \
	pkill -f "chrome_asnb_" 2>/dev/null; \
	echo "All stopped."

status: ## Show running processes
	@ps aux | grep -E "asnb\.(main|gui)|chromedriver" | grep -v grep || echo "Not running."

logs: ## Show today's log
	@cat asnb_buyer_$$(date +%Y%m%d).log 2>/dev/null || echo "No log for today."

tail: ## Tail today's log (live)
	@tail -f asnb_buyer_$$(date +%Y%m%d).log 2>/dev/null || echo "No log for today."

clean: ## Clean temp files and caches
	rm -rf /tmp/chrome_asnb_* __pycache__ asnb/__pycache__
	@echo "Cleaned."
