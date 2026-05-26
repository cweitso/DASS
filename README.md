# DASS

`DASS` is a Distributed Asynchronous Scheduling System: a production-style MVP for creating, scheduling, dispatching, and observing internal jobs.

## Stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic, boto3, httpx, croniter
- Frontend: Next.js, React, TypeScript, TanStack Query, Tailwind CSS
- Database: PostgreSQL
- Queue: AWS SQS, with LocalStack for local development

## Architecture

```mermaid
flowchart LR
  Browser[Browser]
  Frontend[Next.js Frontend\n:3000]
  API[FastAPI API Server\n:8000]
  Scheduler[Scheduler Worker]
  Worker[Job Worker]
  DB[(PostgreSQL)]
  Queue[(AWS SQS / LocalStack)]

  Browser --> Frontend
  Frontend -->|API requests and rewrites| API
  API --> DB
  API --> Queue
  Scheduler --> DB
  Scheduler --> Queue
  Worker --> DB
  Worker --> Queue
  Queue --> Worker
```

The frontend serves the UI and proxies API calls to the backend. The backend owns persistence in PostgreSQL, and scheduling/worker processes coordinate job dispatch and execution through the queue.

## Top-Level Structure

```text
dass/
  backend/       # FastAPI app, scheduler, worker, autoscaler, models, services
  frontend/      # Next.js dashboard (placeholder UI, to be implemented)
  infra/         # LocalStack, PostgreSQL (primary/replica), observability configs
  scripts/       # load_gen.py (stress), run_integration_tests.sh, e2e_smoke.py
  docker-compose.yml
  docker-compose.observability.yml   # Prometheus + Grafana overlay
  docker-compose.local.yml
  .env.example
  README.md
```

## Quick Start (Docker Compose)

This is the **only** way you need to run the full stack. All services (DB, queue, backend, frontend) are brought up together.

```bash
# 1. Copy the environment file
cp .env.example .env

# 2. Build and start everything
docker compose up --build

# 3. Wait for all services to be healthy, then open:
#    - Frontend:   http://localhost:3000
#    - API:        http://localhost:8000
#    - API docs:   http://localhost:8000/docs
#    - LocalStack: http://localhost:4566
```

### Verify Services Are Running

```bash
# Backend health check
curl http://localhost:8000/health
# Expected: {"status":"ok","service":"dass"}

# Metrics
curl http://localhost:8000/metrics
# Expected: {"jobs":0,"tasks":0}
```

### Seed Example Data

```bash
docker compose exec api-server python scripts/seed.py
```

### Stop

```bash
docker compose down
```

### Rebuild From Scratch (including database)

```bash
docker compose down -v
docker compose up --build
```

## Viewing Logs

```bash
# All services at once (recommended)
docker compose logs -f

# Individual services
docker compose logs -f api-server
docker compose logs -f scheduler
docker compose logs -f worker
docker compose logs -f frontend
```

To enable verbose logging, edit `.env` and set `DASS_LOG_LEVEL=DEBUG`, then restart:

```bash
docker compose up --build
```

### Common Issues

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `connection refused` on port 8000 | API server still starting or crashed | Check `docker compose logs api-server` |
| API server restarts repeatedly | Database or LocalStack not ready yet | Wait — Docker healthchecks handle ordering. If persistent, run `docker compose down -v && docker compose up --build` |
| Frontend shows blank page | Frontend container still building | Check `docker compose logs frontend` — Next.js build takes a moment |

## Database Migrations

Migrations run automatically when the API server starts (`entrypoint.sh` runs `alembic upgrade head`). To run manually:

```bash
docker compose exec api-server alembic upgrade head
```

## Worker Scaling

Workers are horizontally scalable:

```bash
docker compose up --scale worker=3
```

## Autoscaling

An `autoscaler` service watches queue depth and spawns/reaps extra `worker` containers on the host Docker daemon. Autoscaled workers carry the label `com.dass.autoscaled=true`, which is how scale-down (and the cleanup command below) tells them apart from the baseline worker. Tune the loop with `DASS_AUTOSCALER_INTERVAL_SECONDS` (default `30`).

