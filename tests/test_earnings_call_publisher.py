import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import zstandard

import valuechain.earnings_call_publisher as publisher_module
from valuechain.earnings_call_publisher import (
    ArtifactValidationError,
    CosmosPublisherConfig,
    EarningsCallArtifactKey,
    EarningsCallPublisher,
    PublishError,
    validate_artifact_bundle,
)

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def compress(path: Path, payload: bytes) -> None:
    path.write_bytes(zstandard.ZstdCompressor(level=1).compress(payload))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_bundle(
    root: Path,
    *,
    candidate_id: int = 42,
    year: int = 2026,
    quarter: str = "Q1",
    ticker: str = "MSFT",
    include_identity: bool = True,
) -> Path:
    root.mkdir()
    transcript = ("Operator: welcome to the Q1 earnings call.\n" * 100).encode()
    metadata = {
        "artifact_schema_version": 2,
        "accepted_url_id": candidate_id,
        "text_chars": len(transcript.decode()),
        "text_sha256": hashlib.sha256(transcript).hexdigest(),
    }
    if include_identity:
        metadata.update({"year": year, "quarter": quarter, "ticker": ticker})
    compress(root / "transcript.txt.zst", transcript)
    compress(
        root / "metadata.json.zst",
        json.dumps(metadata, sort_keys=True).encode(),
    )
    files = {
        path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(root.iterdir())
    }
    manifest = {
        "artifact_schema_version": 2,
        "accepted_url_id": candidate_id,
        "files": files,
    }
    compress(
        root / "manifest.json.zst",
        json.dumps(manifest, sort_keys=True).encode(),
    )
    return root


