"""Transactional publishing of completed earnings-call artifact bundles.

The downloader owns local bundle creation.  This module only accepts a fully
materialized v2 bundle, verifies it without modifying it, and publishes a new
immutable version to Cosmos.  ``current`` is replaced atomically only after the
staged copy has passed independent remote size, SHA-256, and Zstandard checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import zstandard

ARTIFACT_SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json.zst"
REQUIRED_MEMBERS = frozenset({"transcript.txt.zst", "metadata.json.zst"})
MAX_BUNDLE_FILES = 16

_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,254}$")
_SAFE_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.zst$")
_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactValidationError(ValueError):
    """A local bundle is incomplete, corrupt, or inconsistent."""


class PublishError(RuntimeError):
    """A remote publish step failed before a safe commit was confirmed."""


CommandRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess[bytes]]
UuidFactory = Callable[[], uuid.UUID]


def _default_runner(
    command: Sequence[str], timeout: int
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=False,
        timeout=timeout,
        check=False,
    )


def _validate_executable(value: str, label: str) -> None:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{label} is invalid")
    if value.startswith("/"):
        parts = value.split("/")[1:]
        if not parts or any(
            not part or part in {".", ".."} or not _SAFE_PATH_PART.fullmatch(part)
            for part in parts
        ):
            raise ValueError(f"{label} must be a normalized absolute path")
        return
    if not _SAFE_EXECUTABLE.fullmatch(value):
        raise ValueError(f"{label} must be an executable name or absolute path")


def _validate_remote_root(value: str) -> PurePosixPath:
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        raise ValueError("Cosmos root must be a non-root normalized absolute path")
    if "//" in value or any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError("Cosmos root contains unsafe characters")
    parts = value.split("/")[1:]
    if any(
        part in {"", ".", ".."} or not _SAFE_PATH_PART.fullmatch(part) for part in parts
    ):
        raise ValueError("Cosmos root contains an unsafe path component")
    return PurePosixPath(value)


@dataclass(frozen=True)
class EarningsCallArtifactKey:
    year: int
    quarter: str
    ticker: str
    candidate_id: int

    def validate(self) -> None:
        if (
            isinstance(self.year, bool)
            or not isinstance(self.year, int)
            or not 2000 <= self.year <= 2100
        ):
            raise ValueError("year must be an integer between 2000 and 2100")
        if self.quarter not in {"Q1", "Q2", "Q3", "Q4"}:
            raise ValueError("quarter must be one of Q1, Q2, Q3, Q4")
        if not _SAFE_TICKER.fullmatch(self.ticker):
            raise ValueError("ticker must be an uppercase path-safe token")
        if (
            isinstance(self.candidate_id, bool)
            or not isinstance(self.candidate_id, int)
            or self.candidate_id <= 0
        ):
            raise ValueError("candidate_id must be a positive integer")

    def remote_parts(self) -> tuple[str, str, str, str]:
        self.validate()
        return str(self.year), self.quarter, self.ticker, str(self.candidate_id)


@dataclass(frozen=True)
class CosmosPublisherConfig:
    host: str = "pi@100.102.250.107"
    root: str = "/mnt/hdd8tb/valuechain/earnings_calls"
    ssh_executable: str = "ssh"
    rsync_executable: str = "rsync"
    connect_timeout_seconds: int = 10
    command_timeout_seconds: int = 120
    rsync_timeout_seconds: int = 300
    max_manifest_bytes: int = 1_000_000
    max_metadata_bytes: int = 2_000_000
    max_transcript_bytes: int = 64 * 1024 * 1024

    def validate(self) -> None:
        if (
            not _SAFE_HOST.fullmatch(self.host)
            or self.host.startswith("-")
            or self.host.count("@") > 1
            or self.host.endswith("@")
        ):
            raise ValueError("Cosmos host must be an explicit safe SSH host")
        _validate_remote_root(self.root)
        _validate_executable(self.ssh_executable, "ssh executable")
        _validate_executable(self.rsync_executable, "rsync executable")
        for name, value in (
            ("connect timeout", self.connect_timeout_seconds),
            ("command timeout", self.command_timeout_seconds),
            ("rsync timeout", self.rsync_timeout_seconds),
            ("manifest limit", self.max_manifest_bytes),
            ("metadata limit", self.max_metadata_bytes),
            ("transcript limit", self.max_transcript_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ArtifactMember:
    name: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class ValidatedArtifactBundle:
    directory: Path
    manifest_sha256: str
    members: tuple[ArtifactMember, ...]
    text_sha256: str
    text_chars: int


@dataclass(frozen=True)
class PublishResult:
    key: EarningsCallArtifactKey
    publication_uuid: str
    manifest_sha256: str
    version_path: str
    current_path: str
    current_target: str
    file_count: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_zstd(path: Path, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        with (
            path.open("rb") as compressed,
            zstandard.ZstdDecompressor().stream_reader(
                compressed, read_across_frames=True
            ) as reader,
        ):
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ArtifactValidationError(
                        f"decompressed {path.name} exceeds {limit} bytes"
                    )
                chunks.append(chunk)
    except (OSError, zstandard.ZstdError) as exc:
        raise ArtifactValidationError(f"invalid Zstandard member: {path.name}") from exc
    return b"".join(chunks)


def _verify_zstd(path: Path) -> None:
    try:
        with (
            path.open("rb") as compressed,
            zstandard.ZstdDecompressor().stream_reader(
                compressed, read_across_frames=True
            ) as reader,
        ):
            while reader.read(1024 * 1024):
                pass
    except (OSError, zstandard.ZstdError) as exc:
        raise ArtifactValidationError(f"invalid Zstandard member: {path.name}") from exc


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def validate_artifact_bundle(
    directory: Path,
    key: EarningsCallArtifactKey,
    *,
    max_manifest_bytes: int = 1_000_000,
    max_metadata_bytes: int = 2_000_000,
    max_transcript_bytes: int = 64 * 1024 * 1024,
) -> ValidatedArtifactBundle:
    """Validate a flat, compressed v2 downloader bundle without changing it."""
    key.validate()
    candidate = Path(directory)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ArtifactValidationError("artifact bundle must be a real local directory")
    try:
        resolved = candidate.resolve(strict=True)
        entries = tuple(sorted(resolved.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise ArtifactValidationError("artifact bundle cannot be read") from exc
    if not entries:
        raise ArtifactValidationError("artifact bundle is empty")
    if len(entries) > MAX_BUNDLE_FILES + 1:
        raise ArtifactValidationError("artifact bundle contains too many files")
    if any(not _regular_file(entry) for entry in entries):
        raise ArtifactValidationError("artifact bundle must contain only regular files")
    actual_names = {entry.name for entry in entries}
    if any(not _SAFE_FILENAME.fullmatch(name) for name in actual_names):
        raise ArtifactValidationError("artifact bundle contains an unsafe filename")
    manifest_path = resolved / MANIFEST_NAME
    if MANIFEST_NAME not in actual_names:
        raise ArtifactValidationError(f"artifact bundle is missing {MANIFEST_NAME}")

    manifest_bytes = _read_zstd(manifest_path, limit=max_manifest_bytes)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(
            "artifact manifest is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(manifest, dict):
        raise ArtifactValidationError("artifact manifest must be a JSON object")
    if manifest.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactValidationError("unsupported artifact manifest schema")
    manifest_candidate = manifest.get("accepted_url_id")
    if (
        isinstance(manifest_candidate, bool)
        or not isinstance(manifest_candidate, int)
        or manifest_candidate != key.candidate_id
    ):
        raise ArtifactValidationError(
            "manifest candidate does not match the logical key"
        )
    declared = manifest.get("files")
    if not isinstance(declared, dict) or not declared:
        raise ArtifactValidationError(
            "artifact manifest files must be a non-empty object"
        )
    if len(declared) > MAX_BUNDLE_FILES:
        raise ArtifactValidationError("artifact manifest declares too many files")
    if MANIFEST_NAME in declared:
        raise ArtifactValidationError("artifact manifest must not declare itself")
    if not REQUIRED_MEMBERS.issubset(declared):
        raise ArtifactValidationError("artifact manifest is missing required members")
    if actual_names != set(declared) | {MANIFEST_NAME}:
        raise ArtifactValidationError("bundle files do not exactly match the manifest")

    members: list[ArtifactMember] = []
    for filename in sorted(declared):
        if not isinstance(filename, str) or not _SAFE_FILENAME.fullmatch(filename):
            raise ArtifactValidationError("manifest contains an unsafe filename")
        expected = declared[filename]
        if not isinstance(expected, dict):
            raise ArtifactValidationError(f"invalid manifest entry for {filename}")
        expected_bytes = expected.get("bytes")
        expected_hash = expected.get("sha256")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes <= 0
            or not isinstance(expected_hash, str)
            or not _SHA256.fullmatch(expected_hash)
        ):
            raise ArtifactValidationError(f"invalid size/hash for {filename}")
        path = resolved / filename
        actual_size = path.stat().st_size
        actual_hash = _sha256_file(path)
        if actual_size != expected_bytes or actual_hash != expected_hash:
            raise ArtifactValidationError(f"manifest size/hash mismatch for {filename}")
        _verify_zstd(path)
        members.append(ArtifactMember(filename, actual_size, actual_hash))

    metadata_bytes = _read_zstd(
        resolved / "metadata.json.zst", limit=max_metadata_bytes
    )
    transcript_bytes = _read_zstd(
        resolved / "transcript.txt.zst", limit=max_transcript_bytes
    )
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        transcript_text = transcript_bytes.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("metadata/transcript is not valid UTF-8") from exc
    if not isinstance(metadata, dict):
        raise ArtifactValidationError("metadata must be a JSON object")
    if metadata.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactValidationError("unsupported metadata schema")
    metadata_candidate = metadata.get("accepted_url_id")
    if (
        isinstance(metadata_candidate, bool)
        or not isinstance(metadata_candidate, int)
        or metadata_candidate != key.candidate_id
    ):
        raise ArtifactValidationError(
            "metadata candidate does not match the logical key"
        )
    metadata_year = metadata.get("year")
    metadata_quarter = metadata.get("quarter")
    metadata_ticker = metadata.get("ticker")
    if (
        isinstance(metadata_year, bool)
        or not isinstance(metadata_year, int)
        or metadata_year != key.year
        or not isinstance(metadata_quarter, str)
        or metadata_quarter != key.quarter
        or not isinstance(metadata_ticker, str)
        or metadata_ticker != key.ticker
    ):
        raise ArtifactValidationError(
            "metadata year/quarter/ticker does not match the logical key"
        )
    text_sha256 = hashlib.sha256(transcript_bytes).hexdigest()
    if metadata.get("text_sha256") != text_sha256:
        raise ArtifactValidationError("metadata transcript hash does not match")
    text_chars = metadata.get("text_chars")
    if (
        isinstance(text_chars, bool)
        or not isinstance(text_chars, int)
        or text_chars != len(transcript_text)
    ):
        raise ArtifactValidationError("metadata transcript length does not match")

    manifest_member = ArtifactMember(
        MANIFEST_NAME,
        manifest_path.stat().st_size,
        _sha256_file(manifest_path),
    )
    members.append(manifest_member)
    return ValidatedArtifactBundle(
        directory=resolved,
        manifest_sha256=manifest_member.sha256,
        members=tuple(sorted(members, key=lambda member: member.name)),
        text_sha256=text_sha256,
        text_chars=len(transcript_text),
    )


_REMOTE_PREPARE = """\
import os, sys
base, staging_parent, versions_parent, stage = sys.argv[1:]
os.makedirs(base, mode=0o755, exist_ok=True)
os.makedirs(staging_parent, mode=0o755, exist_ok=True)
os.makedirs(versions_parent, mode=0o755, exist_ok=True)
for path in (base, staging_parent, versions_parent):
    if os.path.islink(path) or not os.path.isdir(path):
        raise RuntimeError("publication path is not a real directory: " + path)
