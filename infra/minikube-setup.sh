#!/usr/bin/env bash
# ============================================================
# DASS — Minikube + KEDA local setup
#
# Prerequisites:
#   minikube   https://minikube.sigs.k8s.io/docs/start/
#   kubectl    https://kubernetes.io/docs/tasks/tools/
#   helm       https://helm.sh/docs/intro/install/
#   docker     running
#
# Usage:
#   cd <repo-root>
#   chmod +x infra/minikube-setup.sh
#   ./infra/minikube-setup.sh
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== [1/6] Starting Minikube (3 nodes) ==="
minikube start \
  --nodes 2 \
  --driver docker \
  --cpus 2 \
  --memory 2048 \
  --kubernetes-version stable

# Verify all nodes are Ready
echo "Waiting for all nodes to be Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s
kubectl get nodes -o wide

echo ""
echo "=== [2/6] Installing KEDA via Helm ==="
helm repo add kedacore https://kedacore.github.io/charts 2>/dev/null || true
helm repo update kedacore
helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --wait \
  --timeout 3m

echo ""
echo "=== [3/6] Building Docker images and loading into all Minikube nodes ==="
# docker-env is incompatible with multi-node clusters.
# Build with the host Docker daemon, then use `minikube image load` which
# copies the image to every node in the cluster.

docker build -t dass-api:local       -f backend/Dockerfile.api       backend/
docker build -t dass-scheduler:local -f backend/Dockerfile.scheduler  backend/
docker build -t dass-worker:local    -f backend/Dockerfile.worker     backend/

echo "Loading images into all Minikube nodes..."
minikube image load dass-api:local
minikube image load dass-scheduler:local
minikube image load dass-worker:local

echo "Images loaded:"
minikube image ls | grep "dass-" || true

echo ""
echo "=== [4/6] Deploying DASS to Minikube ==="
kubectl apply -f infra/k8s/

echo ""
echo "=== [5/6] Waiting for core services to be ready ==="
kubectl -n dass rollout status deployment/postgres           --timeout=120s
kubectl -n dass rollout status deployment/localstack         --timeout=120s
kubectl -n dass rollout status deployment/dass-api           --timeout=180s
kubectl -n dass rollout status deployment/dass-scheduler     --timeout=120s
kubectl -n dass rollout status deployment/dass-worker-normal    --timeout=120s
kubectl -n dass rollout status deployment/dass-worker-scheduled --timeout=120s
kubectl -n dass rollout status deployment/dass-worker-retry     --timeout=120s

echo ""
echo "=== [6/6] Verifying KEDA ScaledObjects ==="
kubectl -n dass get scaledobject
kubectl -n dass get hpa

echo ""
echo "============================================================"
echo "  DASS is running on Minikube!"
echo ""
API_URL=$(minikube service dass-api -n dass --url 2>/dev/null || echo "run: minikube service dass-api -n dass --url")
echo "  API URL : $API_URL"
echo "  Health  : $API_URL/health"
echo ""
echo "  Tail logs:"
echo "    kubectl -n dass logs -f deployment/dass-worker"
echo "    kubectl -n dass logs -f deployment/dass-scheduler"
echo ""
echo "  Load test:"
echo "    ./infra/load-test.sh"
echo ""
echo "  Watch scaling:"
echo "    watch -n5 'kubectl -n dass get pods -o wide && echo && kubectl -n dass get hpa'"
echo "============================================================"
