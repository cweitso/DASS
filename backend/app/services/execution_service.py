from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from app.schemas.job import HttpActionConfig, ShellActionConfig


@dataclass
class ExecutionResult:
    success: bool
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None


class ExecutionService:
    # 看是哪種 action type，然後呼叫對應的執行方法，如果都不是就回傳400
    def run(self, action_type: str, action_config: dict) -> ExecutionResult:
        if action_type == "http":
            return self._run_http(HttpActionConfig.model_validate(action_config))
        if action_type == "shell":
            return self._run_shell(ShellActionConfig.model_validate(action_config))
        raise HTTPException(status_code=400, detail="Unsupported action type")

    def _run_http(self, config: HttpActionConfig) -> ExecutionResult:
        timeout = httpx.Timeout(config.timeout_seconds)
        content = None
        json_body = None
        if isinstance(config.body, dict):
            json_body = config.body
        elif isinstance(config.body, str) and config.body:
            content = config.body
            
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(config.method.upper(), config.url, headers=config.headers, content=content, json=json_body)
            stdout = f"status={response.status_code}\n{response.text}"
            if response.is_success:
                return ExecutionResult(success=True, stdout=stdout, stderr="")
            return ExecutionResult(success=False, stdout=stdout, stderr=f"HTTP {response.status_code}")
        except httpx.RequestError as e:
            return ExecutionResult(success=False, stdout="", stderr=f"HTTP Error: {str(e)}")

    def _run_shell(self, config: ShellActionConfig) -> ExecutionResult:
        try:
            completed = subprocess.run(
                config.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                check=False,
            )
            success = completed.returncode == 0
            return ExecutionResult(
                success=success,
                stdout=completed.stdout,
                stderr=completed.stderr or ("" if success else f"exit code {completed.returncode}"),
                exit_code=completed.returncode,
            )
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return ExecutionResult(success=False, stdout=out, stderr=f"Timeout expired after {config.timeout_seconds}s", exit_code=-1)
        except Exception as e:
            return ExecutionResult(success=False, stdout="", stderr=str(e), exit_code=-1)