# DASS

`DASS` is a Distributed Asynchronous Scheduling System: a production-style MVP for creating, scheduling, dispatching, and observing internal jobs.

## Stack

- Backend: Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic, boto3, httpx, croniter
- Frontend: Next.js, React, TypeScript, TanStack Query, Tailwind CSS
- Proxy: Traefik
- Database: PostgreSQL
- Queue: AWS SQS, with LocalStack for local development

## Quick Commands

```bash
# 啟動模式一（純 Docker Compose，含 worker）
./infra/start-mode1.sh

# 啟動模式二（Docker Compose 基礎服務 + K8s Worker，KEDA 自動擴縮）
./infra/start-mode2.sh

# 完全關閉並清除所有服務（Docker Compose + Minikube，會刪除資料卷）
./infra/down-all.sh

# 開啟 Grafana 監控
./infra/start-grafana.sh          # → http://localhost:3001

# 關閉 Grafana
./infra/stop-grafana.sh

# 壓力測試（可指定 job 數，預設 100）
./infra/load-test.sh 200
```

---

## Architecture

```mermaid
flowchart LR
  Browser[Browser]
  Traefik[Traefik Reverse Proxy\n:80]
  Frontend[Next.js Frontend\ninternal :3000]
  API[FastAPI API Server\ninternal :8000]
  Scheduler[Scheduler Worker]
  Worker[Job Worker]
  DB[(PostgreSQL)]
  Queue[(AWS SQS / LocalStack)]
  Browser --> Traefik
  Traefik --> Frontend
  Traefik -->|/api, /health, /metrics, docs| API
  Frontend -->|server-side API rewrites| API
  API --> DB
  API --> Queue
  Scheduler --> DB
  Scheduler --> Queue
  Worker --> DB
  Worker --> Queue
  Queue --> Worker
```

Traefik is the public entrypoint. It load-balances the frontend and API containers, while the frontend still uses internal rewrites for server-side requests. The backend owns persistence in PostgreSQL, and scheduling/worker processes coordinate job dispatch and execution through the queue.

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

## 啟動方式

DASS 支援兩種 worker 執行模式，依需求選擇其中一種。

---

### 模式一：純 Docker Compose（本機開發，推薦）

所有服務（DB、queue、backend、frontend、worker）都在 Docker Compose 裡，一行指令搞定。Worker 在單一 process 內同時消費 normal / scheduled / retry 三條 queue，手動 `--scale` 控制數量。

```bash
./infra/start-mode1.sh
```

啟動完成後：
- Frontend：http://localhost:3000
- API：http://localhost:8000
- API docs：http://localhost:8000/docs

手動調整 worker 數量：

```bash
docker compose up --scale worker=3
```

---

### 模式二：Docker Compose + Kubernetes Worker（KEDA 自動擴縮）

其他服務跑在 Docker Compose，worker 改由 Kubernetes 管理。三條 queue（normal / scheduled / retry）各有一個獨立的 worker Deployment，由 KEDA 根據各自的 queue 深度自動擴縮（1–10 pods，每 20 條訊息對應 1 個 pod）。

