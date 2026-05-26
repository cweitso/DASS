#!/usr/bin/env python3
"""Bulk job creator for stress testing.

Creates N jobs via the API (and optionally triggers each), so you can watch
queue depth / worker throughput / DB pressure react in real time at
http://localhost:3001 (Grafana DASS · Overview).

Usage:
  scripts/load_gen.py                              # 1,000 jobs, create only
  scripts/load_gen.py --count 50000                # 5万
  scripts/load_gen.py --count 50000 --trigger      # also fire each one once
  scripts/load_gen.py --count 100 --concurrency 50 # tune parallelism

Why a CLI tool: realistic load means real HTTP roundtrips to the API. Direct
DB inserts would skip the API → JobService → repository → DB write path that's
actually what you want to stress.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from typing import Any

import httpx


DEFAULT_PAYLOAD: dict[str, Any] = {
    # 每年才跑一次的 cron — scheduler 不會自動 dispatch；要 dispatch 就 --trigger
    "cron_expression": "0 0 1 1 *",
    "action_type": "shell",
    "action_config": {"command": "echo load-gen", "timeout_seconds": 5},
    "enabled": True,
    "concurrency_policy": "allow",
    "max_retries": 0,
}


async def create_one(client: httpx.AsyncClient, base: str, prefix: str, i: int) -> tuple[bool, str | None]:
    payload = {**DEFAULT_PAYLOAD, "name": f"{prefix}-{i:08d}-{uuid.uuid4().hex[:6]}"}
    try:
        resp = await client.post(f"{base}/api/v1/jobs", json=payload)
        if resp.status_code == 200:
            return True, resp.json().get("id")
        return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# async def trigger_one(client: httpx.AsyncClient, base: str, job_id: str) -> bool:
#     try:
#         resp = await client.post(f"{base}/api/v1/jobs/{job_id}/trigger")
#         return resp.status_code == 200
#     except Exception:
#         return False

async def trigger_one(client: httpx.AsyncClient, base: str, job_id: str) -> bool:
    try:
        resp = await client.post(f"{base}/api/v1/jobs/{job_id}/trigger")
        return resp.status_code == 200
    except Exception as e:
        print(f"\n  [trigger error] {type(e).__name__}: {e}", flush=True)  # 加這行
        return False


def _print_progress(label: str, done: int, total: int, start: float) -> None:
    elapsed = time.time() - start
    rps = done / max(elapsed, 0.001)
    eta = (total - done) / max(rps, 0.001) if rps > 0 else float("inf")
    eta_s = f"{eta:5.0f}s" if eta != float("inf") else "  ?  "
    print(f"  [{label}] {done:>8}/{total}  {rps:8.1f}/s  eta={eta_s}", end="\r", flush=True)


async def bulk_create(client: httpx.AsyncClient, base: str, count: int, concurrency: int, prefix: str) -> list[str]:
    sem = asyncio.Semaphore(concurrency)
    succeeded = 0
    failed = 0
    last_error: str | None = None
    created_ids: list[str] = []
    start = time.time()
    progress_step = max(1, count // 200)

    async def work(i: int) -> None:
        nonlocal succeeded, failed, last_error
        async with sem:
            ok, info = await create_one(client, base, prefix, i)
            if ok:
                succeeded += 1
                if info:
                    created_ids.append(info)
            else:
                failed += 1
                last_error = info or "unknown"
            done = succeeded + failed
            if done % progress_step == 0 or done == count:
                _print_progress("create", done, count, start)

    await asyncio.gather(*(work(i) for i in range(count)))
    print()
    elapsed = time.time() - start
    print(f"  create done: ok={succeeded} fail={failed} in {elapsed:.1f}s ({succeeded / max(elapsed, 0.001):.1f}/s)")
    if last_error:
        print(f"  last failure: {last_error}")
    return created_ids


async def bulk_trigger(client: httpx.AsyncClient, base: str, ids: list[str], concurrency: int) -> None:
    sem = asyncio.Semaphore(concurrency)
    ok = 0
    fail = 0
    start = time.time()
    progress_step = max(1, len(ids) // 200)
    count = len(ids)

    async def work(jid: str) -> None:
        nonlocal ok, fail
        async with sem:
            if await trigger_one(client, base, jid):
                ok += 1
            else:
                fail += 1
            done = ok + fail
            if done % progress_step == 0 or done == count:
                _print_progress("trigger", done, count, start)

    await asyncio.gather(*(work(jid) for jid in ids))
    print()
    elapsed = time.time() - start
    print(f"  trigger done: ok={ok} fail={fail} in {elapsed:.1f}s ({ok / max(elapsed, 0.001):.1f}/s)")


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=1000, help="number of jobs to create (default 1000)")
    parser.add_argument("--concurrency", type=int, default=32, help="parallel HTTP requests (default 32)")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--trigger", action="store_true", help="after creation, fire each job once via /trigger")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-request timeout seconds")
    parser.add_argument("--prefix", default=None, help="job name prefix (default: load-<timestamp>)")
    args = parser.parse_args()

    if args.count > 1_000_000:
        print(f"⚠ {args.count:,} jobs is extreme — make sure your DB can hold this many rows.")

    prefix = args.prefix or f"load-{int(time.time())}"
    print(f"target={args.api}  count={args.count:,}  concurrency={args.concurrency}  trigger={args.trigger}")
    print(f"prefix={prefix}")
    print(f"watch http://localhost:3001/d/dass-overview while this runs\n")

    limits = httpx.Limits(
        max_connections=args.concurrency * 2,
        max_keepalive_connections=args.concurrency,
    )
    timeout = httpx.Timeout(args.timeout, connect=5.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        # Quick health check
        try:
            r = await client.get(f"{args.api}/health")
            if r.status_code != 200:
                print(f"✗ API /health returned {r.status_code} — is the stack up? (docker compose up -d)")
                return 1
        except Exception as e:
            print(f"✗ cannot reach {args.api}: {e}")
            print("  is the stack up? `docker compose up -d`")
            return 1

        ids = await bulk_create(client, args.api, args.count, args.concurrency, prefix)

        if args.trigger and ids:
            print()
            await bulk_trigger(client, args.api, ids, args.concurrency)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
