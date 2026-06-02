#!/usr/bin/env bash
# 關閉所有服務（Docker Compose + Minikube）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== 關閉所有 Docker Compose 服務 ==="
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.observability.yml \
  down 2>/dev/null || \
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  down 2>/dev/null || true

echo ""
echo "=== 關閉 kube-state-metrics port-forward ==="
pkill -f "ksm-pf.sh" 2>/dev/null || true
pkill -f "kubectl.*port-forward.*30091" 2>/dev/null && echo "Port-forward stopped" || echo "Port-forward 未在運行，略過"
rm -f /tmp/dass-ksm-portforward.pid /tmp/ksm-pf.sh /tmp/ksm-pf.log

echo ""
echo "=== 關閉 Minikube（若在運行）==="
if minikube status &>/dev/null 2>&1; then
  minikube stop
  echo "Minikube stopped"
else
  echo "Minikube 未在運行，略過"
fi

echo ""
echo "所有服務已關閉。"
