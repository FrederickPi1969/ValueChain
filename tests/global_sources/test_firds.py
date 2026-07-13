from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

from gcu_priority_markets.adapters.firds import FcaFirdsAdapter


XML = b"""<?xml version='1.0' encoding='UTF-8'?>
<FinInstrmRptgRefDataRpt xmlns='urn:iso:std:iso:20022:tech:xsd:auth.017.001.02'>
  <RefData>
    <FinInstrm><NewRcrd>
      <FinInstrmGnlAttrbts><Id>GB00TEST0001</Id><FullNm>Example PLC Ordinary Shares</FullNm><ShrtNm>EXAMPLE PLC</ShrtNm><ClssfctnTp>ESVUFR</ClssfctnTp><NtnlCcy>GBP</NtnlCcy></FinInstrmGnlAttrbts>
      <Issr>549300TESTLEI000001</Issr><TradgVnRltdAttrbts><Id>XLON</Id><FrstTradDt>2025-01-02</FrstTradDt></TradgVnRltdAttrbts>
    </NewRcrd></FinInstrm>
    <FinInstrm><NewRcrd>
      <FinInstrmGnlAttrbts><Id>US00NOTTARGET1</Id><FullNm>Not Target</FullNm><ClssfctnTp>ESVUFR</ClssfctnTp></FinInstrmGnlAttrbts>
      <TradgVnRltdAttrbts><Id>XNAS</Id></TradgVnRltdAttrbts>
    </NewRcrd></FinInstrm>
  </RefData>
</FinInstrmRptgRefDataRpt>"""


def test_firds_index_parser() -> None:
    payload = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "file_type": "FULINS",
                        "file_name": "FULINS_E_20260710_01of01.zip",
                        "publication_date": "2026-07-10",
                        "download_link": "https://example.invalid/file.zip",
                        "last_refreshed": "2026-07-10T06:00:00Z",
                    }
                }
            ]
        }
    }
    item = FcaFirdsAdapter.parse_file_index(payload)[0]
    assert item.file_type == "FULINS"
    assert item.publication_date == date(2026, 7, 10)


def test_firds_stream_parser_and_target_mapping() -> None:
    listings = list(FcaFirdsAdapter.parse_xml_stream(io.BytesIO(XML)))
    assert len(listings) == 2
    entity = FcaFirdsAdapter.listing_to_entity(listings[0])
    assert entity is not None
    assert entity.jurisdiction == "GB"
    assert entity.exchange == "XLON"
    assert FcaFirdsAdapter.listing_to_entity(listings[1]) is None


def test_firds_zip_parser(tmp_path: Path) -> None:
    path = tmp_path / "firds.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("payload.xml", XML)
    rows = list(FcaFirdsAdapter.parse_path(path))
    assert len(rows) == 2
    assert rows[0].source_file.endswith("payload.xml")


def test_firds_patch_contract_includes_growth_market_mics() -> None:
    from gcu.config import Settings
    from gcu.http import PoliteHttpClient
    from gcu_priority_markets.registry import PatchRegistry

    settings = Settings()
    client = PoliteHttpClient(settings)
    try:
        adapter = PatchRegistry().create_adapter("fca_firds_priority", settings, client)
        assert adapter.mic_to_jurisdiction["ALXP"] == "FR"
        assert adapter.mic_to_jurisdiction["EXGM"] == "IT"
        assert adapter.mic_to_jurisdiction["GROW"] == "ES"
        assert adapter.mic_to_jurisdiction["XETS"] == "DE"
    finally:
        client.close()


def test_firds_delta_event_generation(tmp_path: Path) -> None:
    from gcu.config import Settings
    from gcu.http import PoliteHttpClient
    from gcu_priority_markets.registry import PatchRegistry

    path = tmp_path / "delta.xml"
    path.write_bytes(XML)
    settings = Settings()
    client = PoliteHttpClient(settings)
    try:
        adapter = PatchRegistry().create_adapter("fca_firds_priority", settings, client)
        events = list(adapter.list_delta_events(paths=[path], jurisdictions=["GB"]))
    finally:
        client.close()
    assert len(events) == 1
    assert events[0].jurisdiction == "GB"
    assert events[0].form == "instrument_new"
