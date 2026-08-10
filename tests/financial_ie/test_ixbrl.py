from pathlib import Path

from valuechain.financial_ie.ixbrl import extract_financial_facts


def test_extract_financial_facts_prefers_consolidated_annual_context(tmp_path: Path) -> None:
    filing = tmp_path / "filing.htm"
    filing.write_text(
        """<html><body>
        <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
        <xbrli:context id="annual"><xbrli:entity/><xbrli:period>
          <xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate>
        </xbrli:period></xbrli:context>
        <xbrli:context id="segment"><xbrli:entity><xbrli:segment>Cloud</xbrli:segment></xbrli:entity>
          <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
        </xbrli:context>
        <ix:nonfraction id="r1" name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
          contextref="segment" unitref="USD" scale="6">20</ix:nonfraction>
        <ix:nonfraction id="r2" name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
          contextref="annual" unitref="USD" scale="6">100</ix:nonfraction>
        </body></html>""",
        encoding="utf-8",
    )

    facts = extract_financial_facts(filing, report_date="2025-12-31")

    assert facts == [
        {
            "field": "revenue",
            "value": "100000000",
            "unit": "USD",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "period_type": "duration",
            "source_concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "context_ref": "annual",
            "fact_id": "r2",
            "extraction_method": "inline_xbrl",
            "extractor_version": "inline-xbrl-v0.4",
            "confidence": 1.0,
        }
    ]


def test_extract_financial_facts_does_not_fallback_to_segment_fact(tmp_path: Path) -> None:
    filing = tmp_path / "filing.htm"
    filing.write_text(
        """<html><body>
        <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
        <xbrli:context id="segment"><xbrli:entity><xbrli:segment>
          <xbrldi:explicitMember dimension="Axis">Member</xbrldi:explicitMember>
        </xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
        <ix:nonfraction name="us-gaap:Liabilities" contextref="segment" unitref="USD" scale="6">21</ix:nonfraction>
        </body></html>""",
        encoding="utf-8",
    )
    assert extract_financial_facts(filing, report_date="2025-12-31") == []


def test_extract_financial_facts_supports_ifrs_revenue(tmp_path: Path) -> None:
    filing = tmp_path / "filing.htm"
    filing.write_text(
        """<html><body>
        <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
        <xbrli:context id="annual"><xbrli:entity/><xbrli:period>
          <xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate>
        </xbrli:period></xbrli:context>
        <ix:nonfraction name="ifrs-full:Revenue" contextref="annual" unitref="USD" scale="6">100</ix:nonfraction>
        </body></html>""",
        encoding="utf-8",
    )
    facts = extract_financial_facts(filing, report_date="2025-12-31")
    assert facts[0]["field"] == "revenue"
    assert facts[0]["value"] == "100000000"
