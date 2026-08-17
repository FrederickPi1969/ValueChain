#!/usr/bin/env python3
"""One-shot, serialized OpenCLI extraction helper for the Mac mini.

This file intentionally uses only the Python standard library.  Deploy it to
the browser host and call it through one SSH process; the helper owns the
profile-wide lease from tab creation through explicit tab cleanup.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")


class HelperError(RuntimeError):
    pass


class ExtractionFailure(HelperError):
    """Primary extraction failure plus any cleanup failures from the same tab."""

    def __init__(self, primary: Exception, cleanup_warnings: list[str]):
        self.primary = primary
        self.cleanup_warnings = tuple(cleanup_warnings)
        detail = f"{type(primary).__name__}: {primary}"
        if cleanup_warnings:
            detail += "; cleanup failures: " + "; ".join(cleanup_warnings)
        super().__init__(detail)


def run(command: list[str], deadline: float, *, cap: int) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HelperError("overall extraction deadline exceeded")
    environment = os.environ.copy()
    environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + environment.get("PATH", "")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=max(1, min(cap, int(remaining))),
        env=environment,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "OpenCLI command failed")[-1_000:]
        raise HelperError(detail.strip())
    return completed


def acquire_lock(path: Path, deadline: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} acquired={time.time()}\n")
            handle.flush()
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise HelperError("timed out waiting for the profile-wide browser lease")
            time.sleep(0.25)


def parse_chunk(raw: str, requested_start: int) -> tuple[str, int | None, int]:
    try:
        payload = json.loads(raw)
        content = payload["content"]
        start = int(payload.get("start", 0))
        end = int(payload.get("end", start + len(content)))
        total = int(payload.get("total_chars", end))
        next_raw = payload.get("next_start_char")
        next_start = None if next_raw is None else int(next_raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise HelperError(f"invalid OpenCLI extract envelope: {exc}") from exc
    if not isinstance(content, str) or start != requested_start:
        raise HelperError(f"invalid OpenCLI cursor: requested={requested_start}, returned={start}")
    if start < 0 or end < start or total < end:
        raise HelperError("invalid OpenCLI extract offsets")
    if end - start != len(content):
        raise HelperError(
            f"OpenCLI content length disagrees with offsets: "
            f"start={start}, end={end}, content={len(content)}"
        )
    if next_start is not None and next_start != end:
        raise HelperError(f"non-contiguous OpenCLI cursor: end={end}, next={next_start}")
    if next_start is None and end < total:
        raise HelperError("OpenCLI extraction ended before total_chars")
    return content, next_start, total


def resolve_profile(args: argparse.Namespace, deadline: float) -> str:
    if args.profile != "auto-single":
        return args.profile
    completed = run([args.opencli, "profile", "list"], deadline, cap=30)
    connected = re.findall(r"(?m)^\s*([A-Za-z0-9_.:-]+)\s+—\s+connected\b", completed.stdout)
    if len(connected) != 1:
        raise HelperError(
            f"auto-single requires exactly one connected Browser Bridge profile; found {len(connected)}"
        )
    return connected[0]


def extract(args: argparse.Namespace) -> dict:
    if not SAFE_NAME.fullmatch(args.profile) or not SAFE_NAME.fullmatch(args.session):
        raise HelperError("profile and session must be explicit safe identifiers")
    if not Path(args.opencli).is_absolute() or not Path(args.opencli).is_file():
        raise HelperError("OpenCLI executable is missing or not an absolute file path")
    if args.chunk_size < 1_000 or args.max_chars < args.chunk_size:
        raise HelperError("invalid extraction size limits")

    deadline = time.monotonic() + args.deadline
    resolved_profile = resolve_profile(args, deadline)
    lock = acquire_lock(
        Path(args.lock_dir) / f"valuechain-opencli-{resolved_profile}.lock", deadline
    )
    # Every browser command receives the concrete resolved profile.  The
    # special `auto-single` selector is never passed to a browser operation.
    browser = [args.opencli, "--profile", resolved_profile, "browser", args.session]
    target_id: str | None = None
    cleanup_errors: list[str] = []
    result: dict | None = None
    primary_error: Exception | None = None
    try:
        created = run([*browser, "tab", "new", args.url], deadline, cap=60)
        try:
            target_id = str(json.loads(created.stdout)["page"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise HelperError(f"tab creation returned no target ID: {exc}") from exc
        run([*browser, "wait", "time", str(args.settle_seconds)], deadline, cap=30)

        chunks: list[str] = []
        assembled = 0
        start = 0
        total = 0
        expected_total: int | None = None
        while True:
            completed = run(
                [*browser, "extract", "--chunk-size", str(args.chunk_size), "--start", str(start)],
                deadline,
                cap=90,
            )
            content, next_start, total = parse_chunk(completed.stdout, start)
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise HelperError(
                    f"OpenCLI total_chars changed across chunks: {expected_total} -> {total}"
                )
            chunks.append(content)
            assembled += len(content)
            if assembled > args.max_chars:
                raise HelperError(f"extraction exceeded {args.max_chars} characters")
            if next_start is None:
                break
            start = next_start
        # Each chunk is a character slice, not a paragraph.  Never inject a
        # newline at the cursor boundary.
        text = "".join(chunks)
        if expected_total is None or assembled != expected_total or len(text) != expected_total:
            raise HelperError(
                "assembled OpenCLI text length disagrees with reported total: "
                f"assembled={assembled}, text={len(text)}, total={expected_total}"
            )
        if len(text) < 500:
            raise HelperError("extraction contained fewer than 500 readable characters")
        result = {
            "success": True,
            "text": text,
            "text_chars": len(text),
            "reported_total_chars": total,
            "target_id": target_id,
            "resolved_profile": resolved_profile,
            "method": "opencli.remote_helper.paginated",
        }
    except Exception as exc:  # noqa: BLE001 - cleanup must run before surfacing the primary error
        primary_error = exc
    finally:
        cleanup_deadline = max(deadline, time.monotonic() + 60)
        if target_id:
            for attempt in range(2):
                try:
                    run([*browser, "tab", "close", target_id], cleanup_deadline, cap=30)
                    break
                except Exception as exc:  # noqa: BLE001 - cleanup warnings are returned to the caller
                    if attempt:
                        cleanup_errors.append(f"tab close: {exc}")
        try:
            run([*browser, "close"], cleanup_deadline, cap=30)
        except Exception as exc:  # noqa: BLE001 - cleanup warnings are returned to the caller
            cleanup_errors.append(f"lease close: {exc}")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except Exception as exc:  # noqa: BLE001 - report lock cleanup with the extraction failure
            cleanup_errors.append(f"lock release: {exc}")
        try:
            lock.close()
        except Exception as exc:  # noqa: BLE001 - report lock cleanup with the extraction failure
            cleanup_errors.append(f"lock close: {exc}")
    if primary_error is not None:
        raise ExtractionFailure(primary_error, cleanup_errors) from primary_error
    if result is None:
        raise HelperError("extraction ended without a result")
    result["cleanup_warnings"] = cleanup_errors
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opencli", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--settle-seconds", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=20_000)
    parser.add_argument("--max-chars", type=int, default=1_000_000)
    parser.add_argument("--deadline", type=int, default=300)
    parser.add_argument("--lock-dir", default="/tmp")
    args = parser.parse_args()
    try:
        payload = extract(args)
    except Exception as exc:  # noqa: BLE001 - process boundary emits one structured error envelope
        print(
            json.dumps(
                {
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "cleanup_warnings": list(
                        getattr(exc, "cleanup_warnings", ())
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
