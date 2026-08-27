#!/usr/bin/env bash
# Push N job runs through the API, then watch the result in Grafana.
#
#   ./infra/load-test.sh [job_count]                              # default 100
#   DASS_API_URL=http://localhost:8000 ./infra/load-test.sh 1000  # bypass Traefik
#
# N jobs produce N task runs. These jobs carry no cron, so POST /jobs creates a
# one-time job and dispatches it in the same request — no separate trigger is
# needed, and adding --trigger would run every job a second time and double the
# task count against the number you asked for.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

JOB_COUNT="${1:-100}"
API_URL="${DASS_API_URL:-https://dass.localhost:8443}"

require_docker

# load_gen.py needs httpx, which lives in the backend environment.
if [ -x backend/.venv/bin/python ]; then
  PYTHON=(backend/.venv/bin/python)
elif command -v uv >/dev/null 2>&1; then
  PYTHON=(uv run --project backend python)
else
  die "no backend/.venv and no uv. Run 'uv sync --extra dev' in backend/ first."
fi

echo "=== Load test: $JOB_COUNT job runs against $API_URL ==="
start_observability
echo ""

"${PYTHON[@]}" scripts/load_gen.py \
  --count "$JOB_COUNT" \
  --concurrency 64 \
  --api "$API_URL"

cat <<EOF

Watch queue depth and worker throughput:
  http://localhost:3001/d/dass-overview

To also exercise POST /jobs/{id}/trigger, add --trigger to load_gen.py directly.
That runs every job twice, so $JOB_COUNT jobs become $((JOB_COUNT * 2)) task runs.

Watch Kubernetes worker scaling (mode 2):
  watch -n3 'kubectl get pods -n dass | grep worker'
EOF
