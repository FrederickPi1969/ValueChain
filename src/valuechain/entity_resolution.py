from __future__ import annotations

import re
from dataclasses import dataclass

from valuechain.models import Company, EntityMention


COMMON_ALIASES: dict[str, str] = {
    "amazon web services": "Amazon.com Inc.",
    "aws": "Amazon.com Inc.",
    "microsoft azure": "Microsoft Corporation",
    "azure": "Microsoft Corporation",
    "google cloud": "Alphabet Inc.",
    "gcp": "Alphabet Inc.",
    "tsmc": "Taiwan Semiconductor Manufacturing Company Limited",
    "taiwan semiconductor": "Taiwan Semiconductor Manufacturing Company Limited",
    "nvidia": "NVIDIA Corporation",
    "advanced micro devices": "Advanced Micro Devices Inc.",
    "amd": "Advanced Micro Devices Inc.",
    "asml": "ASML Holding N.V.",
    "arm": "Arm Holdings plc",
    "broadcom": "Broadcom Inc.",
}


@dataclass
class EntityResolver:
    companies: list[Company]

    def __post_init__(self) -> None:
        self.alias_to_company: dict[str, Company] = {}
        for company in self.companies:
            aliases = {
                company.ticker.lower(),
                company.company_name.lower(),
                normalize_company_suffix(company.company_name).lower(),
            }
            for alias, target_name in COMMON_ALIASES.items():
                if target_name.lower() == company.company_name.lower():
                    aliases.add(alias)
            for alias in aliases:
                if alias:
                    self.alias_to_company[alias] = company

    def extract_mentions(self, text: str) -> list[EntityMention]:
        lowered = text.lower()
        mentions: list[EntityMention] = []
        for alias, company in sorted(self.alias_to_company.items(), key=lambda item: len(item[0]), reverse=True):
            if len(alias) < 3:
                pattern = rf"\b{re.escape(alias.upper())}\b"
                haystack = text
            else:
                pattern = rf"\b{re.escape(alias)}\b"
                haystack = lowered
            if re.search(pattern, haystack, flags=0):
                mentions.append(
                    EntityMention(
                        text=alias,
                        entity_type="company",
                        normalized_name=company.company_name,
                        ticker=company.ticker,
                        cik=company.cik,
                        confidence=0.85,
                    )
                )
        return dedupe_mentions(mentions)

    def resolve_object(self, object_hint: str, text: str, subject_name: str = "") -> EntityMention:
        mentions = self.extract_mentions(text)
        subject_key = subject_name.strip().lower()
        subject_mentions = [
            mention for mention in mentions if mention.normalized_name.lower() in text[:150].lower()
        ]
        for mention in mentions:
            if subject_key and mention.normalized_name.lower() == subject_key:
                continue
            if mention not in subject_mentions:
                return mention
        normalized = object_hint.strip() or "unnamed counterparty"
        return EntityMention(
            text=normalized,
            entity_type="dependency_class",
            normalized_name=normalized,
            confidence=0.45,
        )


def normalize_company_suffix(name: str) -> str:
    normalized = re.sub(
        r"\b(incorporated|inc\.?|corporation|corp\.?|limited|ltd\.?|plc|n\.v\.|s\.a\.|company|co\.?)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"[,.\s]+", " ", normalized)
    return normalized.strip()


def dedupe_mentions(mentions: list[EntityMention]) -> list[EntityMention]:
    seen: set[tuple[str, str]] = set()
    deduped: list[EntityMention] = []
    for mention in mentions:
        key = (mention.normalized_name.lower(), mention.entity_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(mention)
    return deduped
