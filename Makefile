# Everything runs through Docker Compose project "obs-stack".
COMPOSE := docker compose -p obs-stack

.DEFAULT_GOAL := help
.PHONY: help up demo down clean ps logs lint test dashboards smoke chaos

help: ## Show this help
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-11s %s\n", $$1, $$2}'

up: ## Start the stack and wait until every service is healthy
	$(COMPOSE) up -d --build --wait

demo: ## Start the stack plus the load generator (dashboards fill with data)
	$(COMPOSE) --profile demo up -d --build --wait

down: ## Stop and remove containers (keeps data volumes)
	$(COMPOSE) --profile demo down --remove-orphans

clean: ## Remove containers, network AND volumes of this project
	$(COMPOSE) --profile demo down -v --remove-orphans

ps: ## Show service status
	$(COMPOSE) --profile demo ps

logs: ## Follow the webhook-logger (where alert notifications land)
	$(COMPOSE) logs -f webhook-logger

lint: ## Run all static checks (containerised tools)
	scripts/lint.sh

test: ## Unit-test alert rules and check generated dashboards are current
	scripts/lint.sh promtool_rules promtool_test alert_coverage dashboards

dashboards: ## Regenerate grafana/dashboards/*.json from grafana/generate.py
	python3 grafana/generate.py

smoke: ## End-to-end test: up, verify, break things, assert alerts fire and resolve, tear down
	scripts/smoke-test.sh

chaos: ## Fire real alerts against the running stack (errors, then app down, then recover)
	scripts/chaos.sh demo
