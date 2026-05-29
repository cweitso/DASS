#!/usr/bin/env bash
# 關閉 Grafana + Prometheus 監控 overlay
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

docker compose \
  -f docker-compose.yml \
  -f docker-compose.observability.yml \
  stop prometheus grafana cadvisor postgres-exporter sqs-exporter

echo "Grafana 已關閉。"
