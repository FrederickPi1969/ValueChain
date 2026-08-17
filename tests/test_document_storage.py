from __future__ import annotations

import hashlib
from pathlib import Path

from valuechain.document_storage import (
    finalize_compression,
    logical_path,
    materialized_path,
    prepare_compression,
    prepare_document_record,
    read_bytes,
    resolve_storage_path,
)


def test_verified_compression_round_trip_and_finalize(tmp_path: Path) -> None:
    content = b"ValueChain filing evidence\n" * 50_000
    source = tmp_path / "filing.html"
    source.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()

    document = {
        "local_path": str(source),
        "byte_size": len(content),
        "sha256": sha256,
        "status": "complete",
        "metadata": {"source": "test"},
    }
    prepared = prepare_document_record(document, enabled=True)

    assert prepared is not None
    stored = Path(document["local_path"])
    assert stored.name == "filing.html.zst"
    assert source.exists()
    assert stored.exists()
    assert read_bytes(stored) == content
    assert document["byte_size"] == len(content)
    assert document["sha256"] == sha256
    assert document["metadata"]["source"] == "test"
    assert document["metadata"]["storage"]["stored_byte_size"] < len(content)

    finalize_compression(prepared)

    assert not source.exists()
    assert resolve_storage_path(source) == stored


def test_existing_zstd_is_reconciled_after_interrupted_database_update(
    tmp_path: Path,
) -> None:
    content = b"same immutable record\n" * 50_000
    source = tmp_path / "submission.txt"
    source.write_bytes(content)
    prepared = prepare_compression(source)
    assert prepared is not None

    document = {
        "local_path": str(source),
        "sha256": hashlib.sha256(content).hexdigest(),
        "status": "complete",
    }
    reconciled = prepare_document_record(document, enabled=True)

    assert reconciled is not None
    assert Path(document["local_path"]) == prepared.stored_path
    finalize_compression(reconciled)
    assert not source.exists()
    assert read_bytes(prepared.stored_path) == content


def test_materialized_path_preserves_original_suffix(tmp_path: Path) -> None:
    content = b"%PDF-1.7\n" + (b"object data\n" * 30_000)
    source = tmp_path / "report.pdf"
    source.write_bytes(content)
    prepared = prepare_compression(source)
    assert prepared is not None
    finalize_compression(prepared)

    with materialized_path(prepared.stored_path) as readable:
        assert readable.suffix == ".pdf"
        assert readable.read_bytes() == content
        temporary = readable

    assert not temporary.exists()
    assert logical_path(prepared.stored_path).name == "report.pdf"


def test_compression_skips_already_compressed_or_tiny_content(tmp_path: Path) -> None:
    archive = tmp_path / "package.zip"
    archive.write_bytes(b"x" * 300_000)
    tiny = tmp_path / "small.json"
    tiny.write_bytes(b"{}")

    assert prepare_compression(archive) is None
    assert prepare_compression(tiny) is None
