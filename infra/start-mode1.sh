#!/usr/bin/env bash
# 模式一：純 Docker Compose（所有服務含 worker 都在 Docker Compose）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 抑制因不同 -f 檔案組合而產生的 orphan 容器警告
export COMPOSE_IGNORE_ORPHANS=true

command -v docker >/dev/null 2>&1 || { echo "ERROR: 找不到 docker，請先安裝：https://docs.docker.com/engine/install/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon 未運行，請先啟動 Docker。"; exit 1; }

echo "=== [1/3] 啟動所有 Docker Compose 服務（含 worker）==="
[ -f .env ] || cp .env.example .env

docker compose -f docker-compose.yml -f docker-compose.local.yml up -d \
  postgres postgres-replica pgbouncer localstack api-server scheduler frontend worker autoscaler

echo "等待 api-server healthy..."
deadline=$((SECONDS + 120))
until curl -fs http://localhost:8000/health 2>/dev/null | grep -q "ok"; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "ERROR: api-server 未在 120 秒內 healthy，請看 docker compose logs api-server"
    exit 1
  fi
  sleep 2
done

echo ""
echo "=== [2/3] 啟動 Grafana ==="
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
echo "    ./infra/down-all.sh"
echo "============================================================"
