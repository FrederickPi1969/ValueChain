"""Zstandard storage helpers for earnings-call artifacts."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

COMPRESSIBLE_SUFFIXES = {".txt", ".json", ".pdf", ".html", ".md", ".vtt"}


def compress_file(path: Path, *, level: int = 10, threads: int = 2) -> Path:
    """Atomically replace one artifact with a verified ``.zst`` file."""
    if path.suffix == ".zst":
        return path
    if not path.is_file():
        raise FileNotFoundError(path)
    executable = shutil.which("zstd")
    if not executable:
        raise RuntimeError("zstd is required for earnings-call artifact storage")
    destination = Path(f"{path}.zst")
    temporary = Path(f"{destination}.tmp-{os.getpid()}")
    try:
        subprocess.run(
            [executable, "-q", f"-T{max(1, threads)}", f"-{level}", "-f", str(path), "-o", str(temporary)],
            check=True,
            timeout=180,
        )
        subprocess.run([executable, "-q", "-t", str(temporary)], check=True, timeout=60)
        temporary.replace(destination)
        path.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def compress_artifact_directory(
    directory: Path, *, level: int = 10, threads: int = 2
) -> dict[Path, Path]:
    """Compress every supported artifact below a single candidate directory."""
    compressed: dict[Path, Path] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in COMPRESSIBLE_SUFFIXES:
            compressed[path] = compress_file(path, level=level, threads=threads)
    return compressed
