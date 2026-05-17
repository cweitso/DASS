from __future__ import annotations

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
    def run(self, spec: ContainerSpec) -> ExecutionResult:
        """Execute a job as a Docker container using a pre-built ContainerSpec."""
        try:
            return self._run_container(spec)
        except Exception as exc:
            return ExecutionResult(success=False, stdout="", stderr=str(exc))

    def _run_container(self, spec: ContainerSpec) -> ExecutionResult:
        docker_cmd = ["docker", "run", "--rm"]

        # Security note:
        # Do not allow user-controlled --privileged, host networking, host volume mounts,
        # or raw Docker args. On EC2 workers, also prevent containers from reaching
        # the instance metadata endpoint 169.254.169.254 if the worker has an IAM role.
        if spec.cpu is not None:
            docker_cmd.extend(["--cpus", str(spec.cpu)])

        if spec.memory_mb is not None:
            docker_cmd.extend(["--memory", f"{spec.memory_mb}m"])

        if spec.working_dir:
            docker_cmd.extend(["-w", spec.working_dir])

        for key, value in spec.env.items():
            if not _ENV_KEY_RE.match(key):
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr=f"Invalid environment variable key: {key}",
                )
            docker_cmd.extend(["-e", f"{key}={value}"])

        docker_cmd.append(spec.image)

        if spec.command:
            docker_cmd.extend(spec.command)

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
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=str(exc),
                exit_code=None,
            )