If a load test leaves autoscaled workers behind, kill **just those** — the base stack (DB, queue, API) is untouched:

```bash
docker ps -q --filter "label=com.dass.autoscaled=true" | xargs -r docker kill
```

> `docker ps -q | xargs -r docker kill` (no filter) stops **every** running container, including your DB, queue, and Grafana. Use it only for a deliberate hard reset.

## Observability (Prometheus + Grafana)

The metrics stack (Prometheus, Grafana, cAdvisor, postgres-exporter, custom sqs-exporter) lives in a separate overlay so the dev stack stays lean — bring it up only when you want to watch a load test:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

| Tool | URL | Notes |
|------|-----|-------|
| Grafana | http://localhost:3001 | Dashboard **DASS · Overview** at `/d/dass-overview`; anonymous admin (dev only) |
| Prometheus | http://localhost:9090 | Raw metrics + query UI |
| cAdvisor | http://localhost:8081 | Per-container CPU / memory |

Tear it down, including the metrics volumes:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml down -v
```

## Load / Stress Testing

`scripts/load_gen.py` drives real HTTP traffic through the full API → JobService → repository → DB → queue path (not direct DB inserts), so queue depth, worker throughput, and DB pressure react for real. Run it with the backend's virtualenv, where `httpx` is installed:

```bash
cd backend   # so the project .venv (with httpx) is used
.venv/bin/python ../scripts/load_gen.py --count 10000 --concurrency 64 --trigger
```

| Flag | Meaning |
|------|---------|
| `--count N` | number of jobs to create (default 1000) |
| `--concurrency N` | parallel in-flight HTTP requests (default 32) |
| `--trigger` | after creating, fire each job once via `/trigger` |
| `--api URL` | API base URL (default `http://localhost:8000`) |

Bring up the observability overlay first to watch it live at `/d/dass-overview`.

## Testing

### Unit tests

Unit tests use an in-memory SQLite database and a memory queue — **no Docker, no PostgreSQL, no env setup**. The `tests/integration` directory is auto-excluded via `pyproject.toml`.

```bash
cd backend
uv run pytest            # or: .venv/bin/pytest
```

### Integration tests

Integration tests need real PostgreSQL + LocalStack. `scripts/run_integration_tests.sh` side-cars two throwaway Postgres containers (`dass_test` on :5432, `dass_scheduler` on :5433), runs migrations, and reuses the compose stack's LocalStack on :4566. Each test runs inside a transaction that is rolled back, so DB state stays clean between runs.

```bash
# LocalStack must be reachable first
docker compose up -d localstack

scripts/run_integration_tests.sh                 # bring up test DBs (if needed) + run pytest
scripts/run_integration_tests.sh up              # bring up + migrate only, skip pytest
scripts/run_integration_tests.sh down            # tear down the test DBs
scripts/run_integration_tests.sh test -k retry   # extra args pass through to pytest
```

Test containers stay running between invocations for fast iteration — run `... down` when you're finished.

### Running CI workflows locally (`act`)

