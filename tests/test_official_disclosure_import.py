from pathlib import Path

import pytest

from valuechain.official_disclosure_import import (
    discover_import_packages,
    inspect_import_package,
)


def test_official_import_discovers_only_complete_files(tmp_path: Path) -> None:
    incoming = tmp_path / "hkex" / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "announcements-2026.csv").write_text("issuer_name\nExample\n")
    (incoming / "still-writing.partial").write_bytes(b"partial")
    (incoming / "old.manifest.json").write_text("{}")

    paths = discover_import_packages(tmp_path, "hkex")

    assert [path.name for path in paths] == ["announcements-2026.csv"]


def test_official_import_package_is_content_addressed(tmp_path: Path) -> None:
    path = tmp_path / "asx-comnews-2025.csv"
    path.write_bytes(b"issuer_name,filing_date\nExample,2025-12-01\n")

    package = inspect_import_package("asx", path)

    assert package.object_key.startswith("official-package:")
    assert len(package.sha256) == 64
    assert package.effective_date.isoformat() == "2025-12-31"


def test_official_import_rejects_unapproved_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported official import source"):
        discover_import_packages(tmp_path, "random_portal")
