#!/usr/bin/env bash
# Mode 2 — Compose runs the infrastructure, Kubernetes runs the workers.
#
# One Deployment per queue, each scaled by its own KEDA ScaledObject from that
# queue's depth. Set DASS_NO_AUTO_INSTALL=1 to skip installing missing K8s tools.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

NO_AUTO_INSTALL="${DASS_NO_AUTO_INSTALL:-0}"

case "$(uname -m)" in
  x86_64|amd64) GOARCH=amd64 ;;
  aarch64|arm64) GOARCH=arm64 ;;
  *) GOARCH="" ;;
esac

install_hint() {
  case "$1" in
    docker)   echo "  Docker:   https://docs.docker.com/engine/install/" ;;
    minikube) echo "  minikube: https://minikube.sigs.k8s.io/docs/start/" ;;
    kubectl)  echo "  kubectl:  https://kubernetes.io/docs/tasks/tools/" ;;
    helm)     echo "  helm:     https://helm.sh/docs/intro/install/" ;;
  esac
}

install_helm() {
  echo "Installing helm via the official get-helm-3 script (may prompt for sudo)..."
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
}

install_kubectl() {
  [ -n "$GOARCH" ] || return 1
  echo "Installing kubectl for linux/$GOARCH (may prompt for sudo)..."
  local version tmp
  version="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/kubectl" "https://dl.k8s.io/release/${version}/bin/linux/${GOARCH}/kubectl"
  sudo install -o root -g root -m 0755 "$tmp/kubectl" /usr/local/bin/kubectl
  rm -rf "$tmp"
}

install_minikube() {
  [ -n "$GOARCH" ] || return 1
  echo "Installing minikube for linux/$GOARCH (may prompt for sudo)..."
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/minikube" "https://storage.googleapis.com/minikube/releases/latest/minikube-linux-${GOARCH}"
  sudo install -m 0755 "$tmp/minikube" /usr/local/bin/minikube
  rm -rf "$tmp"
}

ensure_tool() {
  local cmd="$1" installer="${2:-}"
  command -v "$cmd" >/dev/null 2>&1 && return 0

  if [ -n "$installer" ] && [ "$NO_AUTO_INSTALL" != "1" ]; then
    echo "$cmd is missing, attempting to install it..."
    if "$installer" && command -v "$cmd" >/dev/null 2>&1; then
      echo "Installed $cmd at $(command -v "$cmd")"
      return 0
    fi
    echo "Automatic installation of $cmd failed."
  fi

  echo ""
  echo "Install '$cmd' and run this again:"
  install_hint "$cmd"
  echo ""
  echo "(Set DASS_NO_AUTO_INSTALL=1 to always install K8s tools yourself.)"
  exit 1
}

echo "=== [0/8] Checking prerequisites ==="
# Docker cannot be installed safely from a script, so only check for it.
ensure_tool docker ""
require_docker
ensure_tool minikube install_minikube
ensure_tool kubectl install_kubectl
ensure_tool helm install_helm
echo "All prerequisites present."

echo ""
prepull_job_images

echo "=== [1/8] Starting the Compose stack (without workers) ==="
ensure_env_file
docker compose "${COMPOSE[@]}" up -d \
  traefik-pki traefik postgres postgres-replica pgbouncer localstack \
  api-server scheduler frontend
wait_for_api

echo ""
echo "=== [2/8] Starting Minikube ==="
if minikube status -p minikube &>/dev/null; then
  echo "Minikube is already running."
elif minikube profile list -o json 2>/dev/null | grep -q '"Name":"minikube"'; then
  # Reuse the existing cluster's settings; passing --memory/--nodes to a cluster
  # that already exists only produces "you cannot change ..." warnings.
  echo "Starting the existing Minikube cluster..."
  minikube start -p minikube
else
  echo "Creating a two-node Minikube cluster..."
  minikube start --nodes 2 --driver docker --cpus 2 --memory 2048 --kubernetes-version stable
fi
kubectl wait --for=condition=Ready nodes --all --timeout=120s
kubectl get nodes

echo ""
echo "=== [3/8] Installing KEDA and kube-state-metrics ==="
helm repo add kedacore https://kedacore.github.io/charts 2>/dev/null || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update 2>&1 | grep -E "Successfully|Error" || true
helm upgrade --install keda kedacore/keda \
  --namespace keda --create-namespace --wait --timeout 3m
helm upgrade --install kube-state-metrics prometheus-community/kube-state-metrics \
  --namespace kube-system --wait --timeout 3m

echo ""
echo "=== [4/8] Building images ==="
docker build -t dass-api:local       -f backend/Dockerfile.api       backend/
docker build -t dass-scheduler:local -f backend/Dockerfile.scheduler backend/
docker build -t dass-worker:local    -f backend/Dockerfile.worker    backend/

echo ""
echo "=== [5/8] Loading images into Minikube ==="
minikube image load dass-api:local       --overwrite=true &
minikube image load dass-scheduler:local --overwrite=true &
minikube image load dass-worker:local    --overwrite=true &
wait
minikube image ls | grep "dass-" || true

echo ""
echo "=== [6/8] Applying manifests ==="
kubectl apply -f infra/k8s/
for deployment in dass-api dass-scheduler dass-worker-normal dass-worker-scheduled dass-worker-retry; do
  kubectl rollout status "deployment/$deployment" -n dass --timeout=180s &
done
wait

echo ""
echo "=== [7/8] Forwarding kube-state-metrics to the host ==="
start_ksm_port_forward

echo ""
echo "=== [8/8] Starting Prometheus and Grafana ==="
start_observability
sleep 3
curl -s -X POST http://localhost:9090/-/reload >/dev/null 2>&1 && echo "Prometheus reloaded" || true

echo ""
echo "K8s worker pods:"
kubectl get pods -n dass | grep worker || true
echo ""
echo "KEDA ScaledObjects:"
kubectl get scaledobject -n dass

cat <<EOF

============================================================
  Mode 2 is up.

  Frontend   http://localhost:3000
  API        http://localhost:8000
  Traefik    https://dass.localhost:8443   (CA: infra/traefik/pki/rootCA.crt)
  Grafana    http://localhost:3001/d/dass-k8s

  Note: a job whose action_config.url points at this machine must use
        http://host.minikube.internal:8000 — api-server is a Compose hostname
        that Kubernetes pods cannot resolve.

  Watch scaling   watch -n3 'kubectl get pods -n dass | grep worker'
  Stop            ./infra/stop-all.sh     (keeps data)
  Destroy         ./infra/down-all.sh     (deletes volumes)
============================================================
EOF
