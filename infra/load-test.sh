#!/usr/bin/env bash
# 壓力測試 — 透過 API 建立並 trigger N 個 job，在 Grafana 中觀察結果
#
# Usage:
#   ./infra/load-test.sh [job_count]   # 預設 100
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

JOB_COUNT="${1:-100}"

echo "=== DASS Load Test: $JOB_COUNT jobs ==="
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
cd backend
.venv/bin/python ../scripts/load_gen.py \
  --count "$JOB_COUNT" \
  --concurrency 32 \
  --trigger

echo ""
echo "壓測完成。在 Grafana 觀察 queue depth 與 worker throughput："
echo "  http://localhost:3001/d/dass-overview"
echo ""
echo "觀察 K8s worker 擴縮（模式二）："
echo "  watch -n3 'kubectl get pods -n dass | grep worker'"
