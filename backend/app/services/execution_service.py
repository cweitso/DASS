from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass, field

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class ContainerSpec:
    image: str
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 300
    cpu: float | None = None
    memory_mb: int | None = None
    working_dir: str | None = None


@dataclass
class ExecutionResult:
    success: bool
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None


class ExecutionService:
    def __init__(self, network: str | None = None):
        self.network = network

    def _build_docker_cmd(self, spec: ContainerSpec) -> list[str] | None:
        """Build docker run command list. Returns None if env key validation fails."""
        docker_cmd = ["docker", "run", "--rm"]
        if self.network:
            docker_cmd.extend(["--network", self.network])
        # Security: no --privileged, host networking, or host volume mounts allowed.
        if spec.cpu is not None:
            docker_cmd.extend(["--cpus", str(spec.cpu)])
        if spec.memory_mb is not None:
            docker_cmd.extend(["--memory", f"{spec.memory_mb}m"])
        if spec.working_dir:
            docker_cmd.extend(["-w", spec.working_dir])
        for key, value in spec.env.items():
            if not _ENV_KEY_RE.match(key):
                return None
            docker_cmd.extend(["-e", f"{key}={value}"])
        docker_cmd.append(spec.image)
        if spec.command:
            docker_cmd.extend(spec.command)
        return docker_cmd

    def run(self, spec: ContainerSpec) -> ExecutionResult:
        """Execute a job as a Docker container (blocking)."""
        docker_cmd = self._build_docker_cmd(spec)
        if docker_cmd is None:
            return ExecutionResult(success=False, stdout="", stderr="Invalid environment variable key")
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
                check=False,
            )
            return ExecutionResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                success=False,
                stdout=exc.stdout if isinstance(exc.stdout, str) else "",
                stderr=f"Container execution timed out after {spec.timeout_seconds}s",
                exit_code=None,
            )
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc), exit_code=None)

    async def run_async(self, spec: ContainerSpec) -> ExecutionResult:
        """Execute a job as a Docker container (non-blocking async).

        Uses asyncio.create_subprocess_exec so the event loop stays free
        while the container is running — allowing many concurrent I/O-bound
        jobs (e.g. long HTTP calls) to share a single worker thread.
        """
        docker_cmd = self._build_docker_cmd(spec)
        if docker_cmd is None:
            return ExecutionResult(success=False, stdout="", stderr="Invalid environment variable key")
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=spec.timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr=f"Container execution timed out after {spec.timeout_seconds}s",
                    exit_code=None,
                )
            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=stdout_b.decode(errors="replace"),
                stderr=stderr_b.decode(errors="replace"),
                exit_code=proc.returncode,
            )
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc), exit_code=None)
