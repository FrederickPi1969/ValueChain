import shutil
import subprocess
from pathlib import Path

import pytest

from valuechain.earnings_call_artifacts import compress_artifact_directory, compress_file


pytestmark = pytest.mark.skipif(not shutil.which("zstd"), reason="zstd is not installed")


def test_compress_file_round_trip_and_removes_plaintext(tmp_path) -> None:
    source = tmp_path / "transcript.txt"
    expected = ("Operator: welcome to the earnings call.\n" * 1_000).encode()
    source.write_bytes(expected)
    compressed = compress_file(source)
    assert compressed.name == "transcript.txt.zst"
    assert compressed.exists()
    assert not source.exists()
    restored = subprocess.run(
        ["zstd", "-q", "-d", "-c", str(compressed)],
        check=True,
        capture_output=True,
    ).stdout
    assert restored == expected


def test_compress_artifact_directory_covers_pdf_json_and_text(tmp_path) -> None:
    paths = [tmp_path / "transcript.txt", tmp_path / "metadata.json", tmp_path / "source.pdf"]
    for path in paths:
        path.write_bytes(b"earnings-call-artifact" * 100)
    mapping = compress_artifact_directory(tmp_path)
    assert set(mapping) == set(paths)
    assert all(Path(f"{path}.zst").exists() for path in paths)
    assert not any(path.exists() for path in paths)
