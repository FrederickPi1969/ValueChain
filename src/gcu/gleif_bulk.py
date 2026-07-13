from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from gcu.http import PoliteHttpClient


class GleifGoldenCopy:
    """Download and normalize GLEIF Golden Copy bulk files without loading them into memory."""

    BASE_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes"
    FILE_TYPES = {"lei2", "rr", "repex"}
    FORMATS = {"csv", "json", "xml"}
    DELTAS = {"IntraDay", "LastDay", "LastWeek", "LastMonth"}

    def __init__(self, client: PoliteHttpClient) -> None:
        self.client = client

    @classmethod
    def url(cls, *, file_type: str, file_format: str) -> str:
        normalized_type = file_type.strip().lower()
        normalized_format = file_format.strip().lower()
        if normalized_type not in cls.FILE_TYPES:
            raise ValueError(
                f"file_type must be one of {', '.join(sorted(cls.FILE_TYPES))}; "
                f"received {file_type!r}"
            )
        if normalized_format not in cls.FORMATS:
            raise ValueError(
                f"file_format must be one of {', '.join(sorted(cls.FORMATS))}; "
                f"received {file_format!r}"
            )
        return f"{cls.BASE_URL}/{normalized_type}/latest.{normalized_format}"

    def download(
        self,
        *,
        file_type: str,
        file_format: str,
        output_path: Path,
        delta: str | None = None,
    ) -> dict[str, Any]:
        url = self.url(file_type=file_type, file_format=file_format)
        params: dict[str, str] = {}
        if delta:
            normalized_delta = delta.strip()
            if normalized_delta not in self.DELTAS:
                raise ValueError(
                    f"delta must be one of {', '.join(sorted(self.DELTAS))}; received {delta!r}"
                )
            params["delta"] = normalized_delta

        output_path.parent.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC)
        with self.client.stream_to_temporary_file(url, params=params) as payload:
            if not payload.first_bytes.startswith(b"PK"):
                raise ValueError("GLEIF Golden Copy response was not a ZIP archive")
            shutil.copyfile(payload.temporary_path, output_path)
            result = {
                "http_status": payload.http_status,
                "media_type": payload.media_type,
                "content_length": payload.content_length,
                "sha256": payload.sha256,
                "final_url": payload.final_url,
            }
        completed_at = datetime.now(UTC)
        manifest = {
            "source_id": "gleif_golden_copy",
            "file_type": file_type.lower(),
            "file_format": file_format.lower(),
            "delta": delta,
            "request_url": url,
            "query_parameter_names": sorted(params),
            "output_path": str(output_path),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            **result,
        }
        manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest["manifest_path"] = str(manifest_path)
        return manifest


CANONICAL_FIELDS = [
    "entity_id",
    "source_id",
    "source_entity_id",
    "legal_name",
    "jurisdiction",
    "exchange",
    "ticker",
    "lei",
    "isin",
    "local_registry_id",
    "entity_status",
    "registration_status",
    "registration_authority_id",
    "source_member",
]


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _select_header(
    fieldnames: list[str],
    *,
    exact: tuple[str, ...],
    suffixes: tuple[str, ...] = (),
) -> str | None:
    indexed = {field: _normalized_header(field) for field in fieldnames}
    exact_normalized = {_normalized_header(value) for value in exact}
    for field, normalized in indexed.items():
        if normalized in exact_normalized:
            return field
    suffix_normalized = tuple(_normalized_header(value) for value in suffixes)
    for field, normalized in indexed.items():
        if any(normalized.endswith(suffix) for suffix in suffix_normalized):
            return field
    return None


def _map_headers(fieldnames: list[str]) -> dict[str, str | None]:
    return {
        "lei": _select_header(fieldnames, exact=("LEI",)),
        "legal_name": _select_header(
            fieldnames,
            exact=("Entity.LegalName", "LegalEntity.LegalName", "LegalName"),
            suffixes=("EntityLegalName",),
        ),
        "jurisdiction": _select_header(
            fieldnames,
            exact=("Entity.LegalJurisdiction", "LegalJurisdiction"),
            suffixes=("EntityLegalJurisdiction",),
        ),
        "legal_country": _select_header(
            fieldnames,
            exact=("Entity.LegalAddress.Country", "LegalAddress.Country"),
            suffixes=("LegalAddressCountry",),
        ),
        "entity_status": _select_header(
            fieldnames,
            exact=("Entity.EntityStatus", "EntityStatus"),
            suffixes=("EntityEntityStatus",),
        ),
        "registration_status": _select_header(
            fieldnames,
            exact=("Registration.RegistrationStatus", "RegistrationStatus"),
            suffixes=("RegistrationRegistrationStatus",),
        ),
        "registration_authority_id": _select_header(
            fieldnames,
            exact=(
                "Entity.RegistrationAuthority.RegistrationAuthorityID",
                "RegistrationAuthorityID",
            ),
            suffixes=("RegistrationAuthorityRegistrationAuthorityID",),
        ),
        "registration_entity_id": _select_header(
            fieldnames,
            exact=(
                "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
                "Entity.RegistrationAuthority.RegistrationEntityID",
                "RegistrationAuthorityEntityID",
                "RegistrationEntityID",
            ),
            suffixes=(
                "RegistrationAuthorityRegistrationAuthorityEntityID",
                "RegistrationAuthorityRegistrationEntityID",
                "RegistrationAuthorityEntityID",
            ),
        ),
    }


