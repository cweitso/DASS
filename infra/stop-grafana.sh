#!/usr/bin/env bash
# Stop the observability overlay, leaving the rest of the stack running.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
docker compose "${COMPOSE_OBSERVABILITY[@]}" stop "${OBSERVABILITY_SERVICES[@]}"
echo "Observability stack stopped."
