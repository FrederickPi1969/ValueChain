from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import DocumentRef, EntityRef, FilingRef, SmokeResult, SmokeStatus


class CvmBrazilAdapter(BaseAdapter):
    REGISTRY_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"
    DOC_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = self.REGISTRY_URL
        if offline:
            sample = "CNPJ_CIA;DENOM_SOCIAL;CD_CVM;SIT\n33.592.510/0001-54;VALE S.A.;4170;ATIVO\n"
            count = len(list(self.parse_registry_text(sample)))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="public_company_registry",
                endpoint=endpoint,
                records_observed=count,
                message="Offline CVM semicolon-CSV registry and annual/quarterly bulk archive contracts validated.",
            )
        try:
            text = self.client.get_text(endpoint)
            count = sum(1 for _ in self.parse_registry_text(text))
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="public_company_registry",
                endpoint=endpoint,
                records_observed=count,
                message="CVM open-data registry returned public-company records.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("public_company_registry", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="public_company_registry",
                endpoint=endpoint,
                message=f"CVM smoke check failed: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def parse_registry_text(text: str, *, active_only: bool = False) -> Iterable[EntityRef]:
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        for row in reader:
            status = (row.get("SIT") or row.get("SIT_REG") or "").strip().upper()
            if active_only and status not in {"ATIVO", "NORMAL"}:
                continue
            cvm_code = (row.get("CD_CVM") or "").strip()
            cnpj = (row.get("CNPJ_CIA") or "").strip()
            name = (row.get("DENOM_SOCIAL") or row.get("DENOM_COMERC") or "").strip()
            if not cvm_code or not name:
                continue
            yield EntityRef(
                entity_id=f"cvm-{cvm_code}",
                source_id="cvm_brazil",
                source_entity_id=cvm_code,
                legal_name=name,
                jurisdiction="BR",
                exchange="B3" if status in {"ATIVO", "NORMAL"} else None,
                local_registry_id=cnpj or cvm_code,
                aliases=[row.get("DENOM_COMERC", "").strip()]
                if row.get("DENOM_COMERC", "").strip()
                else [],
                metadata=row,
            )

    @classmethod
    def parse_registry_file(cls, path: Path, *, active_only: bool = False) -> Iterable[EntityRef]:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                text = raw.decode(encoding)
                yield from cls.parse_registry_text(text, active_only=active_only)
                return
            except UnicodeDecodeError:
                continue
        raise UnicodeDecodeError("unknown", raw, 0, 1, "Could not decode CVM registry")

    def list_entities(self, *, active_only: bool = False, **_: Any) -> Iterable[EntityRef]:
        text = self.client.get_text(self.REGISTRY_URL)
        for entity in self.parse_registry_text(text, active_only=active_only):
            entity.source_id = self.source_id
            yield entity

    def bulk_filings(
        self, *, start_year: int = 2010, end_year: int | None = None
    ) -> Iterable[FilingRef]:
        end_year = end_year or date.today().year
        for year in range(start_year, end_year + 1):
            for form, path in (
                ("DFP", "DFP/DADOS"),
                ("ITR", "ITR/DADOS"),
                ("FRE", "FRE/DADOS"),
                ("IPE", "IPE/DADOS"),
            ):
                filename = f"{form.lower()}_cia_aberta_{year}.zip"
                url = f"{self.DOC_BASE}/{path}/{filename}"
                yield FilingRef(
                    source_id=self.source_id,
                    filing_id=f"{form}-{year}",
                    entity_id="cvm-bulk",
                    form=form,
                    title=f"CVM {form} bulk data {year}",
                    filed_at=date(year, 12, 31),
                    primary_document_url=url,
                    language="pt",
                    metadata={"bulk": True, "year": year, "form": form},
                )

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        if not filing.primary_document_url:
            return
        filename = filing.primary_document_url.rsplit("/", 1)[-1]
        yield DocumentRef(
            source_id=self.source_id,
            document_id=f"{filing.filing_id}:bulk",
            filing_id=filing.filing_id,
            entity_id=filing.entity_id,
            url=filing.primary_document_url,
            filename=filename,
            document_type=f"CVM {filing.form} bulk archive",
            expected_media_type="application/zip",
            filed_at=filing.filed_at,
            metadata=filing.metadata,
        )