def _open_csv_member(archive: zipfile.ZipFile, member: str) -> TextIO:
    binary = archive.open(member, "r")
    # utf-8-sig removes an optional byte-order mark while preserving ordinary UTF-8.
    return TextIOWrapperWithLargeFields(binary)


class TextIOWrapperWithLargeFields:
    """Context-manager wrapper around a ZIP member with a raised CSV field limit."""

    def __init__(self, binary: Any) -> None:
        import io

        csv.field_size_limit(max(csv.field_size_limit(), 10 * 1024 * 1024))
        self._binary = binary
        self._text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")

    def __enter__(self) -> TextIO:
        return self._text

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._text.close()


def normalize_lei2_zip(
    *,
    input_path: Path,
    output_csv: Path,
    active_only: bool = False,
) -> dict[str, Any]:
    """Normalize a GLEIF LEI Common Data File Golden Copy ZIP to source-local entities."""

    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if not zipfile.is_zipfile(input_path):
        raise ValueError(f"Input is not a ZIP archive: {input_path}")

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    skipped_inactive = 0
    source_members: list[str] = []
    observed_headers: list[str] = []
    with (
        zipfile.ZipFile(input_path) as archive,
        output_csv.open("w", encoding="utf-8", newline="") as output_handle,
    ):
        writer = csv.DictWriter(output_handle, fieldnames=CANONICAL_FIELDS)
        writer.writeheader()
        members = sorted(
            member
            for member in archive.namelist()
            if not member.endswith("/") and member.lower().endswith(".csv")
        )
        if not members:
            raise ValueError("GLEIF ZIP did not contain a CSV member")
        for member in members:
            source_members.append(member)
            with _open_csv_member(archive, member) as text_handle:
                reader = csv.DictReader(text_handle)
                fieldnames = list(reader.fieldnames or [])
                if not fieldnames:
                    continue
                if not observed_headers:
                    observed_headers = fieldnames
                mapping = _map_headers(fieldnames)
                if not mapping["lei"] or not mapping["legal_name"]:
                    raise ValueError(
                        "GLEIF CSV did not expose recognizable LEI and legal-name columns; "
                        f"observed columns: {fieldnames[:30]}"
                    )
                for raw in reader:
                    lei = (raw.get(mapping["lei"] or "") or "").strip().upper()
                    legal_name = (raw.get(mapping["legal_name"] or "") or "").strip()
                    if not lei or not legal_name:
                        continue
                    entity_status = (raw.get(mapping["entity_status"] or "") or "").strip()
                    registration_status = (
                        raw.get(mapping["registration_status"] or "") or ""
                    ).strip()
                    if active_only and (
                        entity_status.upper() not in {"ACTIVE", ""}
                        or registration_status.upper() not in {"ISSUED", "PENDING_TRANSFER", ""}
                    ):
                        skipped_inactive += 1
                        continue
                    jurisdiction = (
                        (
                            raw.get(mapping["jurisdiction"] or "")
                            or raw.get(mapping["legal_country"] or "")
                            or ""
                        )
                        .strip()
                        .upper()
                    )
                    registration_authority_id = (
                        raw.get(mapping["registration_authority_id"] or "") or ""
                    ).strip()
                    registration_entity_id = (
                        raw.get(mapping["registration_entity_id"] or "") or ""
                    ).strip()
                    writer.writerow(
                        {
                            "entity_id": f"gleif-{lei}",
                            "source_id": "gleif_golden_copy",
                            "source_entity_id": lei,
                            "legal_name": legal_name,
                            "jurisdiction": jurisdiction,
                            "exchange": "",
                            "ticker": "",
                            "lei": lei,
                            "isin": "",
                            "local_registry_id": registration_entity_id,
                            "entity_status": entity_status,
                            "registration_status": registration_status,
                            "registration_authority_id": registration_authority_id,
                            "source_member": member,
                        }
                    )
                    rows += 1

    digest = hashlib.sha256()
    with output_csv.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "source_id": "gleif_golden_copy",
        "input_path": str(input_path),
        "output_csv": str(output_csv),
        "rows": rows,
        "active_only": active_only,
        "skipped_inactive": skipped_inactive,
        "source_members": source_members,
        "observed_header_count": len(observed_headers),
        "output_sha256": digest.hexdigest(),
    }
