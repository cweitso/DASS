# DASS

`DASS` is a Distributed Asynchronous Scheduling System: a production-style MVP for
creating, scheduling, dispatching and observing internal jobs.

A **job** is a definition — what to run, and when. A **task** is one run of that job.
Jobs reach a worker through a queue, workers execute each task inside a throwaway
container, and PostgreSQL is the source of truth for both.

- **Backend** — Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic, boto3, croniter
- **Frontend** — Next.js, React, TypeScript, TanStack Query, Tailwind CSS
- **Database** — PostgreSQL with a streaming read replica, pooled through PgBouncer
- **Queue** — AWS SQS, backed by LocalStack for local development
- **Proxy** — Traefik, terminating TLS on a locally generated CA
- **Observability** — Prometheus, Grafana, cAdvisor, postgres-exporter, a custom SQS exporter

---

## Quick start

```bash
./infra/start-mode1.sh        # everything in Docker Compose
python3 scripts/e2e_smoke.py  # verify the whole pipeline end to end
./infra/down-all.sh           # stop and delete the data
```

| Command | What it does |
|---|---|
| `./infra/start-mode1.sh` | Mode 1 — the full stack in Docker Compose, workers included |
| `./infra/start-mode2.sh` | Mode 2 — Compose infrastructure, Kubernetes workers scaled by KEDA |
| `./infra/stop-all.sh` | Stop everything, **keep** the data |
| `./infra/down-all.sh` | Stop everything and **delete** the volumes |
| `./infra/start-grafana.sh` | Bring up Prometheus and Grafana on their own |
| `./infra/stop-grafana.sh` | Stop the observability overlay only |
| `./infra/load-test.sh [n]` | Create and trigger *n* jobs through the API (default 100) |

Both start scripts are idempotent: re-running one converges the stack on that mode,
including standing down the other mode's workers.

### Where things listen

| Service | URL | Notes |
|---|---|---|
| Frontend | http://localhost:3000 | |
| API | http://localhost:8000 | OpenAPI UI at `/docs` |
| Traefik | https://dass.localhost:8443 | Public entrypoint, TLS; `:8080` redirects here |
| Grafana | http://localhost:3001 | `/d/dass-overview` (mode 1), `/d/dass-k8s` (mode 2) |
| Prometheus | http://localhost:9090 | |
| cAdvisor | http://localhost:8081 | Per-container CPU and memory |

`*.localhost` resolves to the loopback address automatically — no `/etc/hosts` entry
is needed. To make the browser trust the local TLS certificate, add
`infra/traefik/pki/rootCA.crt` to your trust store (on macOS: open it and mark it
trusted in Keychain Access).

---

## Architecture

> [`docs/architecture.md`](docs/architecture.md) has the full set — logical
> architecture, both deployment topologies, the task lifecycle sequence and the state
> machine — as PlantUML sources with rendered PNGs, each mapped to the code that
> implements it. Start there when returning to this codebase after a break.

```mermaid
flowchart LR
  Browser[Browser]
  Traefik[Traefik\n:8443]
  Frontend[Next.js frontend]
  API[FastAPI API server]
  Scheduler[Scheduler]
  Worker[Workers]
  PgBouncer[(PgBouncer)]
  DB[(PostgreSQL primary)]
  Replica[(PostgreSQL replica)]
  Queue[(SQS: normal / scheduled / retry)]

  Browser --> Traefik
  Traefik --> Frontend
  Traefik -->|/api, /health, /metrics, /docs| API
  Frontend -->|server-side rewrites| API
  API --> PgBouncer
  API -->|reads| Replica
  API -->|enqueue| Queue
  Scheduler -->|direct, for advisory lock| DB
  Scheduler -->|enqueue| Queue
  Queue --> Worker
  Worker --> PgBouncer
  PgBouncer --> DB
```

**Three queues, not one.** `normal` carries on-demand runs, `scheduled` carries cron
dispatches, `retry` carries re-runs. Each has its own worker pool, so a cron backlog
cannot starve a manual trigger, and each scales on its own queue depth.

**PgBouncer sits in front of the primary** for the API, workers and autoscaler. The
scheduler is the one exception and connects directly: its leader election holds a
*session-level* advisory lock, which PgBouncer's transaction pooling would hand back
to the pool at commit and silently drop.

