#!/usr/bin/env python3
"""End-to-end smoke test with a visual dashboard.

Runs against a live `docker compose up` stack and tells you, component by
component, which part of the pipeline is healthy and which is broken.

Usage:
    python scripts/e2e_smoke.py

No third-party deps required — stdlib only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field


# ── ANSI helpers ────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


def c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def ok(text: str) -> str:
    return c(f"✓ {text}", GREEN)


def fail(text: str) -> str:
    return c(f"✗ {text}", RED)


def warn(text: str) -> str:
    return c(f"! {text}", YELLOW)


def hr(char: str = "─", width: int = 78) -> str:
    return c(char * width, DIM)


def header(title: str) -> None:
    print()
    print(c("━" * 78, BOLD))
    print(c(f"  {title}", BOLD + CYAN))
    print(c("━" * 78, BOLD))


# ── Check primitives ────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "fail" | "warn" | "skip"
    detail: str = ""

    def render(self) -> str:
        icon = {
            "ok": c("✓", GREEN),
            "fail": c("✗", RED),
            "warn": c("!", YELLOW),
            "skip": c("·", DIM),
        }[self.status]
        color = {"ok": GREEN, "fail": RED, "warn": YELLOW, "skip": DIM}[self.status]
        line = f"  {icon}  {c(self.name.ljust(40), color)}"
        if self.detail:
            line += c(f" │ {self.detail}", DIM)
        return line


@dataclass
class Report:
    component_checks: list[CheckResult] = field(default_factory=list)
    pipeline_steps: list[CheckResult] = field(default_factory=list)

    def add_component(self, r: CheckResult) -> None:
        self.component_checks.append(r)
        print(r.render())

    def add_step(self, r: CheckResult) -> None:
        self.pipeline_steps.append(r)
        print(r.render())


def http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 5.0) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        data = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = {"raw": data}
        return e.code, parsed


def docker_compose_ps() -> dict[str, str]:
    """Return {service_name: state} for the current dass compose project."""
    try:
        out = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return {}
    if out.returncode != 0:
        return {}
    services: dict[str, str] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        services[obj.get("Service", "?")] = obj.get("State", "?")
    return services


# ── Phase 1: component health ───────────────────────────────────────────────


def check_components(api_base: str, report: Report) -> dict[str, str]:
    header("Phase 1 ── Component Health")

    services = docker_compose_ps()
    expected = ["postgres", "postgres-replica", "localstack", "api-server", "scheduler", "worker", "frontend"]
    if not services:
        report.add_component(CheckResult("docker compose ps", "warn", "no compose project detected in CWD"))
    else:
        for svc in expected:
            state = services.get(svc)
            if state is None:
                report.add_component(CheckResult(f"container: {svc}", "fail", "not in `docker compose ps`"))
            elif state.lower() == "running":
                report.add_component(CheckResult(f"container: {svc}", "ok", state))
            else:
                report.add_component(CheckResult(f"container: {svc}", "fail", state))

    # API /health (also exercises DB connection)
    try:
        status, body = http_get(f"{api_base}/health")
        if status == 200:
            report.add_component(CheckResult("API /health (also pings DB)", "ok", body.strip()))
        else:
            report.add_component(CheckResult("API /health", "fail", f"HTTP {status}"))
    except Exception as e:
        report.add_component(CheckResult("API /health", "fail", f"{type(e).__name__}: {e}"))

    # API /metrics
    try:
        status, body = http_get(f"{api_base}/metrics")
        if status == 200:
            report.add_component(CheckResult("API /metrics", "ok", body.strip()))
        else:
            report.add_component(CheckResult("API /metrics", "fail", f"HTTP {status}"))
    except Exception as e:
        report.add_component(CheckResult("API /metrics", "fail", str(e)))

    # LocalStack
    try:
        status, body = http_get("http://localhost:4566/_localstack/health")
        sqs_state = json.loads(body).get("services", {}).get("sqs", "?")
        if sqs_state in ("running", "available"):
            report.add_component(CheckResult("LocalStack SQS", "ok", sqs_state))
        else:
            report.add_component(CheckResult("LocalStack SQS", "warn", sqs_state))
    except Exception as e:
        report.add_component(CheckResult("LocalStack SQS", "fail", str(e)))

    # SQS queues exist (via LocalStack list-queues endpoint)
    try:
        status, body = http_get("http://localhost:4566/000000000000/?Action=ListQueues")
        queue_names = ("dass-tasks-normal", "dass-tasks-retry")
        for q in queue_names:
            if q in body:
                report.add_component(CheckResult(f"queue: {q}", "ok", "exists"))
            else:
                report.add_component(CheckResult(f"queue: {q}", "fail", "not found"))
    except Exception as e:
        report.add_component(CheckResult("SQS queue listing", "fail", str(e)))

    # Frontend
    try:
        status, _ = http_get("http://localhost:3000/")
        if status < 500:
            report.add_component(CheckResult("Frontend :3000", "ok", f"HTTP {status}"))
        else:
            report.add_component(CheckResult("Frontend :3000", "fail", f"HTTP {status}"))
    except Exception as e:
        report.add_component(CheckResult("Frontend :3000", "warn", str(e)))

    return services


# ── Phase 2: end-to-end user request flow ───────────────────────────────────


def run_pipeline(api_base: str, report: Report) -> None:
    header("Phase 2 ── End-to-End: create job → trigger → watch task")

    job_name = f"e2e-smoke-{uuid.uuid4().hex[:8]}"
    payload = {
        "name": job_name,
        "cron_expression": "0 0 1 1 *",  # once a year — we'll trigger manually
        "action_type": "shell",
        "action_config": {"command": "echo hello-from-e2e", "timeout_seconds": 5},
        "enabled": True,
        "concurrency_policy": "allow",
        "max_retries": 0,
    }

    # Step 1: create job
    status, resp = http_json("POST", f"{api_base}/api/v1/jobs", payload)
    if status != 200:
        report.add_step(CheckResult("POST /api/v1/jobs", "fail", f"HTTP {status}: {resp}"))
        return
    job_id = resp.get("id")
    report.add_step(CheckResult("POST /api/v1/jobs", "ok", f"job_id={job_id}"))

    # Step 2: trigger manually
    status, resp = http_json("POST", f"{api_base}/api/v1/jobs/{job_id}/trigger")
    if status != 200:
        report.add_step(CheckResult("POST /jobs/{id}/trigger", "fail", f"HTTP {status}: {resp}"))
        return
    task_id = resp.get("task_id")
    report.add_step(CheckResult("POST /jobs/{id}/trigger", "ok", f"task_id={task_id} status={resp.get('status')}"))

    # Step 3: poll task state
    print()
    print(c("  Task state timeline (worker should claim and execute):", DIM))
    deadline = time.time() + 60
    last_status = None
    transitions: list[tuple[float, str]] = []
    start = time.time()
    final_task: dict | None = None
    while time.time() < deadline:
        status, tasks = http_json("GET", f"{api_base}/api/v1/jobs/{job_id}/tasks")
        if status != 200 or not isinstance(tasks, list):
            report.add_step(CheckResult("GET /jobs/{id}/tasks", "fail", f"HTTP {status}: {tasks}"))
            return
        # find our task
        task = next((t for t in tasks if t.get("id") == task_id), None)
        if task is None:
            # might be a brand-new retry task — fall back to most recent
            task = tasks[0] if tasks else None
        if task and task.get("status") != last_status:
            last_status = task.get("status")
            elapsed = time.time() - start
            transitions.append((elapsed, last_status))
            color = {
                "pending": YELLOW,
                "running": CYAN,
                "success": GREEN,
                "failed": MAGENTA,
                "final_failed": RED,
            }.get(last_status, RESET)
            print(f"    {c(f'+{elapsed:5.1f}s', DIM)}  {c(last_status, color)}")
            if last_status in ("success", "final_failed"):
                final_task = task
                break
        time.sleep(1)

    if not final_task:
        report.add_step(CheckResult("task reached terminal state", "fail", f"timed out, last status={last_status}"))
        # diagnose why
        if last_status is None:
            print(c("    diagnosis: task never appeared in DB — JobService or DB write broken", RED))
        elif last_status == "pending":
            print(c("    diagnosis: worker never claimed it → worker not consuming queue", RED))
            print(c("    look at: cli.run_worker(), queue factory, SQS connectivity from worker container", DIM))
        elif last_status == "running":
            print(c("    diagnosis: worker claimed but didn't finish within 60s", RED))
        return

    if final_task["status"] == "success":
        report.add_step(CheckResult("task terminal state", "ok", "success"))
        stdout = (final_task.get("stdout") or "").strip()
        stderr = (final_task.get("stderr") or "").strip()
        if stdout:
            print(c(f"    stdout: {stdout[:200]}", DIM))
        if stderr:
            print(c(f"    stderr: {stderr[:200]}", DIM))
    else:
        report.add_step(CheckResult("task terminal state", "fail", final_task["status"]))
        stderr = (final_task.get("stderr") or "").strip()
        stdout = (final_task.get("stdout") or "").strip()
        if stderr:
            print(c(f"    stderr: {stderr[:300]}", RED))
        if stdout:
            print(c(f"    stdout: {stdout[:300]}", DIM))
        # heuristic diagnosis
        if "runtime_spec" in stderr or "ContainerSpec" in stderr or "image" in stderr.lower():
            print(c("    diagnosis: ExecutionService got no valid ContainerSpec.", RED))
            print(c("              WorkerService reads job.runtime_spec which is not a", DIM))
            print(c("              column on Job — only action_config exists.", DIM))


# ── Verdict ─────────────────────────────────────────────────────────────────


def print_verdict(report: Report) -> int:
    header("Verdict")
    n_comp_fail = sum(1 for c in report.component_checks if c.status == "fail")
    n_step_fail = sum(1 for c in report.pipeline_steps if c.status == "fail")
    n_comp_warn = sum(1 for c in report.component_checks if c.status == "warn")

    summary = [
        f"  Components:  {len(report.component_checks)} checks, {n_comp_fail} failed, {n_comp_warn} warnings",
        f"  Pipeline:    {len(report.pipeline_steps)} steps,  {n_step_fail} failed",
    ]
    for s in summary:
        print(s)

    print()
    if n_comp_fail == 0 and n_step_fail == 0:
        print(c("  ✓ all green — stack is end-to-end healthy", GREEN + BOLD))
        return 0
    print(c("  ✗ something is broken — see red lines above", RED + BOLD))
    return 1


# ── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()

    print(c("DASS end-to-end smoke test", BOLD))
    print(c(f"API base: {args.api}", DIM))

    report = Report()
    check_components(args.api, report)
    run_pipeline(args.api, report)
    return print_verdict(report)


if __name__ == "__main__":
    sys.exit(main())
