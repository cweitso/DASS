#!/usr/bin/env bash
# Stop everything AND delete the data: Postgres databases, Grafana and Prometheus
# history all go. Use stop-all.sh to pause without losing state.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker

echo "=== Removing Compose services and volumes ==="
echo "    This deletes the Postgres data and all Grafana/Prometheus history."
docker compose "${COMPOSE[@]}" down -v --remove-orphans

echo ""
echo "=== Stopping the kube-state-metrics port-forward ==="
stop_ksm_port_forward
rm -f "$KSM_LOG"

echo ""
echo "=== Stopping Minikube ==="
stop_minikube

echo ""
echo "Everything is down and the data has been deleted."