**Reads go to the replica** unless the session has just written. Read-after-write
paths pin themselves to the primary so replication lag can never hide a row the same
request created.

**Every task runs in its own container.** A job's `action_type` and `action_config`
are compiled into a `runtime_spec` at create/update time, and the worker executes
that spec without re-interpreting it — as a `docker run` in mode 1, or as a
Kubernetes Job in mode 2.

---

## Repository layout

```text
dass/
  backend/
    app/
      api/            FastAPI routers
      core/           settings and logging
      db/             engines, read/write routing session
      models/         SQLAlchemy models
      queue/          SQS and in-memory queue clients
      repositories/   data access
      scheduler/      cron scheduler and dependency scheduler
      services/       job, worker, execution, autoscaler, VM services
    alembic/          migrations
    tests/            unit tests; tests/integration needs Docker
  frontend/           Next.js dashboard
  infra/
    lib.sh            shared helpers for the scripts below
    start-mode1.sh    start-mode2.sh    stop-all.sh    down-all.sh
    start-grafana.sh  stop-grafana.sh   load-test.sh
    k8s/              namespace, RBAC, deployments, KEDA ScaledObjects
    observability/    Prometheus, Grafana, exporters
    postgres/         primary and replica bootstrap
    traefik/          dynamic config and local PKI
  scripts/            e2e_smoke.py, load generators, integration test runner
  docs/               architecture.md and the PlantUML diagrams it embeds
  docker-compose.yml               base stack
  docker-compose.local.yml         dev overlay: source mounts, published ports
  docker-compose.observability.yml Prometheus + Grafana overlay
```

---

## Mode 1 — everything in Docker Compose

The default. Database, queue, API, scheduler, frontend, worker and autoscaler all run
as Compose services. One worker process consumes all three queues, with its own
concurrency budget per queue.

```bash
./infra/start-mode1.sh
```

The script pre-pulls the job runner images, brings the stack up, waits for the API to
report healthy, and starts the observability overlay.

Scale workers by hand:

```bash
docker compose up -d --scale worker=3
```

