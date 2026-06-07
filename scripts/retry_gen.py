#!/usr/bin/env python3
"""Retry / failure stress test for DASS.

Creates N jobs whose shell command always fails (`exit 1`), then triggers each
one. Every execution fails, so the worker re-enqueues the task to the retry
queue (dass-tasks-retry) and bumps retry_count — up to the job's max_retries —
and finally marks it `final_failed`. So one job produces (max_retries + 1)
failed executions and lands 1 row in final_failed.

Use it to exercise the retry path and watch, on Grafana (DASS · Overview):
  - Failed/min  (top row)
  - dass-tasks-retry line in "SQS visible / in-flight messages"
  - "Retry runs (total)" + "Retry queue (now)" in the "Jobs & task mix" panel
  - failed / final_failed in "Tasks by status"

These are job_type='normal' (no cron) — the scheduler never touches them; only
the manual /trigger fires them, so all the noise stays in the normal+retry queues.

Usage:
  scripts/retry_gen.py --count 200 --max-retries 3 --insecure
  scripts/retry_gen.py --count 50 --concurrency 16
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CA_CERT = REPO_ROOT / "infra" / "traefik" / "pki" / "rootCA.crt"


def _payload(name: str, max_retries: int) -> dict[str, Any]:
    return {
        "name": name,
        # 沒 cron → job_type='normal'。command 一定失敗(exit 1)→ worker 判定 success=False → 重試。
        "action_type": "shell",
        "action_config": {"command": "echo boom >&2; exit 1", "timeout_seconds": 5},
        "enabled": True,
        "concurrency_policy": "allow",
        "max_retries": max_retries,
    }


async def _create_and_trigger(client: httpx.AsyncClient, base: str, name: str, max_retries: int) -> tuple[bool, str | None]:
    try:
        r = await client.post(f"{base}/api/v1/jobs", json=_payload(name, max_retries))
        if r.status_code != 200:
            return False, f"create HTTP {r.status_code}: {r.text[:80]}"
        job_id = r.json().get("id")
        t = await client.post(f"{base}/api/v1/jobs/{job_id}/trigger")
        if t.status_code != 200:
            return False, f"trigger HTTP {t.status_code}: {t.text[:80]}"
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def amain() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--count", type=int, default=200, help="number of failing jobs to create+trigger (default 200)")
    p.add_argument("--max-retries", type=int, default=3, help="retries per job before final_failed (default 3)")
    p.add_argument("--concurrency", type=int, default=32, help="parallel HTTP requests (default 32)")
    p.add_argument("--api", default="https://localhost:8443", help="API base URL")
    p.add_argument("--ca-cert", default=str(DEFAULT_CA_CERT), help="CA bundle to trust")
    p.add_argument("--insecure", action="store_true", help="disable TLS verification")
    p.add_argument("--timeout", type=float, default=15.0, help="per-request timeout seconds")
    p.add_argument("--prefix", default=None, help="job name prefix (default: retry-<timestamp>)")
    args = p.parse_args()

    prefix = args.prefix or f"retry-{int(time.time())}"
    expected_fail_runs = args.count * (args.max_retries + 1)
    print(f"target={args.api}  count={args.count:,}  max_retries={args.max_retries}  concurrency={args.concurrency}")
    print(f"prefix={prefix}")
    print(f"→ expect ~{expected_fail_runs:,} failed executions and {args.count:,} final_failed rows")
    print("watch http://localhost:3001/d/dass-overview (Failed/min, retry queue, Retry runs)\n")

    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(args.timeout, connect=5.0)
    verify: bool | str = False if args.insecure else args.ca_cert

    sem = asyncio.Semaphore(args.concurrency)
    ok = 0
    fail = 0
    last_err: str | None = None
    start = time.time()
    step = max(1, args.count // 200)

    async with httpx.AsyncClient(limits=limits, timeout=timeout, verify=verify) as client:
        try:
            h = await client.get(f"{args.api}/health")
            if h.status_code != 200:
                print(f"✗ /health returned {h.status_code} — is the stack up?")
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"✗ cannot reach {args.api}: {e}")
            return 1

        async def work(i: int) -> None:
            nonlocal ok, fail, last_err
            async with sem:
                good, info = await _create_and_trigger(client, args.api, f"{prefix}-{i:08d}-{uuid.uuid4().hex[:6]}", args.max_retries)
                if good:
                    ok += 1
                else:
                    fail += 1
                    last_err = info
                done = ok + fail
                if done % step == 0 or done == args.count:
                    elapsed = time.time() - start
                    print(f"  [submit] {done:>8}/{args.count}  {done / max(elapsed, 0.001):8.1f}/s", end="\r", flush=True)

        await asyncio.gather(*(work(i) for i in range(args.count)))

    print()
    elapsed = time.time() - start
    print(f"  submitted ok={ok} fail={fail} in {elapsed:.1f}s")
    if last_err:
        print(f"  last error: {last_err}")
    print("  retries are now draining through dass-tasks-retry — give it a minute and watch Grafana.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
