#!/usr/bin/env bash
# 模式二：Docker Compose 基礎服務 + Kubernetes Worker（KEDA 自動擴縮）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== [1/6] 啟動 Docker Compose 基礎服務（不含 worker）==="
[ -f .env ] || cp .env.example .env

docker compose -f docker-compose.yml -f docker-compose.local.yml up -d \
  postgres postgres-replica localstack api-server scheduler frontend

echo "等待 api-server healthy..."
until curl -s http://localhost:8000/health | grep -q "ok"; do sleep 2; done
echo "API ready: $(curl -s http://localhost:8000/health)"

echo ""
echo "=== [2/6] 啟動 Minikube ==="
if minikube status &>/dev/null; then
  echo "Minikube 已在運行，略過 start"
else
  minikube start \
    --nodes 2 \
    --driver docker \
    --cpus 2 \
    --memory 2048 \
    --kubernetes-version stable
fi
kubectl wait --for=condition=Ready nodes --all --timeout=120s
kubectl get nodes

echo ""
echo "=== [3/7] 安裝 KEDA + kube-state-metrics ==="
helm repo add kedacore https://kedacore.github.io/charts 2>/dev/null || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update 2>&1 | grep -E "Successfully|Error" || true

helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --wait \
  --timeout 3m

helm upgrade --install kube-state-metrics prometheus-community/kube-state-metrics \
  --namespace kube-system \
  --wait \
  --timeout 3m
kubectl get pods -n keda
kubectl get pods -n kube-system | grep kube-state-metrics

echo ""
echo "=== [5/7] Build Docker images ==="
docker build -t dass-api:local       -f backend/Dockerfile.api       backend/
docker build -t dass-scheduler:local -f backend/Dockerfile.scheduler  backend/
docker build -t dass-worker:local    -f backend/Dockerfile.worker     backend/

echo ""
echo "=== [6/7] Load images 進 minikube（平行）==="
minikube image load dass-api:local       --overwrite=true &
minikube image load dass-scheduler:local --overwrite=true &
minikube image load dass-worker:local    --overwrite=true &
wait
echo "Images loaded:"
minikube image ls | grep "dass-" || true

echo ""
echo "=== [7/7] 部署 K8s manifests ==="
kubectl apply -f infra/k8s/

echo "等待所有 Deployment 就緒..."
kubectl rollout status deployment/dass-api              -n dass --timeout=180s &
kubectl rollout status deployment/dass-scheduler        -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-normal    -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-scheduled -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-retry     -n dass --timeout=120s &
wait

echo ""
echo "=== 啟動 kube-state-metrics port-forward (keepalive) ==="
pkill -f "ksm-pf.sh\|kubectl.*port-forward.*30091" 2>/dev/null || true
sleep 1
printf '%s\n' '#!/bin/bash' \
  'while true; do' \
  '  kubectl -n kube-system port-forward --address=0.0.0.0 service/kube-state-metrics-nodeport 30091:8080 2>>/tmp/ksm-pf.log' \
  '  echo "$(date): port-forward exited, restarting in 2s..." >> /tmp/ksm-pf.log' \
  '  sleep 2' \
  'done' > /tmp/ksm-pf.sh
chmod +x /tmp/ksm-pf.sh
nohup /tmp/ksm-pf.sh > /tmp/ksm-pf.log 2>&1 &
echo $! > /tmp/dass-ksm-portforward.pid
sleep 4
curl -s --max-time 3 http://localhost:30091/metrics | head -1 | grep -q "HELP" && \
  echo "Port-forward OK (pid=$(cat /tmp/dass-ksm-portforward.pid))" || \
  echo "WARNING: port-forward not responding yet"

echo ""
echo "=== 啟動 Grafana + 重載 Prometheus ==="
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d \
  prometheus grafana cadvisor postgres-exporter sqs-exporter 2>/dev/null || true
sleep 3
curl -s -X POST http://localhost:9090/-/reload 2>/dev/null && echo "Prometheus reloaded" || true

echo ""
echo "============================================================"
echo "  模式二啟動完成"
echo ""
echo "  Docker Compose 服務："
docker compose ps --format "table {{.Name}}\t{{.Status}}" | grep -v "worker" || true
echo ""
echo "  K8s worker pods："
kubectl get pods -n dass | grep worker
echo ""
echo "  KEDA ScaledObjects："
kubectl get scaledobject -n dass
echo ""
echo "  Frontend  : http://localhost:3000"
echo "  API       : http://localhost:8000"
echo "  Grafana   : http://localhost:3001"
echo "    Mode 2 Dashboard : http://localhost:3001/d/dass-k8s"
echo ""
echo "  注意：job 的 action_config.url 呼叫本機 API 請用"
echo "        http://host.minikube.internal:8000（不是 http://api-server:8000）"
echo ""
echo "  停止服務："
echo "    ./infra/stop-all.sh"
echo "============================================================"
