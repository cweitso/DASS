#!/usr/bin/env bash
# Start the Prometheus + Grafana overlay on its own.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
start_observability

echo ""
echo "Grafana     http://localhost:3001  (dashboards: /d/dass-overview, /d/dass-k8s)"
echo "Prometheus  http://localhost:9090"
