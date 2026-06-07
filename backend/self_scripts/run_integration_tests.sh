#!/usr/bin/env bash
# Run backend integration tests against locally spun-up Postgres + the existing
# docker-compose stack's LocalStack on :4566.
#
# Why a separate script: the dev `docker compose` only provides one DB (`dass`)
# on an unpublished port. Integration tests need `dass_test` on :5432 and
# `dass_scheduler` on :5433. This script side-cars those two containers without
# touching the dev compose, and reuses compose's LocalStack so queues stay
# shared.
#
# Usage:
#   scripts/run_integration_tests.sh             # bring up DBs (if needed) + pytest
#   scripts/run_integration_tests.sh up          # bring up DBs + migrate, skip pytest
#   scripts/run_integration_tests.sh down        # tear down test DBs
#   scripts/run_integration_tests.sh test -k retry      # extra args go to pytest
#
# Test containers stay running between invocations for fast iteration. The
# integration conftest wraps each test in a transaction it rolls back, so DB
# state stays clean without rebuilding.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"

PG_TEST=dass-test-pg
PG_SCHED=dass-test-scheduler-pg

require_localstack() {
  if ! curl -fsS http://localhost:4566/_localstack/health >/dev/null 2>&1; then
    echo "✗ LocalStack not reachable at localhost:4566"
    echo "  Bring it up first: docker compose up -d localstack"
    exit 1
  fi
  local cid
  cid=$(docker ps --filter "ancestor=localstack/localstack:3" --format '{{.ID}}' | head -1)
  if [ -z "$cid" ]; then
    echo "✗ LocalStack container not found"
    exit 1
  fi
  for q in dass-tasks dass-tasks-normal dass-tasks-retry; do
    docker exec "$cid" awslocal sqs create-queue --queue-name "$q" >/dev/null 2>&1 || true
  done
}

start_pg() {
  local name=$1 port=$2 db=$3
  if docker ps --format '{{.Names}}' | grep -qx "$name"; then
    echo "▶ $name already running"
    return
  fi
  echo "▶ starting $name (host :$port → container :5432, db=$db)"
  docker run -d --rm --name "$name" \
    -e POSTGRES_DB="$db" \
    -e POSTGRES_USER=dass \
    -e POSTGRES_PASSWORD=dass \
    -p "${port}:5432" \
    postgres:16-alpine >/dev/null
  echo "  waiting for pg_isready..."
  until docker exec "$name" pg_isready -U dass -d "$db" >/dev/null 2>&1; do
    sleep 1
  done
}

migrate() {
  cd "$BACKEND_DIR"
  echo "▶ alembic upgrade on dass_test"
  DASS_DATABASE_URL=postgresql+psycopg://dass:dass@localhost:5432/dass_test \
    .venv/bin/alembic upgrade head
  echo "▶ alembic upgrade on dass_scheduler"
  DASS_DATABASE_URL=postgresql+psycopg://dass:dass@localhost:5433/dass_scheduler \
    .venv/bin/alembic upgrade head
}

down() {
  docker rm -f "$PG_TEST" "$PG_SCHED" >/dev/null 2>&1 || true
  echo "▶ test PG containers removed"
}

cmd=${1:-test}
case "$cmd" in
  down)
    down
    ;;
  up)
    require_localstack
    start_pg "$PG_TEST"  5432 dass_test
    start_pg "$PG_SCHED" 5433 dass_scheduler
    migrate
    echo "✓ services ready — run tests with: $0 test"
    ;;
  test)
    require_localstack
    start_pg "$PG_TEST"  5432 dass_test
    start_pg "$PG_SCHED" 5433 dass_scheduler
    migrate

    cd "$BACKEND_DIR"
    DASS_DATABASE_URL=postgresql+psycopg://dass:dass@localhost:5432/dass_test \
    DASS_SCHEDULER_DB_URL=postgresql+psycopg://dass:dass@localhost:5433/dass_scheduler \
    DASS_SQS_ENDPOINT_URL=http://localhost:4566 \
    DASS_QUEUE_BACKEND=sqs \
    DASS_AWS_ACCESS_KEY_ID=test \
    DASS_AWS_SECRET_ACCESS_KEY=test \
      .venv/bin/pytest tests/integration/ -m integration --override-ini="addopts=" "${@:2}"
    ;;
  *)
    echo "usage: $0 [up|down|test] [extra-pytest-args...]"
    exit 1
    ;;
esac
