# contributing.md

# PR Workflow Guide — Step by Step

這份指南用一個真實範例帶你走完從寫 code 到 merge 的完整流程。

---

## Step 0：確認你在 `main` 且是最新的

```bash
git checkout main
git pull origin main
```

開始寫任何東西之前，先確保你的 `main` 是最新狀態。

---

## Step 1：開一條 feature branch

```bash
git checkout -b feat/queue-and-ci
```

從 `main` 切出一條新 branch。命名規則：`<type>/<簡短描述>`，用 kebab-case。

常用 type：`feat/` `fix/` `refactor/` `test/` `ci/` `docs/` `chore/`

---

## Step 2：分批 commit，一個 commit 做一件事

不要把所有改動一次 `git add .` 然後一個 commit 帶走。按邏輯分組，每組一個 commit。

### 範例：這次 PR 共做了四件事，拆成四個 commit

**Commit 1 — 修 config**

```bash
git add backend/app/core/config.py
git commit -m "fix(config): resolve .env path absolutely"
```

**Commit 2 — 實作 queue**

```bash
git add backend/app/queue/
git commit -m "feat(queue): implement in-memory and SQS clients"
```

**Commit 3 — 重整 test 檔案結構**

```bash
git add backend/tests/test_queue.py
git rm backend/tests/test_jobs_api.py backend/tests/test_scheduler_and_worker.py
git add backend/tests/test_repositories.py backend/tests/test_api.py \
       backend/tests/test_scheduler.py backend/tests/test_worker.py
git commit -m "test: split monolithic suites into per-module files"
```

**Commit 4 — 加 CI workflow**

```bash
git add .github/workflows/backend-ci.yml
git commit -m "ci(backend): add per-module test workflow with paths-filter"
```

### Commit Message 格式

```
<type>(<scope>): <subject>
```

- 用祈使句（`add X`，不是 `added X`）
- 72 字元以內，結尾不加句號
- Type 和 scope 參考

---

## Step 3：Push 到 GitHub

```bash
git push -u origin feat/queue-and-ci
```

- `u origin` 設定 upstream，之後同一條 branch 直接 `git push` 就好。

---

## Step 4：開 PR

```bash
gh pr create --base main \
  --title "feat(queue): implement queue layer + per-module CI" \
  --body "## Summary
- Fix .env path resolution to use absolute paths
- Implement queue layer with in-memory and SQS clients
- Split monolithic test suites into per-module files
- Add per-module CI workflow with paths-filter

## Why
Building foundational infrastructure for the queue and CI pipeline.

## Test Plan
- [x] Local pytest passed
- [x] CI green"
```

- or 等發完 PR 再到 GitHub 裡面編輯 comment 也行

### PR Title 格式

跟 commit message 一樣：`<type>(<scope>): <subject>`

### PR Body 要包含

- **Summary** — 這個 PR 做了什麼（條列）
- **Why** — 為什麼要做
- **Test Plan** — 怎麼驗證的

---

## Step 5：等 CI 跑完

```bash
gh pr checks --watch
```

這會即時顯示所有 CI job 的狀態，跑完會自動結束：

```
✓  Backend CI/Test / Queue (pull_request)                    14s
-  Backend CI/Test / API (pull_request)                           ← skipped (沒改到)
-  Backend CI/Test / Repositories (pull_request)                  ← skipped
✓  Backend CI/Detect changed paths (pull_request)             4s
```

`✓` = 通過、`-` = 跳過（paths-filter 判定沒改到，正常）、`✗` = 失敗要修。

**CI 沒全綠不要 merge。**

---

## Step 6：Merge

`p.s. 這步可以不用做，我已經有設定好了`

```bash
gh pr merge --squash --delete-branch
```

- `-squash`：把 branch 上所有 commit 壓成一個，合進 `main`
- `-delete-branch`：merge 完自動刪掉遠端的 feature branch

Merge 後 `main` 上只會出現一個 commit，message 就是你的 PR title。

---

## 完整流程速查

```
main (pull latest)
  └─ git checkout -b feat/xxx
       ├─ commit 1: fix(config): ...
       ├─ commit 2: feat(queue): ...
       ├─ commit 3: test: ...
       └─ commit 4: ci(backend): ...
            └─ git push -u origin feat/xxx
                 └─ gh pr create
                      └─ CI passes ✓
                           └─ gh pr merge --squash --delete-branch
                                └─ main ← 一個 squash commit
```

---

## 常見問題

### CI 失敗了怎麼辦？

直接在同一條 branch 上修，commit + push，CI 會自動重跑：

```bash
# 修完後
git add .
git commit -m "fix(queue): correct import path"
git push
```

### 我想跑特定模組的 CI 但 paths-filter 沒觸發？

在 PR title 加 tag：

```
refactor(config): rename env vars [queue][api]
```

### Branch 跟 `main` 有衝突？

```bash
git checkout main
git pull origin main
git checkout feat/xxx
git rebase main
# 解完衝突後
git push --force-with-lease
```

### 可以直接 push 到 `main` 嗎？

不行。`main` 有 branch protection，會被擋下。一律走 PR。