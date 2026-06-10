#!/usr/bin/env bash
# Local mirror of .github/workflows/integration-ci.yml. Spins up throwaway
# Postgres + LocalStack containers (own ports, isolated from the dev stack) and
# runs the integration suite against them.
#
#   run_integration_tests.sh            # up containers + migrate + pytest
#   run_integration_tests.sh up         # up containers + migrate only
#   run_integration_tests.sh down       # tear down test containers
#   run_integration_tests.sh test -k x  # extra args go to pytest

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PG_MAIN=dass-test-pg
LOCALSTACK=dass-test-localstack

# Fixed host ports; the dev stack owns 5432/4566, so the test side-cars don't.
PG_MAIN_PORT=55432
LOCALSTACK_PORT=14566

export DASS_DATABASE_URL="postgresql+psycopg://dass:dass@localhost:${PG_MAIN_PORT}/dass_test"
export DASS_DATABASE_REPLICA_URL="$DASS_DATABASE_URL"
export DASS_SQS_ENDPOINT_URL="http://localhost:${LOCALSTACK_PORT}"
export DASS_QUEUE_BACKEND=sqs
export DASS_QUEUE_NAME=dass-tasks
export DASS_QUEUE_NAME_NORMAL=dass-tasks-normal
export DASS_QUEUE_NAME_SCHEDULED=dass-tasks-scheduled
export DASS_QUEUE_NAME_RETRY=dass-tasks-retry
export DASS_AWS_REGION=us-east-1
export DASS_AWS_ACCESS_KEY_ID=test
export DASS_AWS_SECRET_ACCESS_KEY=test
export DASS_WORKER_ID=ci-worker
export DASS_ENVIRONMENT=test

step() { printf '\n\033[1;34m▶ %s\033[0m\n' "$1"; }

start_pg() {
  local name=$1 port=$2 db=$3
  docker ps --format '{{.Names}}' | grep -qx "$name" && return
  docker run -d --rm --name "$name" \
    -e POSTGRES_DB="$db" -e POSTGRES_USER=dass -e POSTGRES_PASSWORD=dass \
    -p "${port}:5432" postgres:16-alpine >/dev/null
  until docker exec "$name" pg_isready -U dass -d "$db" >/dev/null 2>&1; do sleep 1; done
}

start_localstack() {
  docker ps --format '{{.Names}}' | grep -qx "$LOCALSTACK" && return
  docker run -d --rm --name "$LOCALSTACK" \
    -e SERVICES=sqs -e EAGER_SERVICE_LOADING=1 -e DEBUG=0 \
    -p "${LOCALSTACK_PORT}:4566" localstack/localstack:3 >/dev/null
}

down() {
  docker rm -f "$PG_MAIN" "$LOCALSTACK" >/dev/null 2>&1 || true
  echo "▶ test containers removed"
}

cmd="${1:-test}"
case "$cmd" in
  down) down; exit 0 ;;
  up|test) ;;
  *) echo "usage: $0 [up|down|test] [pytest-args...]"; exit 1 ;;
esac

step "Initialize containers"
start_pg "$PG_MAIN" "$PG_MAIN_PORT" dass_test
start_localstack

step "Wait for LocalStack SQS"
for i in $(seq 1 30); do
  curl -fs "http://localhost:${LOCALSTACK_PORT}/_localstack/health" >/dev/null 2>&1 && break
  [ "$i" = 30 ] && { echo "✗ LocalStack not reachable"; exit 1; }
  sleep 2
done
# A cold SQS provider drops the first messages even after health is green, so
# gate on a real round-trip through a throwaway queue before handing off.
docker exec "$LOCALSTACK" awslocal sqs create-queue --queue-name dass-test-warmup >/dev/null
wurl=$(docker exec "$LOCALSTACK" awslocal sqs get-queue-url --queue-name dass-test-warmup --query QueueUrl --output text)
for i in $(seq 1 30); do
  docker exec "$LOCALSTACK" awslocal sqs send-message --queue-url "$wurl" --message-body ping >/dev/null 2>&1 || true
  docker exec "$LOCALSTACK" awslocal sqs receive-message --queue-url "$wurl" --wait-time-seconds 1 2>/dev/null | grep -q ping && { echo "SQS is ready"; break; }
  [ "$i" = 30 ] && { echo "✗ SQS not round-tripping"; exit 1; }
  sleep 1
done

step "Create SQS queues"
for q in "$DASS_QUEUE_NAME" "$DASS_QUEUE_NAME_NORMAL" "$DASS_QUEUE_NAME_SCHEDULED" "$DASS_QUEUE_NAME_RETRY"; do
  docker exec "$LOCALSTACK" awslocal sqs create-queue --queue-name "$q" >/dev/null
done

cd "$BACKEND_DIR"
step "Run DB migrations"
.venv/bin/python -m alembic upgrade head

if [ "$cmd" = up ]; then
  echo "✓ services ready — run tests with: $0 test"
  exit 0
fi

step "Run integration tests"
# -v -rA: per-test names + full result summary. --capture=tee-sys + --log-cli-level
# stream each test's prints/logs live. --html: self-contained report (kept even on fail).
REPORT="$BACKEND_DIR/test-reports/integration.html"
mkdir -p "$(dirname "$REPORT")"
rc=0
.venv/bin/python -m pytest tests/integration/ -m integration --override-ini="addopts=" \
  -v -rA --capture=tee-sys --log-cli-level=INFO \
  --html="$REPORT" --self-contained-html \
  "${@:2}" || rc=$?
echo
echo "▶ HTML report: http://localhost:8800/integration.html"
exit "$rc"