**Prerequisites：**
- Docker
- [minikube](https://minikube.sigs.k8s.io/docs/start/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [helm](https://helm.sh/docs/intro/install/)

```bash
./infra/start-mode2.sh
```

腳本會依序執行以下步驟（也可手動逐步跑）：

---

**Step 1：啟動 Docker Compose 基礎服務（不含 worker）**

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d \
  postgres postgres-replica localstack api-server scheduler frontend
```

等 api-server healthy（約 10–20 秒）：

```bash
curl http://localhost:8000/health
# 預期：{"status":"ok","service":"dass"}
```

如果你也有啟動 Traefik，則可以改用公開入口 `https://localhost:8443`；本機憑證鏈會放在 `infra/traefik/pki/rootCA.crt`。

---

**Step 2：啟動 Minikube（2 節點）**

```bash
minikube start \
  --nodes 2 \
  --driver docker \
  --cpus 2 \
  --memory 2048 \
  --kubernetes-version stable
```

確認節點 Ready：

```bash
kubectl get nodes
# NAME           STATUS   ROLES           AGE
# minikube       Ready    control-plane   ...
# minikube-m02   Ready    <none>          ...
```

---

**Step 3：安裝 KEDA**

```bash
helm repo add kedacore https://kedacore.github.io/charts 2>/dev/null || true
helm repo update kedacore
helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --wait \
  --timeout 3m
```

確認 KEDA pods 都 Running：

```bash
kubectl get pods -n keda
# keda-operator、keda-admission-webhooks、keda-operator-metrics-apiserver 都 1/1 Running
```

---

**Step 4：Build Docker images**

```bash
docker build -t dass-api:local       -f backend/Dockerfile.api       backend/
docker build -t dass-scheduler:local -f backend/Dockerfile.scheduler  backend/
docker build -t dass-worker:local    -f backend/Dockerfile.worker     backend/
```

---

**Step 5：Load images 進 minikube（三個平行跑，比較快）**

```bash
minikube image load dass-api:local       --overwrite=true &
minikube image load dass-scheduler:local --overwrite=true &
minikube image load dass-worker:local    --overwrite=true &
wait
```

確認 images 已進入 minikube：

```bash
minikube image ls | grep dass-
# docker.io/library/dass-api:local
# docker.io/library/dass-scheduler:local
# docker.io/library/dass-worker:local
```

---

**Step 6：部署所有 K8s manifests**

```bash
kubectl apply -f infra/k8s/
```

等待所有 Deployment 就緒（平行等，約 1–2 分鐘）：

```bash
kubectl rollout status deployment/dass-api              -n dass --timeout=180s &
kubectl rollout status deployment/dass-scheduler        -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-normal    -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-scheduled -n dass --timeout=120s &
kubectl rollout status deployment/dass-worker-retry     -n dass --timeout=120s &
wait
```

---

**Step 7：確認所有服務正常**

```bash
# Docker Compose 服務（api, scheduler, postgres, localstack, frontend 都應該 Up/healthy）
docker compose ps

# K8s — 三個 worker pool 各自 1/1 Running
kubectl get pods -n dass | grep worker

# KEDA ScaledObjects 的 READY 欄位應顯示 True
kubectl get scaledobject -n dass

# API 健康確認
curl http://localhost:8000/health
# {"status":"ok","service":"dass"}
```

**存取前端：**

如果是 VS Code Remote SSH，需要在 VS Code 的 **PORTS** 面板轉發 port 3000 後，才能用本機瀏覽器開 `http://localhost:3000`。

> **注意**：模式二下，job 的 `action_config.url` 若要呼叫本機 API，需使用 `http://host.minikube.internal:8000`，不能用 `http://api-server:8000`（那是 Docker Compose 內部 hostname，K8s pod 無法解析）。

---

**關閉服務：**

```bash
# 停止 Docker Compose 服務（若 observability 也在跑，加上 -f docker-compose.observability.yml）
docker compose -f docker-compose.yml -f docker-compose.observability.yml down

# 停止 Minikube（保留 cluster 狀態，下次 minikube start 可快速恢復）
minikube stop

# 完全刪除 Minikube cluster
minikube delete
```

---

**Minikube 重開後快速恢復（dass namespace 消失但 cluster 還在）：**

```bash
# 重啟 Docker Compose 基礎服務
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d \
  postgres postgres-replica localstack api-server scheduler frontend

# 重啟 minikube
minikube start

# 重裝 KEDA（已裝則自動 upgrade，無副作用）
helm upgrade --install keda kedacore/keda --namespace keda --create-namespace --wait --timeout 3m

# Rebuild images + reload（若程式碼沒改可略過 build，只做 load）
docker build -t dass-api:local -f backend/Dockerfile.api backend/
docker build -t dass-scheduler:local -f backend/Dockerfile.scheduler backend/
docker build -t dass-worker:local -f backend/Dockerfile.worker backend/
minikube image load dass-api:local --overwrite=true &
minikube image load dass-scheduler:local --overwrite=true &
minikube image load dass-worker:local --overwrite=true &
wait

# 重新部署
kubectl apply -f infra/k8s/
```

---

### 切換 Worker 模式

**從模式二切換到模式一（Docker Compose worker）：**

```bash
# K8s worker 縮到 0（不刪 deployment，之後可以再恢復）
kubectl scale deployment dass-worker-normal dass-worker-scheduled dass-worker-retry \
  --replicas=0 -n dass

# 啟動 Docker Compose worker（一個 process 消費全部三條 queue）
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d worker
```

確認啟動：
```bash
docker compose logs worker | grep "started"
# Worker 'worker-1' started. pools=3, containers_per_pool=2, queues=['normal', 'scheduled', 'retry']
```

**從模式一切換回模式二（K8s worker）：**

```bash
# 停 Docker Compose worker
docker compose stop worker

# 恢復 K8s worker（KEDA 接管 replica 數量）
kubectl scale deployment dass-worker-normal dass-worker-scheduled dass-worker-retry \
  --replicas=1 -n dass
```

---

### 驗證服務與重置

```bash
# 健康檢查
curl http://localhost:8000/health
# {"status":"ok","service":"dass"}

# 查看 task 統計
curl http://localhost:8000/metrics
# {"jobs":0,"tasks":0}

# 填入範例資料
docker compose exec api-server python scripts/seed.py

# 停止所有 Docker Compose 服務
docker compose down

# 從頭重建（含清空資料庫）
docker compose down -v
docker compose up --build
```

## Viewing Logs

```bash
# All services at once (recommended)
docker compose logs -f

# Individual services
docker compose logs -f traefik
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
| K8s Job `curl: (6) Could not resolve host: api-server` | `api-server` 是 Docker Compose hostname，K8s pod 無法解析 | job 的 `action_config.url` 改用 `http://host.minikube.internal:8000` |
| cAdvisor 一直 restart（`inotify_init: too many open files`） | OS inotify instances 上限太低（預設 128） | `sudo sysctl fs.inotify.max_user_instances=8192` 後重啟 cadvisor |
| K8s worker pod 起來後馬上 CrashLoopBackOff | `DASS_*` env var 被 K8s service env var 覆蓋（`tcp://...` 格式） | 確認 `07-worker.yaml` 的 pod spec 有 `enableServiceLinks: false` |

## Database Migrations

Migrations run automatically when the API server starts (`entrypoint.sh` runs `alembic upgrade head`). To run manually:

```bash
docker compose exec api-server alembic upgrade head
```

## Worker Scaling

**Docker Compose 模式：** 手動指定 worker 數量

```bash
docker compose up --scale worker=3
```

**Kubernetes 模式（KEDA）：** 三條 queue 各有獨立的 Deployment，KEDA 各自根據 queue 深度自動擴縮（範圍 1–10 pods，每 20 條訊息 = 1 pod，scale-down 有 60 秒 cooldown）：

```bash
# 查看三個 worker pool 的 scaling 狀態
kubectl get scaledobject -n dass
kubectl get hpa -n dass

# 觀察 Pod 數量即時變化
watch -n5 'kubectl get pods -n dass | grep worker'
```

## Autoscaling

**Kubernetes 模式** 使用 KEDA（Kubernetes Event-Driven Autoscaler）作為唯一的 autoscaling 機制。每條 queue 有獨立的 ScaledObject，互不干擾：

| ScaledObject | 目標 Deployment | Queue |
|---|---|---|
| `dass-worker-normal` | `dass-worker-normal` | `dass-tasks-normal` |
| `dass-worker-scheduled` | `dass-worker-scheduled` | `dass-tasks-scheduled` |
| `dass-worker-retry` | `dass-worker-retry` | `dass-tasks-retry` |

公式：`desired_pods = ceil(queue_depth / 20)`，範圍 `[1, 10]`。

**Docker Compose 模式** 有一個 `autoscaler` container（`autoscaler_service.py`），透過 Docker daemon API 動態新增/移除 worker container。若壓測後留下殘餘 worker：

```bash
docker ps -q --filter "label=com.dass.autoscaled=true" | xargs -r docker kill
```

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

> **cAdvisor 前置條件：** cAdvisor 需要較高的 inotify 限制才能啟動。若 cAdvisor 一直 restart（`inotify_init: too many open files`），執行一次以下指令後再重啟：
> ```bash
> sudo sysctl fs.inotify.max_user_instances=8192
> sudo sysctl fs.inotify.max_user_watches=524288
> docker compose -f docker-compose.yml -f docker-compose.observability.yml restart cadvisor
> ```

確認 Prometheus 所有 scrape targets 正常：

```bash
curl -s http://localhost:9090/api/v1/targets | python3 -c "
import sys,json; d=json.load(sys.stdin)
for t in d['data']['activeTargets']:
    print(t['labels']['job'], '-', t['health'])
"
# cadvisor - up
# postgres - up
# prometheus - up
# sqs - up
```

Tear it down, including the metrics volumes:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml down -v
```

## Load / Stress Testing

先啟動 observability overlay，再執行壓測，這樣可以在 Grafana `http://localhost:3001/d/dass-overview` 即時觀察 queue depth、worker throughput、DB 壓力。

`scripts/load_gen.py` 走完整的 HTTP → API → DB → queue 路徑（非直接寫 DB），queue depth 和 worker 反應都是真實的：

```bash
# 先啟動 observability
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d

# 執行壓測（從 backend/ 目錄執行，確保使用 .venv 裡的 httpx）
cd backend
.venv/bin/python ../scripts/load_gen.py --count 1000 --concurrency 32 --trigger
```

| Flag | Meaning |
|------|---------|
| `--count N` | number of jobs to create (default 1000) |
| `--concurrency N` | parallel in-flight HTTP requests (default 32) |
| `--trigger` | after creating, fire each job once via `/trigger` |
| `--api URL` | API base URL (default `https://localhost:8443`) |

`load_test.sh` 會沿用這個預設值；如果你只啟動了純 Docker Compose 的 API，而沒有 Traefik，請加上 `--api http://localhost:8000` 自行覆蓋。

**Kubernetes 模式下觀察 KEDA 擴縮：**

```bash
# 另開一個 terminal 持續觀察 worker pod 數量
watch -n3 'kubectl get pods -n dass | grep worker'

# 查看 HPA 目前 metric 值
kubectl get hpa -n dass
```

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

Interactive API documentation is available at `https://localhost:8443/docs` when the stack is running.

To trust the local CA on macOS, open `infra/traefik/pki/rootCA.crt` and add it to your login keychain as a trusted root certificate.

### Load Balancing the API

Traefik can distribute traffic across multiple `api-server` replicas. After the stack is up, scale the API service and Traefik will spread requests across the running containers:

```bash
docker compose up -d --scale api-server=3
```

Keep the frontend at a single replica unless you also want to scale it intentionally; the public entrypoint remains `https://localhost:8443`.

For a real public deployment, replace the internal CA with Traefik ACME/Let’s Encrypt and a real DNS name. The compose setup here is meant for local and lab environments where you want production-style TLS semantics without public certificate issuance.

## Development Workflow (Optional)

If you want **hot reload** for backend code while all infrastructure (DB, LocalStack) stays in Docker, use the local override file:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

This mounts `backend/` source into the containers so code changes take effect immediately without rebuilding images.

> **Frontend note：** `docker-compose.local.yml` 的 frontend dev mode (Next.js dev server) 在 container 環境裡會遇到 OS inotify watch 數量不足的限制。目前預設走 production build。如需前端 hot reload，建議在 host 直接執行（見下方）。

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

Note: this minimal setup skips `postgres-replica`. With `DASS_REPLICA_DATABASE_URL` unset, the backend falls back to the primary DB for read paths, so read/write split behaviour is not exercised. If you need to test it, also start `postgres-replica` and set `DASS_REPLICA_DATABASE_URL` to point at it.

## Notes

- PostgreSQL is the source of truth.
- SQS is used only as a delivery mechanism.
- The Next.js frontend proxies API requests to the backend through rewrites, so the browser stays on one origin and avoids CORS issues.
- The scheduler runs every `DASS_SCHEDULER_INTERVAL_SECONDS` (default `5`) and is responsible for deciding when work should be dispatched.
- Each task executes inside an ephemeral Docker container spawned by the worker via the host Docker daemon; the job's `runtime_spec` (derived from `action_type` + `action_config`) decides image and command. Both `http` and `shell` action types compile down to this container model.
- Workers claim tasks atomically and report results back to PostgreSQL.
- The autoscaler watches queue depth and spawns/reaps extra workers — see the Autoscaling section above.
- Shell execution is supported for local and internal use, but it is dangerous in production and should be restricted carefully.
