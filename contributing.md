# Contributing

`main` is protected — every change lands through a pull request. This walks the whole
loop once.

## 1. Start from an up-to-date `main`

```bash
git checkout main
git pull origin main
```

## 2. Branch

```bash
git checkout -b feat/queue-and-ci
```

Naming: `<type>/<short-description>` in kebab-case. Common types: `feat`, `fix`,
`refactor`, `test`, `ci`, `docs`, `chore`.

## 3. Commit in logical groups

One commit per thing done, not one commit per session. `git add .` followed by a
single commit makes the change impossible to review or bisect.

A PR that did four things becomes four commits:

```bash
git add backend/app/core/config.py
git commit -m "fix(config): resolve .env path absolutely"

git add backend/app/queue/
git commit -m "feat(queue): implement in-memory and SQS clients"

git add backend/tests/
git commit -m "test: split monolithic suites into per-module files"

git add .github/workflows/backend-ci.yml
git commit -m "ci(backend): run the unit suite on backend changes"
```

**Message format** — `<type>(<scope>): <subject>`

- Imperative mood: `add X`, not `added X`
- Under 72 characters, no trailing period
- The subject says what changed; the body, if any, says why

## 4. Push and open the PR

```bash
git push -u origin feat/queue-and-ci

gh pr create --base main \
  --title "feat(queue): implement the queue layer" \
  --body "## Summary
- Fix .env path resolution to use absolute paths
- Implement the queue layer with in-memory and SQS clients

## Why
Foundational work for dispatching tasks off the request path.

## Test Plan
- [x] uv run pytest
- [x] scripts/run_integration_tests.sh
- [x] CI green"
```

The PR title follows the same format as a commit message. The body needs three
things: **Summary** (what), **Why** (motivation), **Test Plan** (how you know).

## 5. Wait for CI

```bash
gh pr checks --watch
```

Three workflows run, each gated on the paths a PR touches:

| Workflow | Runs when | What it does |
|---|---|---|
| Backend CI | `backend/**` changed | The whole unit suite — SQLite and an in-memory queue, no Docker |
| Frontend CI | `frontend/**` changed | Typecheck, format check, vitest, production build |
| Integration Tests | every PR except docs-only | The integration suite against real PostgreSQL and LocalStack |

`✓` passed, `-` skipped because the paths did not match, `✗` needs fixing.
**Do not merge on anything but green.**

Run the same checks locally before pushing:

```bash
cd backend  && uv run pytest
cd frontend && npm run typecheck && npm run format:check && npm test
scripts/run_integration_tests.sh
```

## 6. Merge

```bash
gh pr merge --squash --delete-branch
```

Squashing puts one commit on `main` with the PR title as its message, and deletes the
remote branch.

---

## Common situations

**CI failed.** Fix it on the same branch; pushing re-runs the checks.

```bash
git add . && git commit -m "fix(queue): correct import path" && git push
```

**Your branch conflicts with `main`.**

```bash
git checkout main && git pull origin main
git checkout feat/xxx
git rebase main
# resolve, then
git push --force-with-lease
```

**You want to run a workflow that the path filters skipped.** Trigger it by hand from
the Actions tab, or with `gh workflow run backend-ci.yml`. Both `backend-ci.yml` and
`frontend-ci.yml` accept `workflow_dispatch`.

**You want to reproduce CI locally.** See the CI section of the
[README](README.md#ci) for running the workflows with `act`.

**Can you push straight to `main`?** No — branch protection rejects it.
