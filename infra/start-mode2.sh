#!/usr/bin/env bash
# 模式二：Docker Compose 基礎服務 + Kubernetes Worker（KEDA 自動擴縮）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 使用不同的 -f 檔案組合時，compose 會把上次留下的 observability 容器當成
# orphan 而印警告。這裡統一抑制這個純裝飾性的警告（真正的清理交給 down-all.sh）。
export COMPOSE_IGNORE_ORPHANS=true

# 想跳過自動安裝（例如 CI / 不想用 sudo）時，設 DASS_NO_AUTO_INSTALL=1
NO_AUTO_INSTALL="${DASS_NO_AUTO_INSTALL:-0}"

# ------------------------------------------------------------------
# 前置工具檢查 / 自動安裝
# ------------------------------------------------------------------
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) GOARCH=amd64 ;;
  aarch64|arm64) GOARCH=arm64 ;;
  *) GOARCH="" ;;
esac

manual_hint() {
  # $1 = tool name
  case "$1" in
    docker)  echo "  Docker:   https://docs.docker.com/engine/install/" ;;
    minikube)echo "  minikube: https://minikube.sigs.k8s.io/docs/start/" ;;
    kubectl) echo "  kubectl:  https://kubernetes.io/docs/tasks/tools/" ;;
    helm)    echo "  helm:     https://helm.sh/docs/intro/install/" ;;
  esac
}

install_helm() {
  echo "→ 安裝 helm（官方 get-helm-3 script，可能需要 sudo 密碼）..."
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
}

install_kubectl() {
  [ -n "$GOARCH" ] || return 1
  echo "→ 安裝 kubectl（linux/$GOARCH，可能需要 sudo 密碼）..."
  local ver tmp
  ver="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/kubectl" "https://dl.k8s.io/release/${ver}/bin/linux/${GOARCH}/kubectl"
  sudo install -o root -g root -m 0755 "$tmp/kubectl" /usr/local/bin/kubectl
  rm -rf "$tmp"
}

install_minikube() {
  [ -n "$GOARCH" ] || return 1
  echo "→ 安裝 minikube（linux/$GOARCH，可能需要 sudo 密碼）..."
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/minikube" "https://storage.googleapis.com/minikube/releases/latest/minikube-linux-${GOARCH}"
  sudo install -m 0755 "$tmp/minikube" /usr/local/bin/minikube
  rm -rf "$tmp"
}

ensure_tool() {
  # $1 = command, $2 = installer function name ("" 表示不可自動安裝)
  local cmd="$1" installer="${2:-}"
  command -v "$cmd" >/dev/null 2>&1 && return 0

  if [ -n "$installer" ] && [ "$NO_AUTO_INSTALL" != "1" ]; then
    echo "缺少 $cmd，嘗試自動安裝..."
    if "$installer" && command -v "$cmd" >/dev/null 2>&1; then
      echo "✔ $cmd 已安裝：$(command -v "$cmd")"
      return 0
    fi
    echo "✗ $cmd 自動安裝失敗。"
  fi

  echo ""
  echo "ERROR: 找不到必要工具 '$cmd'。請先安裝後再執行："
  manual_hint "$cmd"
  echo ""
  echo "（K8s 工具可自動安裝；若要關閉自動安裝請設 DASS_NO_AUTO_INSTALL=1）"
  exit 1
}

echo "=== [0/8] 檢查前置工具（docker / minikube / kubectl / helm）==="
# docker 無法安全地自動安裝，僅檢查
ensure_tool docker ""
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon 未運行，請先啟動 Docker。"; exit 1; }
ensure_tool minikube install_minikube
ensure_tool kubectl install_kubectl
ensure_tool helm install_helm
echo "✔ 前置工具齊備"

echo ""
echo "=== [1/8] 啟動 Docker Compose 基礎服務（不含 worker）==="
[ -f .env ] || cp .env.example .env

docker compose -f docker-compose.yml -f docker-compose.local.yml up -d \
  postgres postgres-replica pgbouncer localstack api-server scheduler frontend

echo "等待 api-server healthy..."
deadline=$((SECONDS + 120))
until curl -fs http://localhost:8000/health 2>/dev/null | grep -q "ok"; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "ERROR: api-server 未在 120 秒內 healthy，請看 docker compose logs api-server"
    exit 1
  fi
  sleep 2
done
echo "API ready: $(curl -fs http://localhost:8000/health)"

echo ""
echo "=== [2/8] 啟動 Minikube ==="
if minikube status -p minikube &>/dev/null; then
  echo "Minikube 已在運行，略過 start"
elif minikube profile list -o json 2>/dev/null | grep -q '"Name":"minikube"'; then
  # cluster 已存在但停止中：沿用既有設定啟動，避免 --memory/--nodes 觸發
  # "You cannot change the memory size for an existing minikube cluster" 警告
  echo "偵測到既有 Minikube cluster（停止中），沿用既有設定啟動..."
  minikube start -p minikube
else
  echo "建立新的 Minikube cluster（2 節點）..."
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
echo "=== [3/8] 安裝 KEDA + kube-state-metrics ==="
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
kubectl get pods -n kube-system | grep kube-state-metrics || true

echo ""
echo "=== [4/8] Build Docker images ==="
docker build -t dass-api:local       -f backend/Dockerfile.api       backend/
docker build -t dass-scheduler:local -f backend/Dockerfile.scheduler  backend/
docker build -t dass-worker:local    -f backend/Dockerfile.worker     backend/

echo ""
echo "=== [5/8] Load images 進 minikube（平行）==="
minikube image load dass-api:local       --overwrite=true &
minikube image load dass-scheduler:local --overwrite=true &
minikube image load dass-worker:local    --overwrite=true &
wait
echo "Images loaded:"
minikube image ls | grep "dass-" || true

echo ""
echo "=== [6/8] 部署 K8s manifests ==="
kubectl apply -f infra/k8s/

echo "等待所有 Deployment 就緒..."
kubectl rollout status deployment/dass-api              -n dass --timeout=180s &
kubectl rollout status deployment/dass-scheduler        -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-normal    -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-scheduled -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-retry     -n dass --timeout=120s &
wait

echo ""
echo "=== [7/8] 啟動 kube-state-metrics port-forward (keepalive) ==="
pkill -f "ksm-pf.sh" 2>/dev/null || true
pkill -f "kubectl.*port-forward.*30091" 2>/dev/null || true
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
if curl -s --max-time 3 http://localhost:30091/metrics | head -1 | grep -q "HELP"; then
  echo "Port-forward OK (pid=$(cat /tmp/dass-ksm-portforward.pid))"
else
  echo "WARNING: port-forward not responding yet"
fi

echo ""
echo "=== [8/8] 啟動 Grafana + 重載 Prometheus ==="
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
kubectl get pods -n dass | grep worker || true
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
echo "    ./infra/down-all.sh"
echo "============================================================"
