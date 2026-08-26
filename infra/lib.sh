#!/usr/bin/env bash
# Shared helpers for the scripts in this directory. Source it, do not run it.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Suppress the orphan-container warning that appears whenever these scripts use a
# different -f file combination than the previous invocation. down-all.sh does the
# real cleanup.
export COMPOSE_IGNORE_ORPHANS=true

# Compose file sets. The base stack always includes the local overlay: it is what
# publishes ports 8000/3000 to the host and enables backend hot reload.
COMPOSE_BASE=(-f docker-compose.yml -f docker-compose.local.yml)
COMPOSE_OBSERVABILITY=(-f docker-compose.yml -f docker-compose.observability.yml)
COMPOSE_ALL=(-f docker-compose.yml -f docker-compose.local.yml -f docker-compose.observability.yml)

OBSERVABILITY_SERVICES=(prometheus grafana cadvisor postgres-exporter sqs-exporter)

# kube-state-metrics is scraped by the Compose Prometheus through this forward.
KSM_PORT=30091
KSM_SCRIPT=/tmp/dass-ksm-portforward.sh
KSM_LOG=/tmp/dass-ksm-portforward.log

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_docker() {
  command -v docker >/dev/null 2>&1 ||
    die "docker not found. Install it: https://docs.docker.com/engine/install/"
  docker info >/dev/null 2>&1 ||
    die "the Docker daemon is not running. Start Docker and try again."
}

ensure_env_file() {
  [ -f .env ] || cp .env.example .env
}

# Block until the API answers /health, or give up after $1 seconds (default 120).
wait_for_api() {
  local deadline=$((SECONDS + ${1:-120}))
  echo "Waiting for api-server to become healthy..."
  until curl -fs http://localhost:8000/health 2>/dev/null | grep -q "ok"; do
    [ "$SECONDS" -lt "$deadline" ] ||
      die "api-server was not healthy in time. Check: docker compose logs api-server"
    sleep 2
  done
  echo "API ready: $(curl -fs http://localhost:8000/health)"
}

start_observability() {
  docker compose "${COMPOSE_OBSERVABILITY[@]}" up -d "${OBSERVABILITY_SERVICES[@]}"
}

start_ksm_port_forward() {
  stop_ksm_port_forward
  # A plain kubectl port-forward dies on the first dropped connection, which
  # silently blanks the Kubernetes panels. Wrap it in a restart loop.
  cat > "$KSM_SCRIPT" <<INNER
#!/bin/bash
while true; do
  kubectl -n kube-system port-forward --address=0.0.0.0 \\
    service/kube-state-metrics-nodeport ${KSM_PORT}:8080 2>>"$KSM_LOG"
  echo "\$(date): port-forward exited, restarting in 2s" >> "$KSM_LOG"
  sleep 2
done
INNER
  chmod +x "$KSM_SCRIPT"
  nohup "$KSM_SCRIPT" > "$KSM_LOG" 2>&1 &
  sleep 4
  if curl -s --max-time 3 "http://localhost:${KSM_PORT}/metrics" | head -1 | grep -q "HELP"; then
    echo "kube-state-metrics port-forward is up on :${KSM_PORT}"
  else
    echo "WARNING: kube-state-metrics port-forward is not responding yet"
  fi
}

stop_ksm_port_forward() {
  pkill -f "$(basename "$KSM_SCRIPT")" 2>/dev/null || true
  pkill -f "kubectl.*port-forward.*${KSM_PORT}" 2>/dev/null || true
  rm -f "$KSM_SCRIPT"
}

stop_minikube() {
  if command -v minikube >/dev/null 2>&1 && minikube status &>/dev/null; then
    minikube stop
    echo "Minikube stopped."
  else
    echo "Minikube is not running, skipping."
  fi
}