The `autoscaler` service also adjusts the fleet on its own — see
[Worker scaling](#worker-scaling).

### Doing it manually

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.local.yml \
               -f docker-compose.observability.yml up -d
curl http://localhost:8000/health
# {"status":"ok","service":"dass"}
```

> Always pass the same set of `-f` files. Compose compares a running container
> against the config it computes now, so a different combination silently recreates
> `postgres` and `localstack` — which, on a first run, interrupts initdb partway
> through the replication setup. `infra/lib.sh` exists to keep that set in one place.

---

## Mode 2 — Compose infrastructure with Kubernetes workers

The database, queue, API, scheduler and frontend stay in Compose. Workers move to
Kubernetes, one Deployment per queue, each scaled independently by its own KEDA
ScaledObject from that queue's depth. Task containers become Kubernetes Jobs.

**Prerequisites:** Docker, [minikube](https://minikube.sigs.k8s.io/docs/start/),
[kubectl](https://kubernetes.io/docs/tasks/tools/), [helm](https://helm.sh/docs/intro/install/).
The script installs the last three automatically on Linux; set
`DASS_NO_AUTO_INSTALL=1` to install them yourself.

```bash
./infra/start-mode2.sh
```

It runs eight steps, each of which you can also run by hand:

**1. Compose infrastructure, without workers**

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.local.yml \
               -f docker-compose.observability.yml up -d \
  traefik-pki traefik postgres postgres-replica pgbouncer localstack \
  api-server scheduler frontend
docker compose stop worker autoscaler   # in case mode 1 was running
```

**2. Minikube**

```bash
minikube start --nodes 2 --driver docker --cpus 2 --memory 2048 --kubernetes-version stable
kubectl wait --for=condition=Ready nodes --all --timeout=120s
```

**3. KEDA and kube-state-metrics**

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install keda kedacore/keda \
  --namespace keda --create-namespace --wait --timeout 3m
helm upgrade --install kube-state-metrics prometheus-community/kube-state-metrics \
  --namespace kube-system --wait --timeout 3m
```

**4. Build the images**

```bash
docker build -t dass-api:local       -f backend/Dockerfile.api       backend/
docker build -t dass-scheduler:local -f backend/Dockerfile.scheduler backend/
docker build -t dass-worker:local    -f backend/Dockerfile.worker    backend/
```

**5. Push them into every Minikube node**

```bash
for image in dass-api:local dass-scheduler:local dass-worker:local alpine:3 curlimages/curl:8.6.0; do
  for node in $(minikube node list | awk '{print $1}'); do
    docker save "$image" | minikube ssh -n "$node" --native-ssh=false -- docker load
  done
done
```

> Not `minikube image load`. When the tag already exists on a node it leaves the old
> image in place, even with `--overwrite=true` — so re-running the script after a code
> change redeploys the *previous* build and nothing tells you. Piping `docker save`
> into each node's daemon replaces the tag every time.

**6. Apply the manifests**

```bash
kubectl apply -f infra/k8s/
for d in dass-api dass-scheduler dass-worker-normal dass-worker-scheduled dass-worker-retry; do
  kubectl rollout status deployment/$d -n dass --timeout=180s
done
```

**7. Expose kube-state-metrics to the Compose Prometheus**

```bash
kubectl -n kube-system port-forward --address=0.0.0.0 \
  service/kube-state-metrics-nodeport 30091:8080
```

**8. Observability**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml \
               -f docker-compose.observability.yml up -d \
  prometheus grafana cadvisor postgres-exporter sqs-exporter
curl -X POST http://localhost:9090/-/reload
```

### Checking it

```bash
docker compose ps                       # Compose services
kubectl get pods -n dass                # api, scheduler, three worker pools
kubectl get scaledobject -n dass        # READY should be True for all three
curl http://localhost:8000/health
```

> **Job URLs in mode 2.** A job whose `action_config.url` points at this machine must
> use `http://host.minikube.internal:8000`. `api-server` is a Compose hostname and
> Kubernetes pods cannot resolve it.

### Switching between modes

The start scripts handle this: `start-mode1.sh` scales the Kubernetes workers to zero,
and `start-mode2.sh` stops the Compose worker and autoscaler. By hand:

```bash
# mode 2 -> mode 1
kubectl scale deployment dass-worker-normal dass-worker-scheduled dass-worker-retry \
  --replicas=0 -n dass
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d worker autoscaler

# mode 1 -> mode 2
docker compose stop worker autoscaler
kubectl scale deployment dass-worker-normal dass-worker-scheduled dass-worker-retry \
  --replicas=1 -n dass
```

### After a Minikube restart

```bash
minikube start
helm upgrade --install keda kedacore/keda --namespace keda --create-namespace --wait --timeout 3m
kubectl apply -f infra/k8s/
```

Rebuild and reload the images too if the code changed (steps 4 and 5). Simply
re-running `./infra/start-mode2.sh` does all of this.

---

## Verifying the stack

`scripts/e2e_smoke.py` checks every component and then drives one job through the full
pipeline: create → trigger → claim → execute → result. It works in both modes and
needs no third-party packages.

```bash
python3 scripts/e2e_smoke.py
python3 scripts/e2e_smoke.py --api http://localhost:8000   # bypass Traefik
python3 scripts/e2e_smoke.py --insecure                    # skip TLS verification
```

A healthy run ends with `all green — stack is end-to-end healthy`. When something is
broken it names the stage that failed and where to look.

Quick manual checks:

```bash
curl http://localhost:8000/health    # {"status":"ok","service":"dass"}
curl http://localhost:8000/metrics   # {"jobs":N,"tasks":N}
```

### Demo and load scripts

Everything in `scripts/` is runnable against a live stack. The generators need `httpx`
or a database URL, so run them through the backend environment (`cd backend` first, or
prefix with `uv run --project backend`).

| Script | What it exercises |
|---|---|
| `e2e_smoke.py` | Every component, then one job end to end. Start here. |
| `dag_demo.py` | An A→C, B→C diamond: both upstreams must succeed before C fires, and C fires exactly once. |
| `load_gen.py` | Bulk create and trigger through the HTTP API — the real queue and worker path. |
| `sched_gen.py` | Cron jobs, so the scheduler dispatch path and the `scheduled` queue carry load. |
| `retry_gen.py` | Jobs that always exit non-zero, to drive the retry queue and `final_failed`. |
| `load_gen_scheduler.py` | Bulk cron jobs written straight to the database, for scheduler-only load. |

```bash
# Watch a DAG chain itself
cd backend && DASS_DATABASE_URL=postgresql+psycopg://dass:dass@localhost:5432/dass \
  DASS_SQS_ENDPOINT_URL=http://localhost:4566 \
  uv run python ../scripts/dag_demo.py

# Push real load through the API
./infra/load-test.sh 400
```

---

## Observing the stack

### Every container at a glance

```bash
docker compose ps                                   # status and health of each service
docker compose ps --format "table {{.Name}}\t{{.Status}}"
docker stats                                        # live CPU / memory / IO per container
docker compose top                                  # processes inside each container
```

### One service at a time

```bash
docker compose logs -f                # everything, interleaved
docker compose logs -f api-server     # HTTP requests, validation errors
docker compose logs -f scheduler      # leader election, dispatch counts, chaining
docker compose logs -f worker         # claim, execute, retry decisions
docker compose logs -f postgres       # slow queries, connection limits
docker compose logs -f postgres-replica   # replication bootstrap and lag
docker compose logs -f pgbouncer      # pool saturation
docker compose logs -f localstack     # SQS
docker compose logs -f traefik        # routing and TLS
docker compose logs -f frontend
docker compose logs --tail 100 worker # last 100 lines instead of following
```

Set `DASS_LOG_LEVEL=DEBUG` in `.env` and restart for verbose output.

Inside a container:

```bash
docker compose exec api-server sh
docker compose exec postgres psql -U dass -d dass -c "SELECT status, count(*) FROM tasks GROUP BY status"
docker compose exec postgres psql -U dass -d dass -c "SELECT client_addr, state, sync_state FROM pg_stat_replication"
```

### Mode 2: the Kubernetes side

```bash
kubectl get pods -n dass -o wide                     # which node each pod is on
kubectl logs -n dass -l app=dass-worker-normal -f    # follow a whole worker pool
kubectl logs -n dass -l app=dass-scheduler --tail=100
kubectl describe pod -n dass <pod>                   # events: pulls, probes, evictions
kubectl top pods -n dass                             # needs metrics-server
kubectl get jobs -n dass                             # the per-task Jobs workers create
kubectl get scaledobject,hpa -n dass                 # KEDA's view of queue depth
watch -n3 'kubectl get pods -n dass | grep worker'   # scaling in real time
```

### Grafana

http://localhost:3001 — anonymous admin is enabled, so no login. Two dashboards:

- **DASS · Overview** (`/d/dass-overview`) — the Compose stack. Job and task counts,
  tasks pending and running per logical queue, task outcomes (enqueued, succeeded,
  failed runs, retries, final failures), scheduler dispatch rate, plus per-container
  CPU and memory from cAdvisor and connection and throughput stats from
  postgres-exporter.
- **DASS · Kubernetes** (`/d/dass-k8s`) — mode 2. Worker pod count per queue, KEDA's
  computed target, and SQS visible versus in-flight messages per queue.

The pending/running panels are read from the database rather than from SQS on purpose:
a task is usually consumed within one scrape interval, so the SQS gauge reads zero and
hides the real load.

To watch a load test: start the observability overlay, open **DASS · Overview**, then
run `./infra/load-test.sh 400` and watch queue depth rise and drain.

### Prometheus and cAdvisor

```bash
# Are all scrape targets healthy?
curl -s http://localhost:9090/api/v1/targets | python3 -c "
import sys, json
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(t['labels']['job'], '-', t['health'])
"
```

Expect `prometheus`, `cadvisor`, `postgres` and `sqs` to be up. The `kubernetes`
target is down whenever Minikube is not running, which is normal in mode 1.

Raw per-container metrics are at http://localhost:8081 (cAdvisor) and the query UI at
http://localhost:9090.

---

## Working with jobs

Create a one-time job — no cron, so it runs as soon as it is created:

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"name":"hello","action_type":"shell",
       "action_config":{"command":"echo hi","timeout_seconds":30}}'
