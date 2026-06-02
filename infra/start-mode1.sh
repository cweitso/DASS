#!/usr/bin/env bash
# 模式一：純 Docker Compose（所有服務含 worker 都在 Docker Compose）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== [1/2] 啟動所有 Docker Compose 服務（含 worker）==="
[ -f .env ] || cp .env.example .env

docker compose -f docker-compose.yml -f docker-compose.local.yml up -d \
  postgres postgres-replica localstack api-server scheduler frontend worker autoscaler

echo "等待 api-server healthy..."
until curl -s http://localhost:8000/health | grep -q "ok"; do sleep 2; done

echo ""
echo "=== [2/2] 啟動 Grafana ==="
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d \
  prometheus grafana cadvisor postgres-exporter sqs-exporter

echo ""
echo "=== [3/3] 確認狀態 ==="
docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo ""
curl -s http://localhost:8000/health
echo ""
echo "============================================================"
echo "  模式一啟動完成"
echo "  Frontend  : http://localhost:3000"
echo "  API       : http://localhost:8000"
echo "  API docs  : http://localhost:8000/docs"
echo "  Grafana   : http://localhost:3001"
echo "    Mode 1 Dashboard : http://localhost:3001/d/dass-overview"
echo ""
echo "  停止服務："
echo "    ./infra/stop-all.sh"
echo "============================================================"
