import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.valuechain_remote_opencli_extract as helper
from valuechain.remote_opencli import (
    RemoteOpenCLIConfig,
    RemoteOpenCLIError,
    RemoteOpenCLIExtractor,
)


def test_remote_extractor_uses_one_ssh_helper_call() -> None:
    calls = []

    def runner(command, timeout):
        calls.append((list(command), timeout))
        payload = {
            "success": True,
            "text": "Operator: welcome. " * 50,
            "method": "opencli.remote_helper.paginated",
            "cleanup_warnings": [],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    extractor = RemoteOpenCLIExtractor(
        RemoteOpenCLIConfig(host="macmini-m4", profile="2wz4s44u"),
        runner=runner,
    )
    text, method = extractor.extract("https://example.com/call", "vc-test-1")
    assert len(calls) == 1
    assert calls[0][0][0] == "ssh"
    assert "macmini-m4" in calls[0][0]
    assert "valuechain-opencli-extract" in calls[0][0][-1]
    assert text.startswith("Operator")
    assert method == "opencli.remote_helper.paginated"


def test_remote_extractor_rejects_cleanup_warning() -> None:
    def runner(command, timeout):
        payload = {
            "success": True,
            "text": "Operator: welcome. " * 50,
            "method": "opencli.remote_helper.paginated",
            "cleanup_warnings": ["tab close failed"],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    extractor = RemoteOpenCLIExtractor(
        RemoteOpenCLIConfig(host="macmini-m4", profile="2wz4s44u"),
        runner=runner,
    )
    with pytest.raises(RemoteOpenCLIError, match="tab/session"):
        extractor.extract("https://example.com/call", "vc-test-2")


def test_remote_config_rejects_empty_host_and_profile() -> None:
    with pytest.raises(ValueError):
        RemoteOpenCLIExtractor(RemoteOpenCLIConfig(host="", profile=""))


def helper_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        profile="fixed-profile",
        session="vc-test",
        opencli="/bin/echo",
        url="https://example.com/call",
        settle_seconds=0,
        chunk_size=20_000,
        max_chars=1_000_000,
        deadline=30,
        lock_dir=str(tmp_path),
    )


def test_helper_rejects_content_length_that_disagrees_with_offsets() -> None:
    raw = json.dumps(
        {
            "content": "word " * 120,
            "start": 0,
            "end": 1_000,
            "next_start_char": None,
            "total_chars": 1_000,
        }
    )
    with pytest.raises(helper.HelperError, match="content length"):
        helper.parse_chunk(raw, 0)


def test_helper_primary_failure_preserves_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_attempts = 0

    def fake_run(command, deadline, *, cap):
        nonlocal cleanup_attempts
        if "tab" in command and "new" in command:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"page": "tab-123"}), ""
            )
        if "extract" in command:
            raise helper.HelperError("primary extraction failed")
        if "close" in command:
            cleanup_attempts += 1
            raise helper.HelperError("cleanup also failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(helper, "run", fake_run)
    with pytest.raises(helper.ExtractionFailure, match="cleanup failures") as raised:
        helper.extract(helper_args(tmp_path))
    assert cleanup_attempts == 3
    assert raised.value.cleanup_warnings == (
        "tab close: cleanup also failed",
        "lease close: cleanup also failed",
    )


def test_helper_rejects_final_assembled_length_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Operator welcome analyst question management answer. " * 20

    def fake_run(command, deadline, *, cap):
        if "tab" in command and "new" in command:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"page": "tab-123"}), ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(helper, "run", fake_run)
    monkeypatch.setattr(
        helper,
        "parse_chunk",
        lambda raw, requested_start: (text, None, len(text) + 10),
    )
    with pytest.raises(helper.ExtractionFailure, match="assembled OpenCLI text length"):
        helper.extract(helper_args(tmp_path))


def test_remote_extractor_surfaces_structured_cleanup_failure() -> None:
    def runner(command, timeout):
        payload = {
            "success": False,
            "error": "ExtractionFailure: primary; cleanup failures: tab close failed",
            "cleanup_warnings": ["tab close failed"],
        }
        return subprocess.CompletedProcess(command, 1, "", json.dumps(payload))

    extractor = RemoteOpenCLIExtractor(
        RemoteOpenCLIConfig(host="macmini-m4", profile="fixed-profile"),
        runner=runner,
    )
    with pytest.raises(RemoteOpenCLIError, match="tab close failed"):
        extractor.extract("https://example.com/call", "vc-test-structured-error")


def test_helper_main_emits_cleanup_warnings_in_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = helper.ExtractionFailure(
        helper.HelperError("primary extraction failed"),
        ["tab close: cleanup failed"],
    )

    def fail_extract(args):
        raise failure

    monkeypatch.setattr(helper, "extract", fail_extract)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "valuechain-opencli-extract",
            "--opencli",
            "/bin/echo",
            "--profile",
            "fixed-profile",
            "--session",
            "vc-test",
            "--url",
            "https://example.com/call",
        ],
    )
    assert helper.main() == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["success"] is False
    assert payload["cleanup_warnings"] == ["tab close: cleanup failed"]
    assert "primary extraction failed" in payload["error"]