```

Create a cron job — the scheduler owns it from then on:

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"name":"every-minute","cron_expression":"* * * * *","action_type":"shell",
       "action_config":{"command":"echo tick","timeout_seconds":30}}'
```

Chain jobs into a DAG with `upstream_job_ids` / `downstream_job_ids`. A downstream job
runs once **all** of its upstreams have succeeded, and exactly once per generation of
upstream completions. Cycles are rejected at create and update time.

`scripts/dag_demo.py` builds a small A→C, B→C diamond and watches the chain fire.

### API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/jobs` | Create a job |
| `GET` | `/api/v1/jobs` | List jobs — paged and filterable (`page`, `page_size`, `enabled`, `action_type`, `concurrency_policy`, `q`) |
| `GET` | `/api/v1/jobs/{id}` | Get one job |
| `PUT` | `/api/v1/jobs/{id}` | Update a job |
| `DELETE` | `/api/v1/jobs/{id}` | Delete a job |
| `POST` | `/api/v1/jobs/{id}/trigger` | Run a job now |
| `GET` | `/api/v1/jobs/{id}/tasks` | Run history for a job |
| `GET` | `/api/v1/tasks/{id}` | One task, with stdout and stderr |
| `POST` | `/api/v1/tasks/{id}/retry` | Re-run a failed task |
| `GET` | `/health` | Liveness, including a database round trip |
| `GET` | `/metrics` | Job and task counts (JSON, not Prometheus format) |
| `POST` | `/vms` | Start worker containers by hand — **disabled by default** |