CI runs two workflows: `backend-ci.yml` (unit tests, no Docker) and `integration-ci.yml` (integration tests, real Postgres + LocalStack). You can reproduce them locally with [`act`](https://nektosact.com). The repo's `.actrc` and `.actignore` are auto-loaded — they point `--env-file` at `/dev/null` so your local `.env` can't shadow the workflow's own `env:` block. Since `.actrc` pins no runner image, pass one with `-P`.

**Unit workflow** — no service containers, so it runs anytime (even with the dev stack up). Its jobs are gated by PR-title tags, so feed a fake event whose title contains `[all]` to trigger them all:

```bash
echo '{"pull_request": {"title": "[all] local ci"}}' > /tmp/pr-event.json

act pull_request \
  -W .github/workflows/backend-ci.yml \
  -e /tmp/pr-event.json \
  -P ubuntu-latest=catthehacker/ubuntu:act-latest
```

**Integration workflow** — `act` uses your host Docker daemon, and this workflow publishes a LocalStack service container on host port **4566**, which the dev stack's own LocalStack already owns. Bring the dev stack down first to free the port (and avoid `act` grabbing the wrong LocalStack container), then restore it afterward:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml down

act workflow_dispatch \
  -W .github/workflows/integration-ci.yml \
  -P ubuntu-latest=catthehacker/ubuntu:act-latest

# bring the dev stack back up when done
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

`workflow_dispatch` is used so the run isn't gated by a PR title. The integration tests run in the **Run integration tests** step — that's where pass/xfail/fail shows up.

> The final **Upload test results** step (`actions/upload-artifact@v4`) fails under `act` with `Unable to get the ACTIONS_RUNTIME_TOKEN env variable`. This is harmless — artifact upload needs the GitHub-hosted artifact service, which `act` doesn't provide by default; your tests have already run by then. To silence it, add `--artifact-server-path /tmp/act-artifacts` to the `act` command.

> If you only want to *run* the integration tests (not exercise the workflow YAML), `scripts/run_integration_tests.sh` is simpler — it coexists with the running dev stack and reuses its LocalStack, so no port juggling.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/jobs` | Create a job |
| `GET` | `/api/v1/jobs` | List all jobs |
| `GET` | `/api/v1/jobs/{id}` | Get a single job |
| `PUT` | `/api/v1/jobs/{id}` | Update a job |
| `DELETE` | `/api/v1/jobs/{id}` | Delete a job |
| `POST` | `/api/v1/jobs/{id}/trigger` | Manually trigger a job |
| `GET` | `/api/v1/jobs/{id}/tasks` | List tasks for a job |
| `POST` | `/api/v1/tasks/{id}/retry` | Retry a failed task |
| `GET` | `/health` | Health check |
| `GET` | `/metrics` | Job and task counts |

Interactive API documentation is available at `http://localhost:8000/docs` when the server is running.

## Development Workflow (Optional)

If you want **hot reload** for backend or frontend code while all infrastructure (DB, LocalStack) stays in Docker, use the local override file:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

This mounts `backend/` and `frontend/` source into the containers so code changes take effect immediately without rebuilding images.

### Running Backend or Frontend Outside Docker

If you prefer to run the backend or frontend directly on your host (e.g. for debugger support), you can stop the corresponding Docker service and run it locally instead. **You still need the Docker services for PostgreSQL and LocalStack.**

```bash
# Keep infra running
docker compose up postgres localstack -d

# Backend (from backend/)
cd backend
uv sync --extra dev
DASS_DATABASE_URL=postgresql+psycopg://dass:dass@localhost:5432/dass \
DASS_SQS_ENDPOINT_URL=http://localhost:4566 \
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend (from frontend/)
cd frontend
npm install
npm run dev
```

Note: when running outside Docker, the database URL must use `localhost` instead of `postgres`, and the SQS endpoint must use `localhost` instead of `localstack`.

## Implementation Status

This repository is a scaffold — the architecture, interfaces, models, schemas, tests, and infrastructure are all in place, but the business logic has been hollowed out for the team to implement.

The frontend dashboard also shows placeholder pages — the three interactive views (jobs-list-page, job-detail-page, job-form-page) are scaffolded but not yet implemented.

The provided test suite (tests/) serves as the acceptance criteria. Once your implementations replace the NotImplementedError stubs, the tests should pass.

## Notes

- PostgreSQL is the source of truth.
- SQS is used only as a delivery mechanism.
- The Next.js frontend proxies API requests to the backend through rewrites, so the browser stays on one origin and avoids CORS issues.
- The scheduler runs every 5 seconds and is responsible for deciding when work should be dispatched.
- Workers claim tasks atomically and report results back to PostgreSQL.
- Shell execution is supported for local and internal use, but it is dangerous in production and should be restricted carefully.
