# ===========================================================================
# MSc Phase 1 -- RCA research bench
#
# Two targets, one platform layer:
#   TARGET=cloud  (default)  single EC2 node running k3s, managed by Terraform
#   TARGET=local             k3d on your own machine -- same k3s, zero cost
#
# Everything under platform/ applies identically to either, because both run
# k3s. See docs/02-platform-choice.md for why that symmetry was chosen.
# ===========================================================================

SHELL     := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

TARGET     ?= cloud
TF_DIR     := infra/terraform
KUBECONFIG_PATH := $(CURDIR)/kubeconfig
export KUBECONFIG_PATH
export TARGET

# ---------------------------------------------------------------------------
help: ## Show this help
	@echo ""
	@echo "  MSc RCA bench -- TARGET=$(TARGET)"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n",$$1,$$2}'
	@echo ""
	@echo "  Typical first run (local, free):"
	@echo "    make local-up && make install TARGET=local && make verify TARGET=local"
	@echo ""
	@echo "  Typical first run (cloud):"
	@echo "    make tf-apply && make kubeconfig && make install && make verify"
	@echo ""

# --------------------------------------------------------------- cloud infra
.PHONY: tf-init tf-plan tf-apply tf-destroy tf-fmt tf-validate
tf-init: ## terraform init
	cd $(TF_DIR) && terraform init

tf-plan: ## terraform plan
	cd $(TF_DIR) && terraform plan

tf-apply: ## Create the cloud bench (asks for confirmation)
	cd $(TF_DIR) && terraform apply

tf-destroy: ## Tear the cloud bench down completely
	cd $(TF_DIR) && terraform destroy

tf-fmt: ## Format terraform files
	cd $(TF_DIR) && terraform fmt -recursive

tf-validate: ## Validate terraform syntax and types
	cd $(TF_DIR) && terraform init -backend=false && terraform validate

# ------------------------------------------------------------- cloud node ops
.PHONY: start stop status
start:  ## Start the stopped cloud node
	@scripts/bench.sh start
stop:   ## Stop the cloud node -- DO THIS WHEN YOU FINISH
	@scripts/bench.sh stop
status: ## Is the node running and burning credits?
	@scripts/bench.sh status

# ------------------------------------------------------------------ local k3d
.PHONY: local-up local-stop local-destroy
local-up:      ## Create/start the free local k3d bench
	@scripts/local-up.sh
local-stop:    ## Stop the local bench, keep its state
	@scripts/local-down.sh stop
local-destroy: ## Delete the local bench entirely
	@scripts/local-down.sh destroy

# -------------------------------------------------------------------- cluster
.PHONY: kubeconfig install verify diag telemetry pin-versions uninstall
kubeconfig: ## Fetch kubeconfig into ./kubeconfig
	@scripts/kubeconfig.sh

install: ## Install the platform layer (observability, app, chaos, rbac)
	@scripts/install-platform.sh

verify: ## P0 exit test: liveness + trust-boundary validity
	@scripts/verify.sh
	@scripts/chaos-check.sh

diag: ## What is the cluster doing right now? Safe to run during install.
	@scripts/diag.sh

telemetry: ## Trace the telemetry path and show exactly where it stops
	@scripts/telemetry-trace.sh

pin-versions: ## Record deployed chart versions into platform/versions.env
	@scripts/pin-versions.sh

uninstall: ## Remove the platform layer, leave the cluster
	@KUBECONFIG=$(KUBECONFIG_PATH) helm uninstall astronomy -n app || true
	@KUBECONFIG=$(KUBECONFIG_PATH) helm uninstall chaos-mesh -n chaos-mesh || true
	@for r in grafana otel-collector loki tempo prometheus; do \
	  KUBECONFIG=$(KUBECONFIG_PATH) helm uninstall $$r -n observability || true; \
	done

# ---------------------------------------------------------------------- chaos
.PHONY: chaos-apply chaos-delete chaos-list chaos-check chaos-smoke
chaos-check: ## Verify every experiment's selector matches running pods
	@scripts/chaos-check.sh

chaos-smoke: ## P0 exit test: inject a fault and confirm it is visible
	@scripts/smoke-chaos.sh $(EXP)

chaos-apply: ## Inject a fault: make chaos-apply EXP=01-network-delay-productcatalog
	@test -n "$(EXP)" || { echo "usage: make chaos-apply EXP=<name>"; exit 1; }
	KUBECONFIG=$(KUBECONFIG_PATH) kubectl apply -f platform/chaos/experiments/$(EXP).yaml

chaos-delete: ## Stop a fault: make chaos-delete EXP=<name>
	@test -n "$(EXP)" || { echo "usage: make chaos-delete EXP=<name>"; exit 1; }
	KUBECONFIG=$(KUBECONFIG_PATH) kubectl delete -f platform/chaos/experiments/$(EXP).yaml --ignore-not-found

chaos-list: ## List available experiments and anything currently running
	@ls platform/chaos/experiments/*.yaml | xargs -n1 basename | sed 's/.yaml//' | sed 's/^/  /'
	@echo "  --- active ---"
	@KUBECONFIG=$(KUBECONFIG_PATH) kubectl get networkchaos,podchaos,stresschaos -n chaos-mesh 2>/dev/null || true

# --------------------------------------------------------------------- access
.PHONY: tunnel-grafana tunnel-prometheus tunnel-tempo tunnel-loki tunnel-frontend tunnel-chaos
tunnel-grafana:    ## Grafana on localhost:3000 (admin/admin)
	@scripts/tunnel.sh grafana
tunnel-prometheus: ## Prometheus on localhost:9090
	@scripts/tunnel.sh prometheus
tunnel-tempo:      ## Tempo on localhost:3200
	@scripts/tunnel.sh tempo
tunnel-loki:       ## Loki on localhost:3100
	@scripts/tunnel.sh loki
tunnel-frontend:   ## Astronomy Shop on localhost:8080
	@scripts/tunnel.sh frontend
tunnel-chaos:      ## Chaos Mesh dashboard on localhost:2333
	@scripts/tunnel.sh chaos

# ----------------------------------------------------------------------- lint
.PHONY: lint lint-yaml lint-sh lint-rbac show-app-values
lint: lint-yaml lint-sh lint-rbac ## Run all local linters

lint-yaml: ## Parse every YAML file
	@python3 scripts/lint_yaml.py

lint-rbac: ## Assert the trust boundary statically (no cluster needed)
	@python3 scripts/check_rbac.py

lint-sh: ## shellcheck + bash -n on every script
	@for f in scripts/*.sh; do bash -n "$$f" || exit 1; done
	@command -v shellcheck >/dev/null 2>&1 \
	  && shellcheck -S warning scripts/*.sh \
	  || echo "  (shellcheck not installed -- syntax checked only)"
	@echo "  scripts OK"

show-app-values: ## Print the demo chart's own default values, to diff key names
	@helm show values open-telemetry/opentelemetry-demo | head -120
