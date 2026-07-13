from __future__ import annotations

import io
import shutil
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import EntityRef, SmokeResult, SmokeStatus

from gcu_priority_markets.catalog import load_contracts

from gcu_priority_markets.models import DisclosureEvent, FirdsFileRef, FirdsListing


TARGET_MICS: dict[str, set[str]] = {
    "GB": {"XLON", "AIMX", "AQSL", "AQSG"},
    "DE": {"XETR", "XETA", "XETB", "XETS", "XFRA", "FRAA", "FRAB", "FRAS"},
    "FR": {"XPAR", "ALXP", "XMLI"},
    "IT": {"XMIL", "EXGM"},
    "ES": {"XMAD", "GROW", "MABX", "XMCE", "XBIL", "XBAR", "XVAL"},
    "NL": {"XAMS"},
}
MIC_TO_JURISDICTION = {
    mic: jurisdiction for jurisdiction, mics in TARGET_MICS.items() for mic in mics
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(element: ET.Element, names: Iterable[str]) -> str | None:
    wanted = set(names)
    for node in element.iter():
        if _local_name(node.tag) in wanted and node.text and node.text.strip():
            return node.text.strip()
    return None


def _scoped_text(element: ET.Element, scope_names: Iterable[str], value_names: Iterable[str]) -> str | None:
    scopes = set(scope_names)
    for scope in element.iter():
        if _local_name(scope.tag) in scopes:
            value = _first_text(scope, value_names)
            if value:
                return value
    return None


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


class FcaFirdsAdapter(BaseAdapter):
    """FCA FIRDS file-index and streaming ISO 20022 reference-data parser."""

    INDEX_URL = "https://api.data.fca.org.uk/fca_data_firds_files"

    def __init__(self, *, contract: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contract = contract or load_contracts().get(self.source_id, {})
        configured = self.contract.get("target_mics") or TARGET_MICS
        self.target_mics = {
            str(jurisdiction).upper(): {str(mic).upper() for mic in mics}
            for jurisdiction, mics in configured.items()
        }
        self.mic_to_jurisdiction = {
            mic: jurisdiction
            for jurisdiction, mics in self.target_mics.items()
            for mic in mics
        }

    @staticmethod
    def parse_file_index(payload: dict[str, Any]) -> list[FirdsFileRef]:
        hits = payload.get("hits", {}).get("hits", [])
        output: list[FirdsFileRef] = []
        for hit in hits:
            source = hit.get("_source", hit)
            try:
                output.append(
                    FirdsFileRef(
                        file_type=str(source["file_type"]),
                        file_name=str(source["file_name"]),
                        publication_date=date.fromisoformat(str(source["publication_date"])[:10]),
                        download_link=str(source["download_link"]),
                        last_refreshed=_parse_iso_datetime(source.get("last_refreshed")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return output

    def list_files(
        self,
        *,
        begin: date,
        end: date,
        file_type: str = "FULINS",
        size: int = 1000,
    ) -> list[FirdsFileRef]:
        query = (
            f"((file_type:{file_type}) AND "
            f"(publication_date:[{begin.isoformat()} TO {end.isoformat()}]))"
        )
        payload = self.client.get_json(
            self.INDEX_URL,
            params={
                "q": query,
                "from": 0,
                "size": min(max(size, 1), 5000),
                "pretty": "true",
                "sort": "publication_date:desc",
            },
        )
        return self.parse_file_index(payload)

    def latest_equity_full_files(
        self,
        *,
        lookback_days: int = 21,
        as_of: date | None = None,
    ) -> list[FirdsFileRef]:
        end = as_of or date.today()
        files = self.list_files(
            begin=end - timedelta(days=lookback_days),
            end=end,
            file_type="FULINS",
        )
        equity_files = [item for item in files if item.file_name.upper().startswith("FULINS_E_")]
        if not equity_files:
            return []
        latest = max(item.publication_date for item in equity_files)
        return sorted(
            [item for item in equity_files if item.publication_date == latest],
            key=lambda item: item.file_name,
        )

    def download_files(self, files: Iterable[FirdsFileRef], output_dir: Path) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for item in files:
            destination = output_dir / item.file_name
            if destination.exists() and destination.stat().st_size > 0:
                paths.append(destination)
                continue
            with self.client.stream_to_temporary_file(item.download_link) as payload:
                shutil.copyfile(payload.temporary_path, destination)
            paths.append(destination)
        return paths

    @classmethod
    def _parse_financial_instrument(
        cls,
        element: ET.Element,
        *,
        source_file: str | None,
    ) -> FirdsListing | None:
        record = element
        action = "full"
        action_names = {
            "NewRcrd": "new",
            "ModfdRcrd": "modified",
            "TermntdRcrd": "terminated",
            "CancRcrd": "cancelled",
        }
        for child in element.iter():
            name = _local_name(child.tag)
            if name in action_names:
                action = action_names[name]
                record = child
                break
        isin = _scoped_text(
            record,
            ("FinInstrmGnlAttrbts", "FinInstrmGnlAttrbts2"),
            ("Id", "ISIN"),
        ) or _first_text(record, ("ISIN",))
        cfi = _scoped_text(
            record,
            ("FinInstrmGnlAttrbts", "FinInstrmGnlAttrbts2"),
            ("ClssfctnTp", "CFI"),
        )
        mic = _scoped_text(
            record,
            ("TradgVnRltdAttrbts", "TradgVnRltdAttrbts2", "TradgVnAttrbts"),
            ("Id", "TradgVn", "MktIdrCd", "MIC"),
        ) or _first_text(record, ("TradgVn", "MktIdrCd", "MIC"))
        if not isin or not mic:
            return None
        if cfi and not cfi.upper().startswith("E"):
            return None
        issuer_lei = _first_text(record, ("Issr", "IssrLEI", "LEI"))
        full_name = _scoped_text(
            record,
            ("FinInstrmGnlAttrbts", "FinInstrmGnlAttrbts2"),
            ("FullNm", "Nm"),
        )
        short_name = _scoped_text(
            record,
            ("FinInstrmGnlAttrbts", "FinInstrmGnlAttrbts2"),
            ("ShrtNm",),
        )
        currency = _scoped_text(
            record,
            ("FinInstrmGnlAttrbts", "FinInstrmGnlAttrbts2"),
            ("NtnlCcy", "Ccy"),
        )
        first_trade_date = _parse_iso_date(
            _first_text(record, ("FrstTradDt", "FrstTradgDt", "AdmssnApprvlDtByIssr"))
        )
        termination_date = _parse_iso_date(
            _first_text(record, ("TermntnDt", "TermntdDt", "LastTradDt"))
        )
        return FirdsListing(
            action=action,
            isin=isin,
            mic=mic.upper(),
            issuer_lei=issuer_lei,
            full_name=full_name,
            short_name=short_name,
            cfi=cfi,
            currency=currency,
            first_trade_date=first_trade_date,
            termination_date=termination_date,
            source_file=source_file,
        )

    @classmethod
    def parse_xml_stream(
        cls,
        stream: BinaryIO,
        *,
        source_file: str | None = None,
    ) -> Iterator[FirdsListing]:
        for _event, element in ET.iterparse(stream, events=("end",)):
            if _local_name(element.tag) not in {"FinInstrm", "FinInstrmRcrd"}:
                continue
            listing = cls._parse_financial_instrument(element, source_file=source_file)
            if listing is not None:
                yield listing
            element.clear()

    @classmethod
    def parse_path(cls, path: Path) -> Iterator[FirdsListing]:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
                if not names:
                    raise ValueError(f"FIRDS ZIP contains no XML file: {path}")
                for name in names:
                    with archive.open(name) as stream:
                        yield from cls.parse_xml_stream(
                            stream,
                            source_file=f"{path.name}:{name}",
                        )
        else:
            with path.open("rb") as stream:
                yield from cls.parse_xml_stream(stream, source_file=path.name)

    @classmethod
    def listing_to_entity(
        cls,
        listing: FirdsListing,
        *,
        mic_to_jurisdiction: dict[str, str] | None = None,
        as_of: date | None = None,
        include_terminated: bool = False,
    ) -> EntityRef | None:
        jurisdiction = (mic_to_jurisdiction or MIC_TO_JURISDICTION).get(listing.mic)
        if not jurisdiction:
            return None
        reference_date = as_of or date.today()
        if not include_terminated and listing.termination_date and listing.termination_date < reference_date:
            return None
        name = listing.short_name or listing.full_name or listing.isin
        entity_id = f"lei-{listing.issuer_lei}" if listing.issuer_lei else f"firds-{listing.isin}"
        source_entity_id = f"{listing.issuer_lei or 'NOLEI'}:{listing.isin}:{listing.mic}"
        return EntityRef(
            entity_id=entity_id,
            source_id="fca_firds_priority",
            source_entity_id=source_entity_id,
            legal_name=name,
            jurisdiction=jurisdiction,
            exchange=listing.mic,
            ticker=None,
            lei=listing.issuer_lei,
            isin=listing.isin,
            metadata=listing.model_dump(mode="json"),
        )

    def list_entities(
        self,
        *,
        paths: Iterable[Path] | None = None,
        jurisdictions: Iterable[str] | None = None,
        auto_download: bool = False,
        cache_dir: Path = Path("data/snapshots/firds"),
        as_of: date | None = None,
        **_: Any,
    ) -> Iterable[EntityRef]:
        selected_paths = list(paths or [])
        if not selected_paths and auto_download:
            refs = self.latest_equity_full_files(as_of=as_of)
            selected_paths = self.download_files(refs, cache_dir)
        if not selected_paths:
            raise ValueError("FIRDS list_entities requires paths or auto_download=True")
        allowed = {item.upper() for item in jurisdictions} if jurisdictions else None
        seen: set[tuple[str, str]] = set()
        for path in selected_paths:
            for listing in self.parse_path(path):
                entity = self.listing_to_entity(
                    listing,
                    mic_to_jurisdiction=self.mic_to_jurisdiction,
                    as_of=as_of,
                )
                if entity is None:
                    continue
                if allowed and entity.jurisdiction not in allowed:
                    continue
                key = (entity.isin or "", entity.exchange or "")
                if key in seen:
                    continue
                seen.add(key)
                entity.source_id = self.source_id
                yield entity

    def list_delta_events(
        self,
        *,
        paths: Iterable[Path],
        jurisdictions: Iterable[str] | None = None,
    ) -> Iterable[DisclosureEvent]:
        allowed = {item.upper() for item in jurisdictions} if jurisdictions else None
        for path in paths:
            for listing in self.parse_path(path):
                jurisdiction = self.mic_to_jurisdiction.get(listing.mic)
                if not jurisdiction or (allowed and jurisdiction not in allowed):
                    continue
                action = listing.action if listing.action != "full" else "observed"
                source_fragment = listing.source_file or path.name
                filing_id = f"{action}:{listing.isin}:{listing.mic}:{source_fragment}"
                yield DisclosureEvent(
                    event_id=f"{self.source_id}:{filing_id}",
                    source_id=self.source_id,
                    jurisdiction=jurisdiction,
                    channel="firds_delta_or_cancellation",
                    issuer_id=listing.issuer_lei or listing.isin,
                    issuer_name=listing.short_name or listing.full_name,
                    security_code=listing.isin,
                    filing_id=filing_id,
                    form=f"instrument_{action}",
                    title=f"FIRDS {action}: {listing.short_name or listing.full_name or listing.isin}",
                    filed_at=listing.termination_date or listing.first_trade_date,
                    metadata=listing.model_dump(mode="json"),
                )

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        if offline:
            xml = b"""<?xml version='1.0' encoding='UTF-8'?>
            <FinInstrmRptgRefDataRpt xmlns='urn:iso:std:iso:20022:tech:xsd:auth.017.001.02'>
              <RefData><FinInstrm><NewRcrd>
                <FinInstrmGnlAttrbts>
                  <Id>GB00TEST0001</Id><FullNm>Example PLC Ordinary Shares</FullNm>
                  <ShrtNm>EXAMPLE PLC</ShrtNm><ClssfctnTp>ESVUFR</ClssfctnTp><NtnlCcy>GBP</NtnlCcy>
                </FinInstrmGnlAttrbts>
                <Issr>549300TESTLEI000001</Issr>
                <TradgVnRltdAttrbts><Id>XLON</Id><FrstTradDt>2025-01-02</FrstTradDt></TradgVnRltdAttrbts>
              </NewRcrd></FinInstrm></RefData>
            </FinInstrmRptgRefDataRpt>"""
            listings = list(self.parse_xml_stream(io.BytesIO(xml), source_file="fixture.xml"))
            entities = [self.listing_to_entity(item) for item in listings]
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="file_index_and_equity_reference_parser",
                endpoint=self.INDEX_URL,
                records_observed=len([item for item in entities if item is not None]),
                message="FCA FIRDS file-index and streaming ISO 20022 equity parser contracts validated offline.",
            )
        try:
            end = date.today()
            files = self.list_files(begin=end - timedelta(days=14), end=end, size=5)
            if not files:
                raise ValueError("FCA FIRDS file index returned zero records")
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="file_index",
                endpoint=self.INDEX_URL,
                records_observed=len(files),
                message="FCA FIRDS machine file index returned parseable metadata.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("file_index", self.INDEX_URL, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="file_index",
                endpoint=self.INDEX_URL,
                message=f"FCA FIRDS smoke check failed: {type(exc).__name__}: {exc}",
            )
