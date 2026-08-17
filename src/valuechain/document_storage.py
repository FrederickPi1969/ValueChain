from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import zstandard

ZSTD_SUFFIX = ".zst"
DEFAULT_MIN_BYTES = 256 * 1024
DEFAULT_MIN_SAVINGS_RATIO = 0.05
COMPRESSIBLE_SUFFIXES = {
    ".htm",
    ".html",
    ".json",
    ".pdf",
    ".txt",
    ".xbrl",
    ".xhtml",
    ".xml",
}


@dataclass(frozen=True)
class PreparedCompression:
    source_path: Path
    stored_path: Path
    original_size: int
    stored_size: int
    sha256: str
    level: int


def compression_enabled() -> bool:
    return os.getenv("VALUECHAIN_DOCUMENT_COMPRESSION", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def logical_path(path: Path) -> Path:
    return path.with_suffix("") if path.suffix.lower() == ZSTD_SUFFIX else path


def resolve_storage_path(path: Path) -> Path:
    if path.is_file():
        return path
    compressed = Path(f"{path}{ZSTD_SUFFIX}")
    return compressed if compressed.is_file() else path


def is_compressed_path(path: Path) -> bool:
    return path.suffix.lower() == ZSTD_SUFFIX


def is_compressible(path: Path, byte_size: int, *, min_bytes: int) -> bool:
    return (
        not is_compressed_path(path)
        and logical_path(path).suffix.lower() in COMPRESSIBLE_SUFFIXES
        and byte_size >= min_bytes
    )


def _hash_and_size(handle: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def inspect_compressed(path: Path) -> tuple[str, int]:
    with (
        path.open("rb") as compressed,
        zstandard.ZstdDecompressor().stream_reader(compressed) as reader,
    ):
        return _hash_and_size(reader)


def prepare_compression(
    path: Path,
    *,
    expected_sha256: str = "",
    level: int = 3,
    min_bytes: int = DEFAULT_MIN_BYTES,
    min_savings_ratio: float = DEFAULT_MIN_SAVINGS_RATIO,
    force: bool = False,
) -> PreparedCompression | None:
    path = resolve_storage_path(path)
    if is_compressed_path(path):
        return None
    stat = path.stat()
    if not is_compressible(path, stat.st_size, min_bytes=min_bytes):
        return None
    stored_path = Path(f"{path}{ZSTD_SUFFIX}")
    if stored_path.exists():
        actual_sha256, original_size = inspect_compressed(stored_path)
    else:
        temp_path = stored_path.with_name(f".{stored_path.name}.tmp-{os.getpid()}")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source, temp_path.open("xb") as target:
                compressor = zstandard.ZstdCompressor(level=level)
                with compressor.stream_writer(target, closefd=False) as writer:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        writer.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            original_size = stat.st_size
            actual_sha256 = digest.hexdigest()
            verified_sha256, verified_size = inspect_compressed(temp_path)
            if verified_sha256 != actual_sha256 or verified_size != original_size:
                raise OSError(f"zstd verification failed for {path}")
            os.replace(temp_path, stored_path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp_path.unlink(missing_ok=True)
    if expected_sha256 and actual_sha256 != expected_sha256:
        stored_path.unlink(missing_ok=True)
        raise OSError(f"source checksum changed while compressing {path}")
    stored_size = stored_path.stat().st_size
    savings_ratio = 1.0 - (stored_size / original_size)
    if not force and savings_ratio < min_savings_ratio:
        stored_path.unlink(missing_ok=True)
        return None
    return PreparedCompression(
        source_path=path,
        stored_path=stored_path,
        original_size=original_size,
        stored_size=stored_size,
        sha256=actual_sha256,
        level=level,
    )


def prepare_document_record(
    document: dict[str, Any],
    *,
    enabled: bool | None = None,
    force: bool = False,
) -> PreparedCompression | None:
    if document.get("status", "complete") != "complete":
        return None
    recorded_path = Path(str(document.get("local_path") or ""))
    path = resolve_storage_path(recorded_path)
    if not path.is_file():
        return None
    if is_compressed_path(path):
        sha256, original_size = inspect_compressed(path)
        expected_sha256 = str(document.get("sha256") or "")
        if expected_sha256 and sha256 != expected_sha256:
            raise OSError(f"compressed checksum does not match record for {path}")
        metadata = dict(document.get("metadata") or {})
        metadata["storage"] = {
            **dict(metadata.get("storage") or {}),
            "compression": "zstd",
            "original_byte_size": original_size,
            "stored_byte_size": path.stat().st_size,
            "original_filename": logical_path(path).name,
        }
        document.update(
            {
                "local_path": str(path),
                "byte_size": original_size,
                "sha256": sha256,
                "metadata": metadata,
            }
        )
        return PreparedCompression(
            source_path=logical_path(path),
            stored_path=path,
            original_size=original_size,
            stored_size=path.stat().st_size,
            sha256=sha256,
            level=int(metadata["storage"].get("level") or 3),
        )
    if enabled is None:
        enabled = compression_enabled()
    if not enabled and not force:
        return None
    prepared = prepare_compression(
        path,
        expected_sha256=str(document.get("sha256") or ""),
        level=int(os.getenv("VALUECHAIN_DOCUMENT_COMPRESSION_LEVEL", "3")),
        min_bytes=int(
            os.getenv(
                "VALUECHAIN_DOCUMENT_COMPRESSION_MIN_BYTES",
                str(DEFAULT_MIN_BYTES),
            )
        ),
        force=force,
    )
    if prepared is None:
        return None
    metadata = dict(document.get("metadata") or {})
    metadata["storage"] = {
        "compression": "zstd",
        "level": prepared.level,
        "original_byte_size": prepared.original_size,
        "stored_byte_size": prepared.stored_size,
        "original_filename": prepared.source_path.name,
    }
    document.update(
        {
            "local_path": str(prepared.stored_path),
            "byte_size": prepared.original_size,
            "sha256": prepared.sha256,
            "metadata": metadata,
        }
    )
    return prepared


def finalize_compression(prepared: PreparedCompression | None) -> None:
    if prepared is None:
        return
    if prepared.source_path != prepared.stored_path:
        prepared.source_path.unlink(missing_ok=True)


@contextmanager
def open_binary(path: Path) -> Iterator[BinaryIO]:
    resolved = resolve_storage_path(path)
    if not is_compressed_path(resolved):
        with resolved.open("rb") as handle:
            yield handle
        return
    with (
        resolved.open("rb") as compressed,
        zstandard.ZstdDecompressor().stream_reader(compressed) as reader,
    ):
        yield reader


def read_bytes(path: Path) -> bytes:
    with open_binary(path) as handle:
        return handle.read()


def read_text(path: Path, encoding: str = "utf-8", errors: str = "strict") -> str:
    return read_bytes(path).decode(encoding, errors)


@contextmanager
def materialized_path(path: Path) -> Iterator[Path]:
    resolved = resolve_storage_path(path)
    if not is_compressed_path(resolved):
        yield resolved
        return
    suffix = logical_path(resolved).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        with open_binary(resolved) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                temporary.write(chunk)
    try:
        yield temporary_path
    finally:
        temporary_path.unlink(missing_ok=True)


def iter_bytes(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with open_binary(path) as handle:
        yield from iter(lambda: handle.read(chunk_size), b"")
