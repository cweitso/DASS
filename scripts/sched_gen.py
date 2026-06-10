#!/usr/bin/env python3
"""Scheduled-dispatch stress test for DASS.

Creates N *scheduled* jobs (cron `*/2 * * * *` → due every 2 minutes) and then
does NOTHING else — it lets the SCHEDULER dispatch them. At each 2-minute boundary
the scheduler fires all N jobs at once into the scheduled queue (dass-tasks-scheduled),
giving an N-per-2-min burst that exercises the real scheduler → scheduled queue
→ worker (scheduled pool) path.

This is the opposite of load_gen: load_gen manually /triggers jobs, which creates
trigger_type='manual' tasks in the *normal* queue. Here nothing is triggered by
hand — every run is a trigger_type='scheduled' task produced by the scheduler.

The jobs are LEFT RUNNING (no auto-cleanup). cron `*/2 * * * *` fires forever, so
delete a run yourself when done with --cleanup --prefix (the script prints the exact
command after creating).

Watch on Grafana (DASS · Overview):
  - "Scheduled dispatches (/min)"  → spikes to ~N every 2 min
  - dass-tasks-scheduled line in "SQS visible / in-flight messages"
  - "Scheduled jobs" + "Scheduler runs" in the "Jobs & task mix" panel

Usage:
  uv --project backend run python scripts/sched_gen.py --count 200 --insecure                  # create 200, leave running
  uv --project backend run python scripts/sched_gen.py --cleanup --prefix sched-123 --insecure # delete a previous run
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
        # 帶 cron → job_type='scheduled'。每 2 分鐘 due 一次,由 scheduler 派發進 scheduled queue。
        "cron_expression": "*/5 * * * *",
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
    p.add_argument("--cleanup", action="store_true", help="only delete jobs matching --prefix, then exit")
    p.add_argument("--api", default="https://dass.localhost:8443", help="API base URL")
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

        print(f"target={args.api}  count={args.count:,}  cron='*/2 * * * *'  prefix={prefix}")
        print("watch http://localhost:3001/d/dass-overview → 'Scheduled dispatches (/min)' should spike every 2 min\n")

        ids: list[str] = []

        async def mk(i: int) -> None:
            async with sem:
                jid = await _create(client, args.api, f"{prefix}-{i:08d}-{uuid.uuid4().hex[:6]}")
                if jid:
                    ids.append(jid)

        start = time.time()
        await asyncio.gather(*(mk(i) for i in range(args.count)))
        print(f"  created {len(ids)}/{args.count} scheduled jobs in {time.time() - start:.1f}s")
        print(f"  the scheduler will dispatch ~{len(ids)} tasks every 2 min into dass-tasks-scheduled.")
        print(f"\n  jobs left running (no auto-cleanup). Delete this run later with:")
        print(f"    uv --project backend run python scripts/sched_gen.py --cleanup --prefix {prefix} {'--insecure' if args.insecure else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