class RecordingRunner:
    def __init__(self, *, fail_call: int | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.fail_call = fail_call

    def __call__(self, command, timeout: int) -> subprocess.CompletedProcess[bytes]:
        normalized = tuple(command)
        self.calls.append((normalized, timeout))
        call_number = len(self.calls)
        if self.fail_call == call_number:
            return subprocess.CompletedProcess(
                normalized, 23, b"", b"simulated failure"
            )
        return subprocess.CompletedProcess(normalized, 0, b"", b"")


class AmbiguousCommitRunner(RecordingRunner):
    def __init__(self, expected_target: str) -> None:
        super().__init__()
        self.expected_target = expected_target

    def __call__(self, command, timeout: int) -> subprocess.CompletedProcess[bytes]:
        normalized = tuple(command)
        self.calls.append((normalized, timeout))
        call_number = len(self.calls)
        if call_number == 4:
            raise subprocess.TimeoutExpired(normalized, timeout)
        if call_number == 5:
            return subprocess.CompletedProcess(
                normalized,
                0,
                self.expected_target.encode("utf-8"),
                b"",
            )
        return subprocess.CompletedProcess(normalized, 0, b"", b"")


def test_publish_is_staged_verified_versioned_and_committed_last(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    runner = RecordingRunner()
    key = EarningsCallArtifactKey(2026, "Q1", "MSFT", 42)
    publisher = EarningsCallPublisher(runner=runner, uuid_factory=lambda: FIXED_UUID)

    result = publisher.publish(bundle, key)

    assert len(runner.calls) == 4
    prepare, rsync, verify_and_promote, commit = [call[0] for call in runner.calls]
    publication = FIXED_UUID.hex
    assert prepare[0] == "ssh"
    assert f"/.staging/{publication}" in prepare[-1]
    assert rsync[0] == "rsync"
    assert "--delete" not in rsync
    assert rsync[2] == "-e"
    assert "BatchMode=yes" in rsync[3]
    assert "ConnectTimeout=10" in rsync[3]
    assert "ServerAliveInterval=15" in rsync[3]
    assert rsync[-1].endswith(f"/.staging/{publication}/")
    assert "remote SHA-256 mismatch" in verify_and_promote[-1]
    assert "os.rename(stage, version)" in verify_and_promote[-1]
    assert verify_and_promote[-1].index("os.rename(stage, version)") < verify_and_promote[-1].index(
        "os.chmod(version, 0o555)"
    )
    assert "os.replace(temporary, current)" in commit[-1]
    assert all(
        "os.replace(temporary, current)" not in call[0][-1]
        for call in runner.calls[:-1]
    )
    assert result.current_target.startswith("versions/")
    assert result.current_path.endswith("/2026/Q1/MSFT/42/current")
    assert result.version_path.endswith(result.current_target)
    assert result.file_count == 3


def test_remote_verification_failure_never_runs_promote_or_current_update(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path / "bundle", ticker="META")
    runner = RecordingRunner(fail_call=3)
    publisher = EarningsCallPublisher(runner=runner, uuid_factory=lambda: FIXED_UUID)

    with pytest.raises(PublishError, match="verify and atomically promote"):
        publisher.publish(bundle, EarningsCallArtifactKey(2026, "Q1", "META", 42))

    assert len(runner.calls) == 3
    verification_script = runner.calls[-1][0][-1]
    assert verification_script.index(
        "remote SHA-256 mismatch"
    ) < verification_script.index("os.rename(stage, version)")
    assert not any(
        "os.replace(temporary, current)" in call[0][-1] for call in runner.calls
    )


def test_pointer_failure_is_the_only_command_after_immutable_promotion(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path / "bundle", ticker="GOOG")
    runner = RecordingRunner(fail_call=4)
    publisher = EarningsCallPublisher(runner=runner, uuid_factory=lambda: FIXED_UUID)

    with pytest.raises(PublishError, match="update Cosmos current pointer"):
        publisher.publish(bundle, EarningsCallArtifactKey(2026, "Q1", "GOOG", 42))

    assert len(runner.calls) == 5
    assert "os.rename(stage, version)" in runner.calls[-3][0][-1]
    assert "os.replace(temporary, current)" in runner.calls[-2][0][-1]
    assert "os.readlink(current)" in runner.calls[-1][0][-1]


def test_ambiguous_commit_timeout_is_success_when_readback_matches(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    manifest_hash = sha256(bundle / "manifest.json.zst")
    expected_target = f"versions/{manifest_hash[:16]}-{FIXED_UUID.hex}"
    runner = AmbiguousCommitRunner(expected_target)
    publisher = EarningsCallPublisher(runner=runner, uuid_factory=lambda: FIXED_UUID)

    result = publisher.publish(
        bundle,
        EarningsCallArtifactKey(2026, "Q1", "MSFT", 42),
    )

    assert result.current_target == expected_target
    assert len(runner.calls) == 5
    assert "os.replace(temporary, current)" in runner.calls[3][0][-1]
    assert "os.readlink(current)" in runner.calls[4][0][-1]


def test_local_hash_tampering_is_rejected_before_remote_commands(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path / "bundle", ticker="TSLA")
    with (bundle / "transcript.txt.zst").open("ab") as handle:
        handle.write(b"tampered")
    runner = RecordingRunner()
    publisher = EarningsCallPublisher(runner=runner, uuid_factory=lambda: FIXED_UUID)

    with pytest.raises(ArtifactValidationError, match="size/hash mismatch"):
        publisher.publish(bundle, EarningsCallArtifactKey(2026, "Q1", "TSLA", 42))

    assert runner.calls == []


def test_manifest_candidate_must_match_logical_key(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle", candidate_id=41, ticker="NVDA")
    with pytest.raises(ArtifactValidationError, match="logical key"):
        validate_artifact_bundle(
            bundle, EarningsCallArtifactKey(2026, "Q1", "NVDA", 42)
        )


@pytest.mark.parametrize(
    "key",
    [
        EarningsCallArtifactKey(2025, "Q1", "MSFT", 42),
        EarningsCallArtifactKey(2026, "Q2", "MSFT", 42),
        EarningsCallArtifactKey(2026, "Q1", "GOOG", 42),
    ],
)
def test_metadata_period_and_ticker_must_match_logical_key(
    tmp_path: Path,
    key: EarningsCallArtifactKey,
) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    with pytest.raises(ArtifactValidationError, match="year/quarter/ticker"):
        validate_artifact_bundle(bundle, key)


def test_generic_downloader_bundle_without_logical_identity_is_not_publishable(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path / "bundle", include_identity=False)
    with pytest.raises(ArtifactValidationError, match="year/quarter/ticker"):
        validate_artifact_bundle(
            bundle,
            EarningsCallArtifactKey(2026, "Q1", "MSFT", 42),
        )


def test_unlisted_member_and_symlink_are_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle", ticker="WMT")
    compress(bundle / "extra.txt.zst", b"not declared")
    with pytest.raises(ArtifactValidationError, match="exactly match"):
        validate_artifact_bundle(bundle, EarningsCallArtifactKey(2026, "Q1", "WMT", 42))

    (bundle / "extra.txt.zst").unlink()
    (bundle / "escape.zst").symlink_to(bundle / "transcript.txt.zst")
    with pytest.raises(ArtifactValidationError, match="regular files"):
        validate_artifact_bundle(bundle, EarningsCallArtifactKey(2026, "Q1", "WMT", 42))


@pytest.mark.parametrize(
    "key",
    [
        EarningsCallArtifactKey(2026, "Q5", "MSFT", 42),
        EarningsCallArtifactKey(2026, "Q1", "../MSFT", 42),
        EarningsCallArtifactKey(2026, "Q1", "msft", 42),
        EarningsCallArtifactKey(2026, "Q1", "MSFT", 0),
    ],
)
def test_logical_key_rejects_unsafe_tokens(key: EarningsCallArtifactKey) -> None:
    with pytest.raises(ValueError):
        key.validate()


@pytest.mark.parametrize(
    "config",
    [
        CosmosPublisherConfig(host="-oProxyCommand=bad"),
        CosmosPublisherConfig(host="pi@host@other"),
        CosmosPublisherConfig(root="/mnt/valuechain/../escape"),
        CosmosPublisherConfig(root="relative/path"),
        CosmosPublisherConfig(ssh_executable="ssh --bad"),
    ],
)
def test_publisher_config_rejects_unsafe_paths_and_commands(
    config: CosmosPublisherConfig,
) -> None:
    with pytest.raises(ValueError):
        EarningsCallPublisher(config)


def test_uuid_factory_cannot_inject_a_path_token(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    runner = RecordingRunner()
    publisher = EarningsCallPublisher(
        runner=runner,
        uuid_factory=lambda: "../../escape",  # type: ignore[return-value]
    )
    with pytest.raises(TypeError, match="uuid.UUID"):
        publisher.publish(bundle, EarningsCallArtifactKey(2026, "Q1", "MSFT", 42))
    assert runner.calls == []


def test_remote_scripts_are_valid_python() -> None:
    for script in (
        publisher_module._REMOTE_PREPARE,
        publisher_module._REMOTE_VERIFY_AND_PROMOTE,
        publisher_module._REMOTE_COMMIT_CURRENT,
        publisher_module._REMOTE_READ_CURRENT,
    ):
        compile(script, "<remote-publisher-script>", "exec")


def test_current_pointer_script_atomically_replaces_only_a_symlink(
    tmp_path: Path,
) -> None:
    versions = tmp_path / "versions"
    old_version = versions / "old"
    new_version = versions / "new"
    old_version.mkdir(parents=True)
    new_version.mkdir()
    current = tmp_path / "current"
    temporary = tmp_path / ".current-test"
    current.symlink_to("versions/old")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            publisher_module._REMOTE_COMMIT_CURRENT,
            str(new_version),
            str(current),
            str(temporary),
            "versions/new",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert os.readlink(current) == "versions/new"
    assert not temporary.exists()

    current.unlink()
    current.write_text("must not be overwritten", encoding="utf-8")
    rejected = subprocess.run(
        [
            sys.executable,
            "-c",
            publisher_module._REMOTE_COMMIT_CURRENT,
            str(old_version),
            str(current),
            str(temporary),
            "versions/old",
        ],
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert current.read_text(encoding="utf-8") == "must not be overwritten"
