#!/usr/bin/env bash
# 啟動 Grafana + Prometheus 監控 overlay
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

docker compose \
  -f docker-compose.yml \
  -f docker-compose.observability.yml \
  up -d prometheus grafana cadvisor postgres-exporter sqs-exporter

echo ""
echo "Grafana    : http://localhost:3001  (Dashboard: /d/dass-overview)"
echo "Prometheus : http://localhost:9090"