if os.path.lexists(stage):
    raise FileExistsError("publication staging path already exists")
os.mkdir(stage, mode=0o700)
"""

_REMOTE_VERIFY_AND_PROMOTE = """\
import hashlib, json, os, stat, subprocess, sys
stage, version = sys.argv[1:3]
expected = json.loads(sys.argv[3])
if os.path.islink(stage) or not os.path.isdir(stage):
    raise RuntimeError("staging path is not a real directory")
if os.path.islink(os.path.dirname(stage)) or os.path.islink(os.path.dirname(version)):
    raise RuntimeError("staging/version parent cannot be a symlink")
if os.path.lexists(version):
    raise FileExistsError("immutable version already exists")
names = sorted(os.listdir(stage))
if names != sorted(expected):
    raise RuntimeError("remote bundle membership mismatch")
for name in names:
    path = os.path.join(stage, name)
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or os.path.islink(path):
        raise RuntimeError("remote bundle contains a non-regular file")
    specification = expected[name]
    if info.st_size != specification["bytes"]:
        raise RuntimeError("remote byte-size mismatch: " + name)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != specification["sha256"]:
        raise RuntimeError("remote SHA-256 mismatch: " + name)
    subprocess.run(
        ["zstd", "-q", "-t", "--", path],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    os.chmod(path, 0o444)
os.rename(stage, version)
# Some Cosmos/HDD mount configurations reject renaming a directory after its
# own write bit is removed.  Promotion is still atomic; make the immutable
# destination read-only immediately after the rename and before `current` can
# be updated by the separate commit operation.
os.chmod(version, 0o555)
"""

_REMOTE_COMMIT_CURRENT = """\
import os, sys
version, current, temporary, relative_target = sys.argv[1:]
if os.path.islink(version) or not os.path.isdir(version):
    raise RuntimeError("immutable version is unavailable")
if os.path.islink(os.path.dirname(current)):
    raise RuntimeError("current parent cannot be a symlink")
if os.path.lexists(current) and not os.path.islink(current):
    raise RuntimeError("current exists and is not a symlink")
if os.path.lexists(temporary):
    raise FileExistsError("temporary current pointer already exists")
os.symlink(relative_target, temporary)
os.replace(temporary, current)
"""

_REMOTE_READ_CURRENT = """\
import os, sys
current = sys.argv[1]
if not os.path.islink(current):
    raise RuntimeError("current is not a symlink")
sys.stdout.write(os.readlink(current))
"""


class EarningsCallPublisher:
    """Publish verified bundles to versioned Cosmos paths."""

    def __init__(
        self,
        config: CosmosPublisherConfig | None = None,
        *,
        runner: CommandRunner = _default_runner,
        uuid_factory: UuidFactory = uuid.uuid4,
    ) -> None:
        selected_config = config or CosmosPublisherConfig()
        selected_config.validate()
        self.config = selected_config
        self._root = _validate_remote_root(selected_config.root)
        self._runner = runner
        self._uuid_factory = uuid_factory

    def _ssh_command(self, arguments: Sequence[str]) -> tuple[str, ...]:
        remote_command = shlex.join(arguments)
        return (
            self.config.ssh_executable,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout_seconds}",
            "-o",
            "ServerAliveInterval=15",
            self.config.host,
            remote_command,
        )

    def _checked(self, operation: str, command: Sequence[str], timeout: int) -> None:
        try:
            completed = self._runner(tuple(command), timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PublishError(f"{operation} could not run: {exc}") from exc
        if completed.returncode:
            raw_detail = completed.stderr or completed.stdout or b""
            if isinstance(raw_detail, bytes):
                detail = raw_detail.decode("utf-8", errors="replace")
            else:
                detail = str(raw_detail)
            detail = detail.strip()[-1_000:]
            raise PublishError(
                f"{operation} failed with exit {completed.returncode}"
                + (f": {detail}" if detail else "")
            )

    def _current_points_to(self, current: PurePosixPath, expected_target: str) -> bool:
        command = self._ssh_command(
            ("python3", "-c", _REMOTE_READ_CURRENT, str(current))
        )
        try:
            completed = self._runner(command, self.config.command_timeout_seconds)
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode:
            return False
        raw_target = completed.stdout or b""
        if isinstance(raw_target, bytes):
            target = raw_target.decode("utf-8", errors="replace")
        else:
            target = str(raw_target)
        return target.strip() == expected_target

    def publish(self, directory: Path, key: EarningsCallArtifactKey) -> PublishResult:
        """Publish one immutable version and atomically repoint ``current``."""
        key.validate()
        bundle = validate_artifact_bundle(
            directory,
            key,
            max_manifest_bytes=self.config.max_manifest_bytes,
            max_metadata_bytes=self.config.max_metadata_bytes,
            max_transcript_bytes=self.config.max_transcript_bytes,
        )
        generated = self._uuid_factory()
        if not isinstance(generated, uuid.UUID):
            raise TypeError("uuid_factory must return uuid.UUID")
        publication_uuid = generated.hex

        base = self._root.joinpath(*key.remote_parts())
        staging_parent = base / ".staging"
        versions_parent = base / "versions"
        stage = staging_parent / publication_uuid
        version_id = f"{bundle.manifest_sha256[:16]}-{publication_uuid}"
        version = versions_parent / version_id
        current = base / "current"
        current_temporary = base / f".current-{publication_uuid}"
        relative_target = f"versions/{version_id}"

        prepare = self._ssh_command(
            (
                "python3",
                "-c",
                _REMOTE_PREPARE,
                str(base),
                str(staging_parent),
                str(versions_parent),
                str(stage),
            )
        )
        self._checked(
            "create unique Cosmos staging directory",
            prepare,
            self.config.command_timeout_seconds,
        )

        source = f"{bundle.directory}{os.sep}"
        destination = f"{self.config.host}:{stage}/"
        rsync_ssh_transport = shlex.join(
            (
                self.config.ssh_executable,
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={self.config.connect_timeout_seconds}",
                "-o",
                "ServerAliveInterval=15",
            )
        )
        rsync_command = (
            self.config.rsync_executable,
            "-a",
            "-e",
            rsync_ssh_transport,
            "--",
            source,
            destination,
        )
        self._checked(
            "rsync artifact bundle to Cosmos staging",
            rsync_command,
            self.config.rsync_timeout_seconds,
        )

        expected: Mapping[str, Mapping[str, int | str]] = {
            member.name: {"bytes": member.byte_size, "sha256": member.sha256}
            for member in bundle.members
        }
        expected_json = json.dumps(expected, sort_keys=True, separators=(",", ":"))
        verify_and_promote = self._ssh_command(
            (
                "python3",
                "-c",
                _REMOTE_VERIFY_AND_PROMOTE,
                str(stage),
                str(version),
                expected_json,
            )
        )
        self._checked(
            "verify and atomically promote staged Cosmos bundle",
            verify_and_promote,
            self.config.command_timeout_seconds,
        )

        # This is deliberately the final remote operation.  All fallible data
        # transfer, validation, promotion, and chmod work happens before the
        # one atomic os.replace that changes the reader-visible pointer.
        commit_current = self._ssh_command(
            (
                "python3",
                "-c",
                _REMOTE_COMMIT_CURRENT,
                str(version),
                str(current),
                str(current_temporary),
                relative_target,
            )
        )
        try:
            self._checked(
                "atomically update Cosmos current pointer",
                commit_current,
                self.config.command_timeout_seconds,
            )
        except PublishError:
            # A dead SSH connection or local timeout is ambiguous: the remote
            # os.replace may already have committed.  Read back the exact
            # relative target before deciding whether this publication failed.
            if not self._current_points_to(current, relative_target):
                raise
        return PublishResult(
            key=key,
            publication_uuid=publication_uuid,
            manifest_sha256=bundle.manifest_sha256,
            version_path=str(version),
            current_path=str(current),
            current_target=relative_target,
            file_count=len(bundle.members),
        )
