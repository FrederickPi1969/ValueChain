from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from gcu.adapters.filings_xbrl import FilingsXbrlAdapter
from gcu.models import EntityRef, FilingRef


class PriorityEsefAdapter(FilingsXbrlAdapter):
    """Thin priority-country monitor over the deployed filings.xbrl.org adapter.

    The upstream index is a discovery channel. National Officially Appointed
    Mechanisms remain the authority for completeness and legal provenance.
    """

    PRIORITY_COUNTRIES = ("DE", "FR", "IT", "ES", "NL")

    def list_recent_filings(
        self,
        *,
        begin: date,
        end: date,
        jurisdictions: Iterable[str] | None = None,
        page_size: int = 100,
        max_pages: int | None = 20,
        **_: Any,
    ) -> Iterable[FilingRef]:
        countries = tuple(
            dict.fromkeys(item.upper() for item in (jurisdictions or self.PRIORITY_COUNTRIES))
        )
        for country in countries:
            for filing in super().list_filings(
                jurisdiction=country,
                page_size=page_size,
                max_pages=max_pages,
                sort="-processed",
            ):
                observed = filing.filed_at
                if observed is None:
                    continue
                if observed > end:
                    continue
                if observed < begin:
                    # Results are sorted newest-first within this country.
                    break
                filing.source_id = self.source_id
                filing.metadata["discovery_country"] = country
                filing.metadata["authority_warning"] = (
                    "filings.xbrl.org is a secondary discovery index; reconcile against the "
                    "national Officially Appointed Mechanism before claiming completeness."
                )
                yield filing

    def list_filings(
        self,
        entity: EntityRef | None = None,
        *,
        begin: date | None = None,
        end: date | None = None,
        jurisdictions: Iterable[str] | None = None,
        **kwargs: Any,
    ) -> Iterable[FilingRef]:
        if begin is not None and end is not None:
            yield from self.list_recent_filings(
                begin=begin,
                end=end,
                jurisdictions=jurisdictions,
                **kwargs,
            )
            return
        countries = tuple(jurisdictions or self.PRIORITY_COUNTRIES)
        for country in countries:
            yield from super().list_filings(entity=entity, jurisdiction=country, **kwargs)
