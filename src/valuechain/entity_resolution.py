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

ORG_SUFFIX_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,8}\s+"
    r"(?:Inc\.?|Incorporated|Corporation|Corp\.?|Company|Co\.?|Ltd\.?|Limited|plc|PLC|"
    r"N\.V\.|S\.A\.|LLC|Holdings)(?:,\s*Ltd\.?)?)\b"
)

LIST_INTRO_RE = re.compile(
    r"\b(?:such as|including|include|includes|including but not limited to)\s+([^;\n]{1,260})",
    flags=re.IGNORECASE,
)

COUNTERPARTY_LIST_MARKERS = ("such as", "including", "include", "includes")

LIST_ITEM_STOPWORDS = {
    "competition",
    "table of contents",
    "we",
    "our",
    "and",
    "or",
    "the",
    "a",
    "an",
    "inc",
    "corp",
    "co",
    "ltd",
    "llc",
    "plc",
    "customers",
    "suppliers",
    "subcontractors",
    "manufacturers",
    "providers",
}


@dataclass
class EntityResolver:
    companies: list[Company]

    def __post_init__(self) -> None:
        self.alias_to_company: dict[str, Company] = {}
        self.uppercase_only_aliases: set[str] = set()
        for company in self.companies:
            ticker_alias = company.ticker.lower()
            aliases = {
                ticker_alias,
                company.company_name.lower(),
                normalize_company_suffix(company.company_name).lower(),
            }
            if ticker_alias:
                self.uppercase_only_aliases.add(ticker_alias)
            for alias, target_name in COMMON_ALIASES.items():
                if target_name.lower() == company.company_name.lower():
                    aliases.add(alias)
                    self.uppercase_only_aliases.discard(alias)
            for alias in aliases:
                if alias:
                    self.alias_to_company[alias] = company

    def extract_mentions(self, text: str) -> list[EntityMention]:
        lowered = text.lower()
        mentions: list[EntityMention] = []
        for alias, company in sorted(self.alias_to_company.items(), key=lambda item: len(item[0]), reverse=True):
            if alias in self.uppercase_only_aliases and len(alias) <= 5:
                pattern = rf"\b{re.escape(alias.upper())}\b"
                haystack = text
            elif len(alias) < 3:
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
        mentions.extend(extract_named_organization_mentions(text))
        return dedupe_mentions(mentions)

    def resolve_object(self, object_hint: str, text: str, subject_name: str = "") -> EntityMention:
        return self.resolve_objects(object_hint, text, subject_name=subject_name, max_objects=1)[0]

    def resolve_objects(
        self,
        object_hint: str,
        text: str,
        subject_name: str = "",
        max_objects: int = 5,
    ) -> list[EntityMention]:
        mentions = self.extract_mentions(text)
        subject_key = subject_name.strip().lower()
        subject_normalized = normalize_entity_key(subject_name)
        resolved: list[EntityMention] = []
        for mention in mentions:
            if subject_key and mention.normalized_name.lower() == subject_key:
                continue
            if subject_normalized and normalize_entity_key(mention.normalized_name) == subject_normalized:
                continue
            if is_leading_sentence_subject(mention, text):
                continue
            resolved.append(mention)
        if resolved:
            if has_counterparty_list_marker(text):
                return resolved[:max_objects]
            return resolved[:1]
        normalized = object_hint.strip() or "unnamed counterparty"
        return [
            EntityMention(
                text=normalized,
                entity_type="dependency_class",
                normalized_name=normalized,
                confidence=0.45,
            )
        ]


def normalize_company_suffix(name: str) -> str:
    normalized = re.sub(
        r"\b(incorporated|inc\.?|corporation|corp\.?|limited|ltd\.?|plc|n\.v\.|s\.a\.|company|co\.?)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"[,.\s]+", " ", normalized)
    return normalized.strip()


def normalize_entity_key(name: str) -> str:
    normalized = normalize_company_suffix(name).lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def extract_named_organization_mentions(text: str) -> list[EntityMention]:
    mentions: list[EntityMention] = []
    for match in ORG_SUFFIX_RE.finditer(text):
        name = clean_organization_name(match.group(1))
        if not looks_like_organization_name(name):
            continue
        mentions.append(
            EntityMention(
                text=name,
                entity_type="organization",
                normalized_name=name,
                confidence=0.68,
            )
        )
    for match in LIST_INTRO_RE.finditer(text):
        for item in split_counterparty_list(match.group(1)):
            if not looks_like_organization_name(item):
                continue
            mentions.append(
                EntityMention(
                    text=item,
                    entity_type="organization",
                    normalized_name=item,
                    confidence=0.58,
                )
            )
    return dedupe_mentions(mentions)


def split_counterparty_list(segment: str) -> list[str]:
    segment = re.split(r"\bto\s+(?:perform|provide|supply|deliver|support)\b", segment, maxsplit=1)[0]
    segment = re.split(r"\bfor\s+(?:assembly|manufacturing|testing|packaging|services)\b", segment, maxsplit=1)[0]
    segment = segment.replace(" and ", ", ")
    return [clean_organization_name(part) for part in segment.split(",") if clean_organization_name(part)]


def clean_organization_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name.strip(" \t\n\r,;:.()"))
    cleaned = re.sub(r"\b(?:among others|etc)$", "", cleaned, flags=re.IGNORECASE).strip(" ,;:.")
    return cleaned


def looks_like_organization_name(name: str) -> bool:
    if not name or len(name) < 3 or len(name) > 120:
        return False
    key = name.lower()
    if key in LIST_ITEM_STOPWORDS:
        return False
    if any(word in key for word in ["table of contents", "item ", "part i", "part ii"]):
        return False
    words = name.split()
    if len(words) > 9:
        return False
    has_suffix = ORG_SUFFIX_RE.fullmatch(name) is not None
    titleish = sum(1 for word in words if word[:1].isupper() or word.isupper()) >= max(1, len(words) - 1)
    return has_suffix or titleish


def has_counterparty_list_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in COUNTERPARTY_LIST_MARKERS)


def is_leading_sentence_subject(mention: EntityMention, text: str) -> bool:
    prefix = text[:120].lower().lstrip()
    names = {mention.text.lower(), mention.normalized_name.lower()}
    return any(prefix.startswith(name) for name in names if name)


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
