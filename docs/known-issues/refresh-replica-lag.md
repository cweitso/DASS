# Known Issue：`refresh()` 撞 replica lag → `Could not refresh instance`

- **狀態：** Open（本 PR 已含暫時緩解 `force_primary`，待 DB owner 確認最終方案 + 補回歸測試）
- **影響範圍：** DB 讀寫分離路由（`RoutingSession`）／高並發建立 Task 的路徑
- **嚴重度：** 高 — 壓測下 `/trigger` 會大量回 500
- **發現於：** 壓力測試 `scripts/load_gen.py --count 10000 --concurrency 64 --trigger`

## 症狀

高並發觸發 `POST /api/v1/jobs/{id}/trigger` 時，api-server 大量噴 `InvalidRequestError: Could not refresh instance`。低流量觀察不到。

<details><summary>error log（節錄關鍵 frame）</summary>

```
sqlalchemy.exc.InvalidRequestError: Could not refresh instance '<Task at 0x...>'
  File "/app/app/api/v1/jobs.py", line 140, in trigger_job
    task = service.trigger_job(job_id, queue)
  File "/app/app/services/job_service.py", line 215, in trigger_job
    task = self.tasks.create(task)
  File "/app/app/repositories/task_repository.py", line 22, in create
    self.db.refresh(task)
  File ".../sqlalchemy/orm/session.py", line 3170, in refresh
    raise sa_exc.InvalidRequestError("Could not refresh instance ...")
```
</details>

## 重現方式

```bash
cd backend
.venv/bin/python ../scripts/load_gen.py --count 10000 --concurrency 64 --trigger
```

## 根因

1. `TaskRepository.create()` 在 **primary** commit 新 `Task` 後呼叫 `self.db.refresh(task)`。
2. `refresh()` 內部會發一條 `SELECT ... WHERE id = :id`；而 `RoutingSession.get_bind`（`backend/app/db/session.py`）把所有 `Select` 路由到 **read replica**。
3. 這筆剛 INSERT 的新 row 還沒被複寫到 replica（replication lag）→ replica 上查不到 → `SELECT` 回 0 筆 → SQLAlchemy 丟 `Could not refresh instance`。
4. 壓測 `10000 × 64` 把 replica lag 放大，「primary 已有、replica 還沒」的時間窗被狂命中，所以一壓就爆；低流量時 lag 小，看不到。

## 為什麼是 `refresh` 中招

`Session.get()` 不帶 `Select` clause，會落到 `get_bind` 的預設分支走 primary；但 `refresh()` 內部是一條 `Select`，會被路由到 replica。所以同樣是「讀剛寫入的 row」，`refresh` 特別容易踩雷。

## 現況 / 本 PR 的緩解

- 本 PR 已在 `create()` 加上 `self.db.info["force_primary"] = True`，理論上讓該 session 後續的 refresh 改走 primary。
- ⚠️ 仍待處理：
  1. **線上 image 需重 build** — error log 的 `task_repository.py:22` 仍是 `self.db.refresh(task)`，代表部署中的 image 跑的是還沒帶這行 fix 的舊碼。
  2. **`force_primary` 設了之後未被還原** — 目前靠「每個 request 開新 session、用完即丟」才沒事；若 session 被重用，會變成「一次寫入後整條 session 永遠走 primary」。建議收斂成用完自動還原的 context manager。
  3. **更乾淨的替代方案：直接移除 `create()` 的 `refresh`** — 兩個 caller（`trigger_job`、`scheduler._dispatch_job`）只用到 `task.id`（client 端 `uuid4` 產生）與 `task.status`（constructor 設定），都不需要回 DB 重讀；配合 `expire_on_commit=False`，commit 後這兩個值仍可存取。移除可同時消除此 bug 並省下每次建 Task 一條 SELECT。

## 待 DB owner 決策

- [ ] 拍板最終方案：A) 保留 `refresh` 並把 `force_primary` 收斂成自動還原；或 B) 移除 `create()` 的 `refresh`。
- [ ] 補上 read-after-write 路徑的回歸測試。
- [ ] image 重 build 後，用上面壓測指令驗證 0 錯誤。

## 相關程式

- `backend/app/repositories/task_repository.py` — `create()` / `refresh()` / `force_primary`
- `backend/app/db/session.py` — `RoutingSession.get_bind`（Select → replica 的路由）
- `backend/app/services/job_service.py:215` — `trigger_job` → `tasks.create`
- `backend/app/api/v1/jobs.py:140` — `/trigger` endpoint
