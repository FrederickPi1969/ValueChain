from valuechain.models import Company
from valuechain.sec_client import SECClient, build_source_documents, classify_archive_document, parse_filing_detail_rows


class FakeSECClient(SECClient):
    def __init__(self) -> None:
        pass

    def submissions(self, cik: str):
        return {
            "filings": {
                "recent": {
                    "accessionNumber": [
                        "0001045810-25-000030",
                        "0001045810-25-000020",
                        "0001045810-25-000010",
                        "0001045810-25-000005",
                    ],
                    "form": ["8-K", "8-K", "10-Q", "10-K"],
                    "filingDate": ["2025-04-01", "2025-03-01", "2025-02-15", "2025-01-01"],
                    "reportDate": ["", "", "2025-01-31", "2024-12-31"],
                    "acceptanceDateTime": ["", "", "", "2025-01-01T16:30:00.000Z"],
                    "primaryDocument": ["nvda-8k-2.htm", "nvda-8k.htm", "nvda-10q.htm", "nvda-10k.htm"],
                }
            }
        }


def test_discover_filings_builds_archive_urls_and_filters_forms() -> None:
    client = FakeSECClient()
    company = Company("NVDA", "NVIDIA Corporation", cik="0001045810")
    filings = client.discover_filings(company, forms={"10-K"}, max_filings=5)
    assert len(filings) == 1
    filing = filings[0]
    assert filing.accession_number == "0001045810-25-000005"
    assert filing.primary_document_url.endswith("/000104581025000005/nvda-10k.htm")


def test_form_balanced_selection_keeps_backbone_forms_when_8k_is_more_recent() -> None:
    client = FakeSECClient()
    company = Company("NVDA", "NVIDIA Corporation", cik="0001045810")
    filings = client.discover_filings(
        company,
        forms={"10-K", "10-Q", "8-K"},
        max_filings=1,
        selection="form_balanced",
    )
    assert [filing.form for filing in filings] == ["10-K", "10-Q", "8-K"]


def test_latest_selection_preserves_total_latest_filing_cap() -> None:
    client = FakeSECClient()
    company = Company("NVDA", "NVIDIA Corporation", cik="0001045810")
    filings = client.discover_filings(
        company,
        forms={"10-K", "10-Q", "8-K"},
        max_filings=2,
        selection="latest",
    )
    assert [filing.form for filing in filings] == ["8-K", "8-K"]


def test_parse_filing_detail_rows_extracts_exhibit_metadata() -> None:
    html = """
    <table class="tableFile">
      <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
      <tr><td>1</td><td>8-K</td><td><a href="/Archives/a/cloud.htm">cloud.htm</a> iXBRL</td><td>8-K</td><td>10</td></tr>
      <tr><td>2</td><td>EX-99.1</td><td><a href="/Archives/a/q1exhibit991.htm">q1exhibit991.htm</a></td><td>EX-99.1</td><td>20</td></tr>
      <tr><td>3</td><td>XBRL</td><td><a href="/Archives/a/cloud.xsd">cloud.xsd</a></td><td>EX-101.SCH</td><td>30</td></tr>
    </table>
    """
    rows = parse_filing_detail_rows(html)
    assert rows[0]["document"] == "cloud.htm"
    assert rows[1]["document_type"] == "EX-99.1"


def test_build_source_documents_keeps_primary_and_selected_exhibits() -> None:
    company = Company("NET", "Cloudflare Inc.", cik="0001477333")
    filing = FakeSECClient().discover_filings(company, forms={"8-K"}, max_filings=1)[0]
    filing.primary_document = "cloud.htm"
    filing.primary_document_url = f"{filing.archive_url}cloud.htm"
    rows = [
        {"sequence": "1", "description": "8-K", "document": "cloud.htm", "document_type": "8-K"},
        {"sequence": "2", "description": "EX-99.1", "document": "q1exhibit991.htm", "document_type": "EX-99.1"},
        {"sequence": "3", "description": "EX-101", "document": "cloud.xsd", "document_type": "EX-101.SCH"},
    ]
    documents = build_source_documents(filing, rows, include_exhibits=True, exhibit_types=("EX-99.1",))
    assert [document.document for document in documents] == ["cloud.htm", "q1exhibit991.htm"]
    assert documents[0].is_primary is True
    assert documents[1].document_type == "EX-99.1"


def test_classify_archive_document_excludes_xbrl_and_graphics() -> None:
    assert classify_archive_document({"document": "cloud.xsd", "document_type": "EX-101.SCH"}) is None
    assert classify_archive_document({"document": "logo.jpg", "document_type": "GRAPHIC"}) is None
    assert classify_archive_document({"document": "agreement.htm", "document_type": "EX-10.1"}) == "EX-10"
    assert classify_archive_document({"document": "exhibit21.htm", "document_type": "EX-2.1"}) is None
