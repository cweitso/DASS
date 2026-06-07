#!/usr/bin/env python3
"""Scheduled-dispatch stress test for DASS.

Creates N *scheduled* jobs (cron `* * * * *` → due every minute) and then does
NOTHING else — it lets the SCHEDULER dispatch them. At each minute boundary the
scheduler fires all N jobs at once into the scheduled queue (dass-tasks-scheduled),
giving an N-per-minute burst that exercises the real scheduler → scheduled queue
→ worker (scheduled pool) path.

This is the opposite of load_gen: load_gen manually /triggers jobs, which creates
trigger_type='manual' tasks in the *normal* queue. Here nothing is triggered by
hand — every run is a trigger_type='scheduled' task produced by the scheduler.

Watch on Grafana (DASS · Overview):
  - "Scheduled dispatches (/min)"  → spikes to ~N each minute
  - dass-tasks-scheduled line in "SQS visible / in-flight messages"
  - "Scheduled jobs" + "Scheduler runs" in the "Jobs & task mix" panel

⚠ cron `* * * * *` fires FOREVER. By default this watches for --watch seconds,
then DELETES every job it created. Pass --keep to leave them running (remember to
clean them up yourself, or Active Jobs / scheduled load stays elevated).

Usage:
  scripts/sched_gen.py --count 200 --insecure        # 200 jobs, watch ~2 min, then cleanup
  scripts/sched_gen.py --count 500 --keep            # leave them firing every minute
  scripts/sched_gen.py --cleanup --prefix sched-123  # delete a previous run's jobs
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


def _payload(name: str) -> dict[str, Any]:
    return {
        "name": name,
        # 帶 cron → job_type='scheduled'。每分鐘 due 一次,由 scheduler 派發進 scheduled queue。
        "cron_expression": "* * * * *",
        "action_type": "shell",
        "action_config": {"command": "echo scheduled-load", "timeout_seconds": 5},
        "enabled": True,
        "concurrency_policy": "allow",
        "max_retries": 0,
    }


async def _create(client: httpx.AsyncClient, base: str, name: str) -> str | None:
    try:
        r = await client.post(f"{base}/api/v1/jobs", json=_payload(name))
        return r.json().get("id") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


async def _delete(client: httpx.AsyncClient, base: str, job_id: str) -> bool:
    try:
        r = await client.delete(f"{base}/api/v1/jobs/{job_id}")
        return r.status_code in (200, 204)
    except Exception:  # noqa: BLE001
        return False


async def _list_ids_by_prefix(client: httpx.AsyncClient, base: str, prefix: str) -> list[str]:
    ids: list[str] = []
    page = 1
    while True:
        r = await client.get(f"{base}/api/v1/jobs", params={"page": page, "page_size": 100, "q": prefix})
        if r.status_code != 200:
            break
        items = r.json().get("items", [])
        ids += [it["id"] for it in items]
        if len(items) < 100:
            break
        page += 1
    return ids


async def amain() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--count", type=int, default=200, help="number of scheduled jobs to create (default 200)")
    p.add_argument("--concurrency", type=int, default=32, help="parallel HTTP requests (default 32)")
    p.add_argument("--watch", type=int, default=140, help="seconds to let the scheduler fire before cleanup (default 140 ≈ 2 boundaries)")
    p.add_argument("--keep", action="store_true", help="do NOT delete the jobs afterwards (they keep firing every minute)")
    p.add_argument("--cleanup", action="store_true", help="only delete jobs matching --prefix, then exit")
    p.add_argument("--api", default="https://localhost:8443", help="API base URL")
    p.add_argument("--ca-cert", default=str(DEFAULT_CA_CERT), help="CA bundle to trust")
    p.add_argument("--insecure", action="store_true", help="disable TLS verification")
    p.add_argument("--timeout", type=float, default=15.0, help="per-request timeout seconds")
    p.add_argument("--prefix", default=None, help="job name prefix (default: sched-<timestamp>)")
    args = p.parse_args()

    prefix = args.prefix or f"sched-{int(time.time())}"
    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(args.timeout, connect=5.0)
    verify: bool | str = False if args.insecure else args.ca_cert
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(limits=limits, timeout=timeout, verify=verify) as client:
        # cleanup-only mode: delete a previous run's jobs and exit
        if args.cleanup:
            if not args.prefix:
                print("✗ --cleanup needs --prefix <the run's prefix>")
                return 1
            ids = await _list_ids_by_prefix(client, args.api, args.prefix)
            print(f"deleting {len(ids)} jobs matching prefix={args.prefix} ...")
            await asyncio.gather(*(_delete(client, args.api, j) for j in ids))
            print("done.")
            return 0

        print(f"target={args.api}  count={args.count:,}  cron='* * * * *'  prefix={prefix}")
        print("watch http://localhost:3001/d/dass-overview → 'Scheduled dispatches (/min)' should spike each minute\n")

        ids: list[str] = []

        async def mk(i: int) -> None:
            async with sem:
                jid = await _create(client, args.api, f"{prefix}-{i:08d}-{uuid.uuid4().hex[:6]}")
                if jid:
                    ids.append(jid)

        start = time.time()
        await asyncio.gather(*(mk(i) for i in range(args.count)))
        print(f"  created {len(ids)}/{args.count} scheduled jobs in {time.time() - start:.1f}s")
        print(f"  the scheduler will dispatch ~{len(ids)} tasks/min into dass-tasks-scheduled.")

        if args.keep:
            print(f"\n  --keep set: jobs left running. Clean up later with:")
            print(f"    scripts/sched_gen.py --cleanup --prefix {prefix} {'--insecure' if args.insecure else ''}")
            return 0

        print(f"\n  watching {args.watch}s, then deleting these jobs (use --keep to leave them)...")
        for remaining in range(args.watch, 0, -10):
            print(f"    cleanup in {remaining:>4}s ...", end="\r", flush=True)
            await asyncio.sleep(min(10, remaining))
        print("\n  deleting...")
        res = await asyncio.gather(*(_delete(client, args.api, j) for j in ids))
        print(f"  deleted {sum(res)}/{len(ids)} jobs. scheduled-load test done.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
