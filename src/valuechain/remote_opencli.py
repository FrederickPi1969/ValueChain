"""Explicit, remote-only OpenCLI browser extraction.

The earnings-call pipeline must never open a browser on the workstation that
launches the batch.  This adapter executes every Browser Bridge command on a
named SSH host and keeps the profile/session explicit for every invocation.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_SAFE_SSH_TARGET = re.compile(r"^[A-Za-z0-9_.@-]+$")
_SAFE_OPENCLI_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")


class RemoteOpenCLIError(RuntimeError):
    """A remote OpenCLI command or extraction contract failed."""


CommandRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


def _default_runner(command: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass(frozen=True)
class RemoteOpenCLIConfig:
    host: str
    profile: str
    executable: str = "/opt/homebrew/bin/opencli"
    helper_executable: str = "/Users/frederickpi/.local/bin/valuechain-opencli-extract"
    settle_seconds: int = 3
    chunk_size: int = 20_000
    max_chars: int = 1_000_000
    deadline_seconds: int = 300

    def validate(self) -> None:
        if not self.host or not _SAFE_SSH_TARGET.fullmatch(self.host):
            raise ValueError("OpenCLI SSH host must be an explicit SSH alias or host name")
        if not self.profile or not _SAFE_OPENCLI_NAME.fullmatch(self.profile):
            raise ValueError("OpenCLI profile must be explicit and contain only safe identifier characters")
        if not self.executable.startswith("/"):
            raise ValueError("remote OpenCLI executable must be an absolute path")
        if not self.helper_executable.startswith("/"):
            raise ValueError("remote OpenCLI helper must be an absolute path")
        if self.chunk_size < 1_000 or self.max_chars < self.chunk_size:
            raise ValueError("invalid OpenCLI extraction limits")


class RemoteOpenCLIExtractor:
    """Paginated Browser Bridge extractor whose browser lives on another Mac."""

    def __init__(self, config: RemoteOpenCLIConfig, *, runner: CommandRunner = _default_runner):
        config.validate()
        self.config = config
        self._runner = runner

    def _ssh(self, arguments: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        remote_command = shlex.join(arguments)
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            self.config.host,
            remote_command,
        ]
        completed = self._runner(command, timeout)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "remote OpenCLI command failed")[-1_000:]
            raise RemoteOpenCLIError(detail.strip())
        return completed

    def _remote_opencli(
        self, arguments: Sequence[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return self._ssh(
            [
                "env",
                "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                self.config.executable,
                "--profile",
                self.config.profile,
                *arguments,
            ],
            timeout=timeout,
        )

    def preflight(self) -> None:
        """Confirm that the named remote executable/profile can be addressed."""
        if self.config.profile == "auto-single":
            completed = self._ssh(
                [
                    "env",
                    "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                    self.config.executable,
                    "profile",
                    "list",
                ],
                timeout=30,
            )
            connected = re.findall(
                r"(?m)^\s*([A-Za-z0-9_.:-]+)\s+—\s+connected\b",
                completed.stdout,
            )
            if len(connected) != 1:
                raise RemoteOpenCLIError(
                    f"auto-single requires exactly one connected profile; found {len(connected)}"
                )
            return
        self._remote_opencli(["doctor"], timeout=30)

    def extract(self, url: str, session: str) -> tuple[str, str]:
        if not session or not _SAFE_OPENCLI_NAME.fullmatch(session):
            raise ValueError("OpenCLI session must be an explicit safe identifier")
        completed = self._ssh(
            [
                self.config.helper_executable,
                "--opencli",
                self.config.executable,
                "--profile",
                self.config.profile,
                "--session",
                session,
                "--url",
                url,
                "--settle-seconds",
                str(self.config.settle_seconds),
                "--chunk-size",
                str(self.config.chunk_size),
                "--max-chars",
                str(self.config.max_chars),
                "--deadline",
                str(self.config.deadline_seconds),
            ],
            timeout=self.config.deadline_seconds + 75,
        )
        try:
            payload = json.loads(completed.stdout)
            text = payload["text"]
            method = payload["method"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RemoteOpenCLIError(f"remote OpenCLI helper returned an invalid envelope: {exc}") from exc
        if payload.get("success") is not True or not isinstance(text, str) or len(text) < 500:
            raise RemoteOpenCLIError("remote OpenCLI helper did not return readable content")
        cleanup_warnings = payload.get("cleanup_warnings")
        if cleanup_warnings:
            raise RemoteOpenCLIError(
                "remote OpenCLI extracted content but could not cleanly release its tab/session: "
                + "; ".join(str(item) for item in cleanup_warnings)
            )
        return text, str(method)