Interactive documentation lives at http://localhost:8000/docs and
https://dass.localhost:8443/docs.

> **This API has no authentication.** Combined with `action_type: "shell"`, anyone who
> can reach port 8000 can run arbitrary commands in a container on this host. Keep it
> on a trusted network, and set `DASS_SHELL_EXECUTION_ENABLED=false` to reject new
> shell jobs anywhere that is not your own machine. `POST /vms` is gated separately
> behind `DASS_VM_ADMIN_API_ENABLED` because it starts containers through the host
> Docker socket.

---

## Worker scaling

**Mode 1** — the `autoscaler` service reads the total depth of all three queues
(waiting plus in-flight) and sizes the worker fleet at
`ceil(depth / 20)`, clamped to `[1, 10]`. It clones the baseline worker container and
labels the copies `com.dass.autoscaled=true`, so scale-down never touches the
original. If a load test leaves strays behind:

```bash
docker ps -q --filter "label=com.dass.autoscaled=true" | xargs -r docker kill
```

**Mode 2** — KEDA owns scaling and the autoscaler service disables itself. Each queue
has its own ScaledObject targeting its own Deployment:

| ScaledObject | Deployment | Queue |
|---|---|---|
| `dass-worker-normal` | `dass-worker-normal` | `dass-tasks-normal` |
| `dass-worker-scheduled` | `dass-worker-scheduled` | `dass-tasks-scheduled` |
| `dass-worker-retry` | `dass-worker-retry` | `dass-tasks-retry` |

`desired = ceil(queue_depth / 20)`, clamped to `[1, 3]`. The ceiling is 3 rather than
10 because each worker pod also creates job pods; three queues at three workers with
two concurrent tasks each is already 18 job pods on a two-node Minikube. Raise it
alongside the node count, not on its own.

```bash
kubectl get scaledobject -n dass
kubectl get hpa -n dass
watch -n3 'kubectl get pods -n dass | grep worker'
```

---

## Load and stress testing

Start the observability overlay first so you can watch queue depth, worker throughput
and database pressure while the test runs.

```bash
./infra/load-test.sh 400
```

That wraps `scripts/load_gen.py`, which drives the full HTTP → API → database → queue
path rather than writing to the database directly, so queue depth and worker behaviour
are real. For finer control:

```bash
cd backend
uv run python ../scripts/load_gen.py --count 1000 --concurrency 64 --trigger
```

| Flag | Meaning |
|---|---|
| `--count N` | jobs to create (default 1000) |
| `--concurrency N` | parallel in-flight HTTP requests (default 32) |
| `--trigger` | after creating, fire each job once via `/trigger` |
| `--api URL` | API base URL (default `https://dass.localhost:8443`) |
| `--insecure` | skip TLS verification |

