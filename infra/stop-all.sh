#!/usr/bin/env bash
# Stop everything but keep the data. Use down-all.sh to delete volumes as well.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker

echo "=== Stopping Compose services ==="
docker compose "${COMPOSE_ALL[@]}" down

echo ""
echo "=== Stopping the kube-state-metrics port-forward ==="
stop_ksm_port_forward

echo ""
echo "=== Stopping Minikube ==="
stop_minikube

echo ""
echo "Stopped. Data volumes are intact — ./infra/start-mode1.sh brings it back."
