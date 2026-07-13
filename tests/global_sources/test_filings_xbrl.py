from gcu.adapters.filings_xbrl import FilingsXbrlAdapter


def test_live_shape_maps_included_entity_and_download_urls() -> None:
    payload = {
        "data": [
            {
                "type": "filing",
                "id": "25245",
                "attributes": {
                    "fxo_id": "LEI-2025-12-31-ESEF-SK-0",
                    "date_added": "2026-07-07 20:15:47",
                    "period_end": "2025-12-31",
                    "package_url": "/entity/report.zip",
                    "report_url": "/entity/report.html",
                    "json_url": "/entity/report.json",
                    "viewer_url": "/entity/viewer.html",
                },
                "relationships": {"entity": {"data": {"type": "entity", "id": "6709"}}},
            }
        ],
        "included": [
            {
                "type": "entity",
                "id": "6709",
                "attributes": {"identifier": "097900TESTLEI000001", "name": "Example SE"},
            }
        ],
    }

    filing = next(FilingsXbrlAdapter.parse_filings(payload))
    fake_adapter = type("Adapter", (), {"source_id": "priority_eu_esef"})()
    documents = list(FilingsXbrlAdapter.list_documents(fake_adapter, filing))

    assert filing.filing_id == "LEI-2025-12-31-ESEF-SK-0"
    assert filing.source_entity_id == "097900TESTLEI000001"
    assert filing.metadata["entity_name"] == "Example SE"
    assert filing.primary_document_url == "https://filings.xbrl.org/entity/report.zip"
    assert filing.period_end.isoformat() == "2025-12-31"
    assert {row.document_type for row in documents} == {
        "original XBRL Report Package",
        "primary Inline XBRL report",
        "xBRL-JSON facts",
    }