Watch the effect on **DASS · Overview**, or in mode 2 on **DASS · Kubernetes**
alongside `watch -n3 'kubectl get pods -n dass | grep worker'`. The other generators
in [Demo and load scripts](#demo-and-load-scripts) put load on specific paths — the
scheduler, or the retry queue.

### Resetting between runs

```bash
docker compose stop                  # pause, keep everything
./infra/stop-all.sh                  # stop the stack and Minikube, keep the data
./infra/down-all.sh                  # stop and delete every volume
docker compose up -d --build         # rebuild images after a dependency change

# Clear generated jobs and tasks without a full teardown
docker compose exec postgres psql -U dass -d dass -c "TRUNCATE tasks, job_dependencies, jobs CASCADE"

# Drop autoscaled workers a load test left behind (mode 1)
docker ps -q --filter "label=com.dass.autoscaled=true" | xargs -r docker kill
```

---

## The observability overlay

Prometheus, Grafana, cAdvisor, postgres-exporter and the SQS exporter live in
`docker-compose.observability.yml` so the dev stack stays lean. Both start scripts
bring it up; on its own:

```bash
./infra/start-grafana.sh    # start Prometheus + Grafana + exporters
./infra/stop-grafana.sh     # stop them, leave the rest running
```

Reading these dashboards is covered in [Observing the stack](#observing-the-stack).

> **cAdvisor needs a raised inotify limit** on Linux. If it restarts in a loop with
> `inotify_init: too many open files`:
> ```bash
> sudo sysctl fs.inotify.max_user_instances=8192
> sudo sysctl fs.inotify.max_user_watches=524288
> ```

---

## Testing

### Unit tests

SQLite and an in-memory queue — no Docker, no environment setup. `tests/integration`
is excluded automatically.

```bash
cd backend
uv sync --extra dev
uv run pytest
```

### Integration tests

Real PostgreSQL and LocalStack. The runner starts throwaway containers on their own
ports (55432 and 14566) so it can coexist with a running dev stack, applies the
migrations, and runs the suite.

```bash
scripts/run_integration_tests.sh                # start containers, migrate, run
scripts/run_integration_tests.sh up             # start and migrate only
scripts/run_integration_tests.sh down           # remove the test containers
scripts/run_integration_tests.sh test -k retry  # extra arguments go to pytest
```

Containers stay up between runs for fast iteration; use `down` when you are finished.
The run also writes `backend/test-reports/integration.html`.

### Frontend

```bash
cd frontend
npm ci
npm run typecheck     # tsc --noEmit
npm run format:check  # prettier
npm test              # vitest
npm run build         # catches what tsc alone does not
```

### CI

| Workflow | Trigger | What it runs |
|---|---|---|
| `backend-ci.yml` | `backend/**` | the whole unit suite |
| `frontend-ci.yml` | `frontend/**` | typecheck, format check, vitest, production build |
| `integration-ci.yml` | every PR | integration suite against real services |

You can reproduce the workflows locally with [`act`](https://nektosact.com). `.actrc`
and `.actignore` are picked up automatically and point `--env-file` at `/dev/null`, so
your local `.env` cannot shadow a workflow's own `env:` block. No runner image is
pinned, so pass one with `-P`.

```bash
act pull_request -W .github/workflows/backend-ci.yml \
  -P ubuntu-latest=catthehacker/ubuntu:act-latest
```

The integration workflow publishes a LocalStack container on host port 4566, which the
dev stack already owns. Bring the dev stack down first:

```bash
./infra/stop-all.sh
act workflow_dispatch -W .github/workflows/integration-ci.yml \
  -P ubuntu-latest=catthehacker/ubuntu:act-latest
```

> The final **Upload test results** step fails under `act` with
> `Unable to get the ACTIONS_RUNTIME_TOKEN env variable`. That is harmless — artifact
> upload needs a GitHub-hosted service and the tests have already run. Add
> `--artifact-server-path /tmp/act-artifacts` to silence it.

---

## Database migrations

Migrations run automatically when the API server starts (`entrypoint.sh` calls
`alembic upgrade head`). Concurrent starters are serialised on a PostgreSQL advisory
lock, so scaling the API to several replicas upgrades the schema exactly once.

```bash
docker compose exec api-server alembic upgrade head   # run by hand
cd backend && uv run alembic revision -m "add something"
```

---

## Development workflow

`docker-compose.local.yml` mounts `backend/` into the containers, so backend changes
take effect without rebuilding. Both start scripts already include it.

The frontend deliberately runs its production build even in the dev overlay: Next.js
dev mode inside a container needs more inotify watches than most hosts allow. For
frontend hot reload, run it on the host instead:

```bash
docker compose up -d postgres postgres-replica pgbouncer localstack api-server scheduler worker
cd frontend && npm install && npm run dev
```

To run the backend on the host as well (for a debugger, say):

```bash
docker compose up -d postgres postgres-replica localstack

cd backend
uv sync --extra dev
DASS_DATABASE_URL=postgresql+psycopg://dass:dass@localhost:5432/dass \
DASS_SQS_ENDPOINT_URL=http://localhost:4566 \
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Outside Docker the hostnames change: `localhost` instead of `postgres` and
`localstack`. `DASS_REPLICA_DATABASE_URL` is left unset above because the replica does
not publish a host port, so reads fall back to the primary — and the database layer
then reuses a single engine rather than opening a second pool to the same server. To
exercise the read/write split, publish `postgres-replica` on 5433 and point the
variable at it.

### Configuration

Every setting is an environment variable with a `DASS_` prefix; see `.env.example`.
Two are deliberately left unset there, because `env_file` would apply them to every
service at once and break the per-service split:

- `DASS_DATABASE_URL` — Compose points the API, worker and autoscaler at PgBouncer and
  the scheduler at PostgreSQL directly.
- `DASS_WORKER_ID` — identifies each process, and the database layer sizes its
  connection pool from it.

### Load balancing the API

Traefik spreads traffic across every `api-server` replica:

```bash
docker compose up -d --scale api-server=3
```

Keep the frontend at one replica unless you mean to scale it. For a real deployment,
replace the internal CA with Traefik's ACME/Let's Encrypt support and a real DNS name;
the setup here gives production-style TLS semantics without public certificate issuance.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `connection refused` on port 8000 | API still starting, or crashed | `docker compose logs api-server` |
| API restarts in a loop | Database or LocalStack not ready | Wait — healthchecks handle ordering. If it persists: `./infra/down-all.sh && ./infra/start-mode1.sh` |
| `postgres-replica` restarting with `no pg_hba.conf entry for replication` | The primary's init script did not run, so the replication role is missing | The volume is half-initialised: `./infra/down-all.sh` and start again |
| The first job fails with `Container execution timed out` | A cold image pull outlasted the job's own timeout | The start scripts pre-pull the runner images; otherwise `docker pull alpine:3 curlimages/curl:8.6.0` |
| `certificate verify failed` against `dass.localhost:8443` | Stale local CA | `rm infra/traefik/pki/*.crt infra/traefik/pki/*.key` and restart, or use `--insecure` |
| A code change does not show up in mode 2 | `minikube image load` kept the old image | Use the `docker save \| minikube ssh -- docker load` loop in step 5 |
| Kubernetes Job: `Could not resolve host: api-server` | Compose hostnames are invisible to pods | Use `http://host.minikube.internal:8000` in `action_config.url` |
| Worker pods `CrashLoopBackOff` right after starting | Kubernetes service-discovery env vars overwrote a `DASS_*` variable | Confirm the pod spec has `enableServiceLinks: false` |
| Worker pods stuck `Pending` | The nodes cannot fit the requested resources | Lower `maxReplicaCount` in `infra/k8s/09-keda-scaledobject.yaml`, or give Minikube more memory |
| cAdvisor restarting with `inotify_init: too many open files` | The host's inotify limit is too low | See [the observability overlay](#the-observability-overlay) |

---

## Design notes

- **PostgreSQL is the source of truth.** SQS is only a delivery mechanism; losing a
  message costs a delay, not a record.
- **Workers claim tasks atomically** with a conditional `UPDATE ... WHERE
  status = 'pending'`, so two workers can never run the same task.
- **A short visibility timeout with a heartbeat.** While a task runs, its worker
  extends the database lock and the queue message visibility together, so a crashed
  worker loses both at once: the message reappears and the scheduler can reclaim the
  row within one window, however long the job was.
- **The scheduler elects a leader** with a PostgreSQL advisory lock. Leadership is
  tied to the connection, so a dead leader releases it automatically and a standby
  takes over on its next attempt.
- **The scheduler keeps a heap** of upcoming firings, refreshed incrementally.
  Superseded entries are not removed but recognised and skipped on pop, which keeps
  each sync cheap.
- **Shell execution is supported** for local and internal use. It is dangerous on any
  reachable deployment; see the warning under [API](#api).
