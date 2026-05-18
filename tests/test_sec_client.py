from valuechain.models import Company
from valuechain.sec_client import SECClient


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
