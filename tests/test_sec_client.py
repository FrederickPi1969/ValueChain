from valuechain.models import Company
from valuechain.sec_client import SECClient


class FakeSECClient(SECClient):
    def __init__(self) -> None:
        pass

    def submissions(self, cik: str):
        return {
            "filings": {
                "recent": {
                    "accessionNumber": ["0001045810-25-000023", "0001045810-25-000010"],
                    "form": ["10-K", "8-K"],
                    "filingDate": ["2025-02-26", "2025-01-01"],
                    "reportDate": ["2025-01-26", ""],
                    "acceptanceDateTime": ["2025-02-26T16:30:00.000Z", ""],
                    "primaryDocument": ["nvda-20250126.htm", "nvda-8k.htm"],
                }
            }
        }


def test_discover_filings_builds_archive_urls_and_filters_forms() -> None:
    client = FakeSECClient()
    company = Company("NVDA", "NVIDIA Corporation", cik="0001045810")
    filings = client.discover_filings(company, forms={"10-K"}, max_filings=5)
    assert len(filings) == 1
    filing = filings[0]
    assert filing.accession_number == "0001045810-25-000023"
    assert filing.primary_document_url.endswith("/000104581025000023/nvda-20250126.htm")

