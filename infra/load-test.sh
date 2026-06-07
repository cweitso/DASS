#!/usr/bin/env bash
# 壓力測試 — 透過 API 建立並 trigger N 個 job，在 Grafana 中觀察結果
#
# Usage:
#   ./infra/load-test.sh [job_count]            # 預設 100
#   DASS_API_URL=https://localhost:8443 ./infra/load-test.sh 200   # 走 Traefik
#
# 預設打 http://localhost:8000（start-mode1/2 直接暴露的 API port，未啟動 Traefik）。
# 若你另外有跑 Traefik，可用 DASS_API_URL 覆蓋成 https://localhost:8443。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export COMPOSE_IGNORE_ORPHANS=true

JOB_COUNT="${1:-10000}"
API_URL="${DASS_API_URL:-http://localhost:8000}"

command -v docker >/dev/null 2>&1 || { echo "ERROR: 找不到 docker，請先安裝：https://docs.docker.com/engine/install/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon 未運行，請先啟動 Docker。"; exit 1; }

# 選擇 Python 直譯器（需有 httpx）：優先用 backend/.venv，其次 uv，否則報錯
if [ -x backend/.venv/bin/python ]; then
  PY=(backend/.venv/bin/python)
elif command -v uv >/dev/null 2>&1; then
  PY=(uv run --project backend python)
else
  echo "ERROR: 找不到 backend/.venv，也沒有 uv。請先在 backend/ 執行 'uv sync --extra dev'。"
  exit 1
fi

echo "=== DASS Load Test: $JOB_COUNT jobs（API: $API_URL）==="
echo ""

# 確保 Grafana 已啟動
echo "--- 啟動 Grafana（若已在運行則略過）---"
docker compose \
  -f docker-compose.yml \
  -f docker-compose.observability.yml \
  up -d prometheus grafana cadvisor postgres-exporter sqs-exporter 2>/dev/null || true

echo ""
echo "Grafana Dashboard: http://localhost:3001/d/dass-overview"
echo ""

# 執行壓測（走完整 HTTP → API → DB → Queue 路徑）
echo "--- 送出 $JOB_COUNT 個 job ---"
"${PY[@]}" scripts/load_gen.py \
  --count "$JOB_COUNT" \
  --concurrency 64 \
  --trigger \
  --api "$API_URL"

echo ""
echo "壓測完成。在 Grafana 觀察 queue depth 與 worker throughput："
echo "  http://localhost:3001/d/dass-overview"
echo ""
echo "觀察 K8s worker 擴縮（模式二）："
echo "  watch -n3 'kubectl get pods -n dass | grep worker'"
