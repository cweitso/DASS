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
    def run(self, action_type: str, action_config: dict) -> ExecutionResult:
        """根據 action_type 分派到對應的執行方法。"""
        if action_type == 'http':
            return self._run_http(HttpActionConfig.model_validate(action_config))
        elif action_type == 'shell':
            return self._run_shell(ShellActionConfig.model_validate(action_config))
        else:
            raise HTTPException(status_code=400, detail="Unsupported action type")

    def _run_http(self, config: HttpActionConfig) -> ExecutionResult:
        """執行 HTTP request 並回傳結果。"""
        kwargs = {}
        if config.body is not None:
            if isinstance(config.body, dict):
                kwargs["json"] = config.body
            else:
                kwargs["content"] = str(config.body)

        try:
            with httpx.Client(timeout=config.timeout_seconds) as client:
                response = client.request(
                    method=config.method,
                    url=config.url,
                    headers=config.headers,
                    **kwargs
                )
                success = response.is_success
                stdout = f"status={response.status_code}\n{response.text}"
                stderr = "" if success else f"HTTP {response.status_code}"
                return ExecutionResult(success=success, stdout=stdout, stderr=stderr)
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc))

    def _run_shell(self, config: ShellActionConfig) -> ExecutionResult:
        """執行 shell command 並回傳結果。"""
        try:
            result = subprocess.run(
                config.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                check=False
            )
            return ExecutionResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode
            )
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc))
