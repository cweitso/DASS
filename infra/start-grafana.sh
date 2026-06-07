#!/usr/bin/env bash
# 啟動 Grafana + Prometheus 監控 overlay
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export COMPOSE_IGNORE_ORPHANS=true
command -v docker >/dev/null 2>&1 || { echo "ERROR: 找不到 docker，請先安裝：https://docs.docker.com/engine/install/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon 未運行，請先啟動 Docker。"; exit 1; }

docker compose \
  -f docker-compose.yml \
  -f docker-compose.observability.yml \
  up -d prometheus grafana cadvisor postgres-exporter sqs-exporter

echo ""
echo "Grafana    : http://localhost:3001  (Dashboard: /d/dass-overview)"
echo "Prometheus : http://localhost:9090"
