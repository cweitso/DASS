#!/usr/bin/env bash
# 完全關閉並清除所有服務：
#   - docker compose down -v --remove-orphans（連同 named volumes 一起刪：
#     Postgres 資料庫、Grafana/Prometheus 資料都會被清掉）
#   - 停止 kube-state-metrics port-forward
#   - 停止 Minikube
#
# 注意：這會刪除資料。只想暫停 Docker 服務而保留資料，請用
#   docker compose stop
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== 關閉並清除所有 Docker Compose 服務（含資料卷）==="
echo "    ⚠ 這會刪除 Postgres 資料庫與 Grafana/Prometheus 資料"
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.observability.yml \
  down -v --remove-orphans

echo ""
echo "=== 關閉 kube-state-metrics port-forward ==="
pkill -f "ksm-pf.sh" 2>/dev/null || true
pkill -f "kubectl.*port-forward.*30091" 2>/dev/null && echo "Port-forward stopped" || echo "Port-forward 未在運行，略過"
rm -f /tmp/dass-ksm-portforward.pid /tmp/ksm-pf.sh /tmp/ksm-pf.log

echo ""
echo "=== 關閉 Minikube（若在運行）==="
if minikube status &>/dev/null; then
  minikube stop
  echo "Minikube stopped"
else
  echo "Minikube 未在運行，略過"
fi

echo ""
echo "所有服務已關閉、資料已清除。"
