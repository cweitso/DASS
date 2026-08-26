#!/usr/bin/env bash
# Mode 1 — everything in Docker Compose, workers included.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
ensure_env_file

prepull_job_images

echo "=== [1/3] Starting the Compose stack ==="
docker compose "${COMPOSE[@]}" up -d \
  traefik-pki traefik postgres postgres-replica pgbouncer localstack \
  api-server scheduler frontend worker autoscaler

wait_for_api

# If mode 2 was running, hand the queues back to the Compose worker.
scale_down_k8s_workers

echo ""
echo "=== [2/3] Starting Prometheus and Grafana ==="
start_observability

echo ""
echo "=== [3/3] Status ==="
docker compose ps --format "table {{.Name}}\t{{.Status}}"

cat <<EOF

============================================================
  Mode 1 is up.

  Frontend   http://localhost:3000
  API        http://localhost:8000
  API docs   http://localhost:8000/docs
  Traefik    https://dass.localhost:8443   (CA: infra/traefik/pki/rootCA.crt)
  Grafana    http://localhost:3001/d/dass-overview

  Scale workers   docker compose up -d --scale worker=3
  Stop            ./infra/stop-all.sh     (keeps data)
  Destroy         ./infra/down-all.sh     (deletes volumes)
============================================================
EOF
