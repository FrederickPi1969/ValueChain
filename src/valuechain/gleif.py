from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests

from valuechain.io_utils import read_jsonl, write_csv, write_json, write_jsonl
from valuechain.rate_limit import RateLimiter


GLEIF_API_BASE = "https://api.gleif.org/api/v1"
DEFAULT_USER_AGENT = "FrederickPi ValueChainPrototype/0.1 GLEIF entity resolver"
NON_ENTITY_OBJECT_TYPES = {
    "dependency_class",
    "geography",
    "facility",
    "anonymous_counterparty",
}
LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "llc",
    "plc",
    "nv",
    "n v",
    "sa",
    "s a",
    "ag",
    "gmbh",
    "bv",
    "b v",
    "pte",
    "pte ltd",
    "private limited",
    "holdings",
    "holding",
}
LEGAL_SUFFIX_RE = re.compile(
    r"\b(incorporated|inc\.?|corporation|corp\.?|company|co\.?|limited|ltd\.?|llc|plc|"
    r"n\.?\s*v\.?|s\.?\s*a\.?|ag|gmbh|b\.?\s*v\.?|pte\.?(?:\s+ltd\.?)?|private\s+limited|holdings?)\b",
    flags=re.IGNORECASE,
)
CLASS_OBJECT_RE = re.compile(
    r"\b("
    r"suppliers?|vendors?|customers?|providers?|manufacturers?|subcontractors?|"
    r"foundries|data centers?|cloud platforms?|utilities|utility|fuel suppliers?|"
    r"transportation suppliers?|contract manufacturers?|manufacturing partners?|limited number|single[- ]source|"
    r"third[- ]party|certain customers?|major customers?|channel partners?|partners?"
    r")\b",
    flags=re.IGNORECASE,
)
PLACEHOLDER_OBJECTS = {
    "entity name",
    "company name",
    "competitor",
    "competitors",
    "dependency class",
    "supplier dependency",
    "customer dependency",
    "manufacturing dependency",
    "foundry dependency",
    "cloud or hosting dependency",
    "data center dependency",
    "power or utility dependency",
    "network or interconnection dependency",
    "distribution or channel dependency",
    "concentration risk",
    "pte ltd",
    "ltd",
    "llc",
    "inc",
    "internet of things",
    "light company",
    "corp",
    "corporation",
    "participating company",
    "public company",
}
GEOGRAPHY_OBJECTS = {
    "australia",
    "asia",
    "canada",
    "china",
    "europe",
    "germany",
    "hong kong",
    "india",
    "ireland",
    "israel",
    "japan",
    "korea",
    "macau",
    "malaysia",
    "netherlands",
    "russia",
    "singapore",
    "south korea",
    "taiwan",
    "united kingdom",
    "united states",
}


@dataclass(frozen=True)
class EntityObjectContext:
    object: str
    evidence_count: int = 0
    subject_count: int = 0
    subjects: str = ""
    relation_types: str = ""
    modalities: str = ""
    forms: str = ""
    sample_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GLEIFCandidate:
    query_object: str
    search_query: str
    normalized_query: str
    evidence_count: int
    subject_count: int
    subjects: str
    relation_types: str
    modalities: str
    forms: str
    candidate_rank: int
    resolver_status: str
    resolver_confidence: float
    confidence_band: str
    match_strategy: str
    name_similarity: float
    matched_name: str
    lei: str = ""
    canonical_name: str = ""
    legal_name: str = ""
    legal_name_language: str = ""
    transliterated_names: str = ""
    other_names: str = ""
    jurisdiction: str = ""
    entity_status: str = ""
    entity_category: str = ""
    legal_form_id: str = ""
    registration_status: str = ""
    corroboration_level: str = ""
    legal_country: str = ""
    legal_region: str = ""
    legal_city: str = ""
    headquarters_country: str = ""
    headquarters_region: str = ""
    headquarters_city: str = ""
    registered_at: str = ""
    registered_as: str = ""
    bic: str = ""
    ocid: str = ""
    qcc: str = ""
    spglobal: str = ""
    direct_parent_lei: str = ""
    direct_parent_name: str = ""
    ultimate_parent_lei: str = ""
    ultimate_parent_name: str = ""
    direct_parent_status: str = ""
    ultimate_parent_status: str = ""
    source_url: str = ""
    direct_parent_url: str = ""
    ultimate_parent_url: str = ""
    sample_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GLEIFClient:
    def __init__(
        self,
        base_url: str = GLEIF_API_BASE,
        user_agent: str = DEFAULT_USER_AGENT,
        requests_per_second: float = 2.0,
        proxies: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limiter = RateLimiter(requests_per_second)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/vnd.api+json,application/json"})
        if proxies:
            self.session.proxies.update({key.rstrip(":/"): value for key, value in proxies.items()})

    def search_lei_records(self, query: str, page_size: int = 5) -> list[tuple[dict[str, Any], str]]:
        query = query.strip()
        if not query:
            return []
        records: dict[str, tuple[dict[str, Any], str]] = {}
        for record, strategy in self._search_exact(query, page_size):
            records.setdefault(record.get("id", ""), (record, strategy))
        for record, strategy in self._search_fulltext(query, page_size):
            records.setdefault(record.get("id", ""), (record, strategy))
        for record, strategy in self._search_fuzzy(query, page_size):
            records.setdefault(record.get("id", ""), (record, strategy))
        return [item for lei, item in records.items() if lei]

    def _search_exact(self, query: str, page_size: int) -> list[tuple[dict[str, Any], str]]:
        payload = self.get_json(
            "/lei-records",
            params={"filter[entity.legalName]": query, "page[size]": str(page_size)},
        )
        return [(record, "exact_legal_name") for record in payload.get("data", [])]

    def _search_fulltext(self, query: str, page_size: int) -> list[tuple[dict[str, Any], str]]:
        payload = self.get_json(
            "/lei-records",
            params={"filter[fulltext]": query, "page[size]": str(page_size)},
        )
        return [(record, "fulltext") for record in payload.get("data", [])]

    def _search_fuzzy(self, query: str, page_size: int) -> list[tuple[dict[str, Any], str]]:
        payload = self.get_json(
            "/fuzzycompletions",
            params={"field": "entity.legalName", "q": query, "page[size]": str(page_size)},
        )
        records: list[tuple[dict[str, Any], str]] = []
        for completion in payload.get("data", []):
            lei = (
                completion.get("relationships", {})
                .get("lei-records", {})
                .get("data", {})
                .get("id", "")
            )
            if lei:
                records.append((self.get_lei_record(lei), "fuzzy_legal_name"))
        return records

    def get_lei_record(self, lei: str) -> dict[str, Any]:
        payload = self.get_json(f"/lei-records/{lei}")
        data = payload.get("data", {})
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    def get_related_lei_record(self, related_url: str) -> dict[str, Any]:
        self.rate_limiter.wait()
        response = self.session.get(related_url, timeout=self.timeout)
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        data = response.json().get("data", {})
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.rate_limiter.wait()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


def resolve_object_contexts(
    contexts: list[EntityObjectContext],
    client: GLEIFClient,
    max_candidates: int = 5,
    include_relationships: bool = False,
) -> list[GLEIFCandidate]:
    rows: list[GLEIFCandidate] = []
    for context in contexts:
        query = context.object.strip()
        search_query = clean_gleif_query_object(query)
        if is_likely_non_entity_object(search_query):
            rows.append(no_match_candidate(context, "skipped_non_entity_object"))
            continue
        candidates = build_candidates_for_context(
            context,
            client.search_lei_records(search_query, page_size=max_candidates),
            include_relationships=include_relationships,
            client=client,
            search_query=search_query,
        )[:max_candidates]
        rows.extend(candidates or [no_match_candidate(context, "no_gleif_match")])
    return rows


def build_candidates_for_context(
    context: EntityObjectContext,
    records: list[tuple[dict[str, Any], str]],
    include_relationships: bool = False,
    client: GLEIFClient | None = None,
    search_query: str | None = None,
) -> list[GLEIFCandidate]:
    search_query = search_query or clean_gleif_query_object(context.object)
    scored = []
    for record, strategy in records:
        name_score, matched_name = best_name_similarity(search_query, record)
        confidence = resolver_confidence(name_score, strategy, record)
        scored.append((confidence, name_score, matched_name, strategy, record))
    scored.sort(key=lambda item: (-item[0], -item[1], str(record_lei(item[4]))))
    rows: list[GLEIFCandidate] = []
    for rank, (confidence, name_score, matched_name, strategy, record) in enumerate(scored, start=1):
        parent_payload = relationship_payload(record, client=client, include_relationships=include_relationships)
        rows.append(
            candidate_from_record(
                context=context,
                record=record,
                rank=rank,
                confidence=confidence,
                name_similarity=name_score,
                matched_name=matched_name,
                strategy=strategy,
                parent_payload=parent_payload,
                search_query=search_query,
            )
        )
    return rows


def candidate_from_record(
    context: EntityObjectContext,
    record: dict[str, Any],
    rank: int,
    confidence: float,
    name_similarity: float,
    matched_name: str,
    strategy: str,
    parent_payload: dict[str, str] | None = None,
    search_query: str | None = None,
) -> GLEIFCandidate:
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})
    registration = attrs.get("registration", {})
    legal_name = entity.get("legalName", {}) or {}
    legal_address = entity.get("legalAddress", {}) or {}
    hq_address = entity.get("headquartersAddress", {}) or {}
    registered_at = entity.get("registeredAt", {}) or {}
    legal_form = entity.get("legalForm", {}) or {}
    parent_payload = parent_payload or {}
    all_names = all_record_names(record)
    canonical_name = choose_canonical_name(legal_name.get("name", ""), all_names, matched_name)
    return GLEIFCandidate(
        query_object=context.object,
        search_query=search_query or clean_gleif_query_object(context.object),
        normalized_query=normalize_legal_name(search_query or clean_gleif_query_object(context.object)),
        evidence_count=context.evidence_count,
        subject_count=context.subject_count,
        subjects=context.subjects,
        relation_types=context.relation_types,
        modalities=context.modalities,
        forms=context.forms,
        candidate_rank=rank,
        resolver_status="candidate",
        resolver_confidence=round(confidence, 3),
        confidence_band=confidence_band(confidence),
        match_strategy=strategy,
        name_similarity=round(name_similarity, 3),
        matched_name=matched_name,
        lei=record_lei(record),
        canonical_name=canonical_name,
        legal_name=str(legal_name.get("name") or ""),
        legal_name_language=str(legal_name.get("language") or ""),
        transliterated_names="; ".join(record_transliterated_names(record)),
        other_names="; ".join(record_other_names(record)),
        jurisdiction=str(entity.get("jurisdiction") or ""),
        entity_status=str(entity.get("status") or ""),
        entity_category=str(entity.get("category") or ""),
        legal_form_id=str(legal_form.get("id") or ""),
        registration_status=str(registration.get("status") or ""),
        corroboration_level=str(registration.get("corroborationLevel") or ""),
        legal_country=str(legal_address.get("country") or ""),
        legal_region=str(legal_address.get("region") or ""),
        legal_city=str(legal_address.get("city") or ""),
        headquarters_country=str(hq_address.get("country") or ""),
        headquarters_region=str(hq_address.get("region") or ""),
        headquarters_city=str(hq_address.get("city") or ""),
        registered_at=str(registered_at.get("id") or ""),
        registered_as=str(entity.get("registeredAs") or ""),
        bic="; ".join(attrs.get("bic") or []),
        ocid=str(attrs.get("ocid") or ""),
        qcc=str(attrs.get("qcc") or ""),
        spglobal="; ".join(attrs.get("spglobal") or []),
        direct_parent_lei=parent_payload.get("direct_parent_lei", ""),
        direct_parent_name=parent_payload.get("direct_parent_name", ""),
        ultimate_parent_lei=parent_payload.get("ultimate_parent_lei", ""),
        ultimate_parent_name=parent_payload.get("ultimate_parent_name", ""),
        direct_parent_status=parent_payload.get("direct_parent_status", ""),
        ultimate_parent_status=parent_payload.get("ultimate_parent_status", ""),
        source_url=str(record.get("links", {}).get("self") or ""),
        direct_parent_url=parent_payload.get("direct_parent_url", ""),
        ultimate_parent_url=parent_payload.get("ultimate_parent_url", ""),
        sample_evidence=context.sample_evidence,
    )


def relationship_payload(
    record: dict[str, Any],
    client: GLEIFClient | None = None,
    include_relationships: bool = False,
) -> dict[str, str]:
    relationships = record.get("relationships", {})
    payload: dict[str, str] = {}
    for key, prefix in [("direct-parent", "direct_parent"), ("ultimate-parent", "ultimate_parent")]:
        links = relationships.get(key, {}).get("links", {})
        related_url = str(links.get("related") or "")
        exception_url = str(links.get("reporting-exception") or "")
        payload[f"{prefix}_url"] = related_url or exception_url
        if related_url:
            payload[f"{prefix}_status"] = "available"
            if include_relationships and client:
                parent = client.get_related_lei_record(related_url)
                payload[f"{prefix}_lei"] = record_lei(parent)
                payload[f"{prefix}_name"] = choose_canonical_name(
                    parent.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", ""),
                    all_record_names(parent),
                    "",
                )
        elif exception_url:
            payload[f"{prefix}_status"] = "reporting_exception"
        else:
            payload[f"{prefix}_status"] = "not_available"
    return payload


def no_match_candidate(context: EntityObjectContext, status: str) -> GLEIFCandidate:
    search_query = clean_gleif_query_object(context.object)
    return GLEIFCandidate(
        query_object=context.object,
        search_query=search_query,
        normalized_query=normalize_legal_name(search_query),
        evidence_count=context.evidence_count,
        subject_count=context.subject_count,
        subjects=context.subjects,
        relation_types=context.relation_types,
        modalities=context.modalities,
        forms=context.forms,
        candidate_rank=0,
        resolver_status=status,
        resolver_confidence=0.0,
        confidence_band="none",
        match_strategy="none",
        name_similarity=0.0,
        matched_name="",
        sample_evidence=context.sample_evidence,
    )


def load_object_contexts_from_evidence(
    evidence_path: Path,
    limit: int = 100,
    min_evidence_count: int = 1,
    include_class_objects: bool = False,
) -> list[EntityObjectContext]:
    rows = read_jsonl(evidence_path)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        obj = str(row.get("object") or "").strip()
        if not obj:
            continue
        if not include_class_objects and is_likely_non_entity_object(obj):
            continue
        grouped[obj].append(row)
    contexts: list[EntityObjectContext] = []
    for obj, group in grouped.items():
        if len(group) < min_evidence_count:
            continue
        contexts.append(context_from_group(obj, group))
    contexts.sort(key=lambda item: (-item.evidence_count, -item.subject_count, item.object.lower()))
    return contexts[:limit] if limit > 0 else contexts


def context_from_group(obj: str, group: list[dict[str, Any]]) -> EntityObjectContext:
    subjects = Counter(str(row.get("subject") or "") for row in group if row.get("subject"))
    relation_types = Counter(str(row.get("relation_type") or "") for row in group if row.get("relation_type"))
    modalities = Counter(str(row.get("modality") or "") for row in group if row.get("modality"))
    forms = Counter(str(row.get("form") or "") for row in group if row.get("form"))
    sample = next((str(row.get("evidence_text") or "") for row in group if row.get("evidence_text")), "")
    return EntityObjectContext(
        object=obj,
        evidence_count=len(group),
        subject_count=len(subjects),
        subjects="; ".join(name for name, _ in subjects.most_common(12)),
        relation_types="; ".join(name for name, _ in relation_types.most_common(12)),
        modalities="; ".join(name for name, _ in modalities.most_common(8)),
        forms="; ".join(name for name, _ in forms.most_common(8)),
        sample_evidence=sample[:600],
    )


def contexts_from_object_strings(objects: list[str]) -> list[EntityObjectContext]:
    return [EntityObjectContext(object=obj.strip()) for obj in objects if obj.strip()]


def write_candidate_queue(output_dir: Path, candidates: list[GLEIFCandidate], prefix: str = "entity_resolution_candidates") -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [candidate.to_dict() for candidate in candidates]
    csv_path = output_dir / f"{prefix}.csv"
    jsonl_path = output_dir / f"{prefix}.jsonl"
    summary_path = output_dir / f"{prefix}.summary.json"
    write_csv(csv_path, rows)
    write_jsonl(jsonl_path, rows)
    write_json(summary_path, candidate_summary(candidates))
    return {"csv": str(csv_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}


def candidate_summary(candidates: list[GLEIFCandidate]) -> dict[str, Any]:
    query_count = len({candidate.query_object for candidate in candidates})
    status_counts = Counter(candidate.resolver_status for candidate in candidates)
    band_counts = Counter(candidate.confidence_band for candidate in candidates if candidate.resolver_status == "candidate")
    high_confidence = [
        candidate.to_dict()
        for candidate in candidates
        if candidate.candidate_rank == 1 and candidate.confidence_band in {"high", "very_high"}
    ][:25]
    return {
        "query_object_count": query_count,
        "candidate_row_count": len(candidates),
        "status_counts": dict(status_counts.most_common()),
        "confidence_band_counts": dict(band_counts.most_common()),
        "high_confidence_top_candidates": high_confidence,
    }


def best_name_similarity(query: str, record: dict[str, Any]) -> tuple[float, str]:
    query_norm = normalize_legal_name(query)
    query_core = strip_legal_suffixes(query_norm)
    best_score = 0.0
    best_name = ""
    for name in all_record_names(record):
        name_norm = normalize_legal_name(name)
        name_core = strip_legal_suffixes(name_norm)
        score = max(
            sequence_ratio(query_norm, name_norm),
            token_sort_ratio(query_norm, name_norm),
            sequence_ratio(query_core, name_core),
            token_sort_ratio(query_core, name_core),
        )
        if query_norm == name_norm:
            score = max(score, 1.0)
        elif query_core and query_core == name_core:
            score = max(score, 0.96)
        elif query_core and (query_core in name_core or name_core in query_core):
            score = max(score, min(0.9, 0.72 + 0.02 * min(len(query_core.split()), len(name_core.split()))))
        if score > best_score:
            best_score = score
            best_name = name
    return round(best_score, 4), best_name


def resolver_confidence(name_similarity: float, strategy: str, record: dict[str, Any]) -> float:
    strategy_bonus = {
        "exact_legal_name": 0.08,
        "fuzzy_legal_name": 0.04,
        "fulltext": 0.0,
    }.get(strategy, 0.0)
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})
    registration = attrs.get("registration", {})
    quality_bonus = 0.0
    if entity.get("status") == "ACTIVE":
        quality_bonus += 0.015
    if registration.get("status") == "ISSUED":
        quality_bonus += 0.015
    if registration.get("corroborationLevel") == "FULLY_CORROBORATED":
        quality_bonus += 0.015
    if attrs.get("conformityFlag") == "CONFORMING":
        quality_bonus += 0.01
    return min(0.99, max(0.0, name_similarity + strategy_bonus + quality_bonus))


def confidence_band(confidence: float) -> str:
    if confidence >= 0.95:
        return "very_high"
    if confidence >= 0.86:
        return "high"
    if confidence >= 0.72:
        return "medium"
    if confidence > 0:
        return "low"
    return "none"


def normalize_legal_name(name: str) -> str:
    normalized = name.casefold()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"['`´]", "", normalized)
    normalized = re.sub(r"[^0-9a-z]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def strip_legal_suffixes(normalized_name: str) -> str:
    words = [word for word in normalized_name.split() if word not in LEGAL_SUFFIXES]
    compact = " ".join(words)
    compact = LEGAL_SUFFIX_RE.sub(" ", compact)
    return re.sub(r"\s+", " ", compact).strip()


def sequence_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def token_sort_ratio(left: str, right: str) -> float:
    return sequence_ratio(" ".join(sorted(left.split())), " ".join(sorted(right.split())))


def is_likely_non_entity_object(value: str) -> bool:
    normalized = normalize_legal_name(value)
    if not normalized or len(normalized) < 3:
        return True
    if normalized in PLACEHOLDER_OBJECTS or normalized in GEOGRAPHY_OBJECTS:
        return True
    if len(normalized.split()) == 1 and not has_legal_suffix(value):
        return True
    if normalized.endswith(" class") or normalized.endswith(" dependency class") or normalized.endswith(" risk class"):
        return True
    if " dependency class" in normalized or normalized.endswith(" exposure class"):
        return True
    if CLASS_OBJECT_RE.search(value) and not has_legal_suffix(value):
        return True
    if normalized in {"china", "united states", "taiwan", "europe", "asia", "russia"}:
        return True
    return False


def clean_gleif_query_object(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^contents\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^notes?\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:.")
    return cleaned


def has_legal_suffix(value: str) -> bool:
    words = normalize_legal_name(value).split()
    if not words:
        return False
    return words[-1] in LEGAL_SUFFIXES or " ".join(words[-2:]) in LEGAL_SUFFIXES


def all_record_names(record: dict[str, Any]) -> list[str]:
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})
    names: list[str] = []
    legal_name = entity.get("legalName", {}) or {}
    if legal_name.get("name"):
        names.append(str(legal_name["name"]))
    names.extend(record_other_names(record))
    names.extend(record_transliterated_names(record))
    return dedupe_preserve_order(names)


def record_other_names(record: dict[str, Any]) -> list[str]:
    entity = record.get("attributes", {}).get("entity", {})
    return [str(item.get("name")) for item in entity.get("otherNames", []) if item.get("name")]


def record_transliterated_names(record: dict[str, Any]) -> list[str]:
    entity = record.get("attributes", {}).get("entity", {})
    return [str(item.get("name")) for item in entity.get("transliteratedOtherNames", []) if item.get("name")]


def choose_canonical_name(legal_name: str, all_names: list[str], matched_name: str) -> str:
    if matched_name and re.search(r"[A-Za-z]", matched_name):
        return matched_name
    if legal_name and re.search(r"[A-Za-z]", legal_name):
        return legal_name
    for name in all_names:
        if re.search(r"[A-Za-z]", name):
            return name
    return legal_name or (all_names[0] if all_names else "")


def record_lei(record: dict[str, Any]) -> str:
    return str(record.get("attributes", {}).get("lei") or record.get("id") or "")


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def run_gleif_resolution(
    evidence_path: Path | None,
    objects: list[str],
    output_dir: Path,
    client: GLEIFClient,
    limit_objects: int = 100,
    min_evidence_count: int = 1,
    max_candidates: int = 5,
    include_class_objects: bool = False,
    include_relationships: bool = False,
    output_prefix: str = "entity_resolution_candidates",
) -> dict[str, Any]:
    if objects:
        contexts = contexts_from_object_strings(objects)
    elif evidence_path:
        contexts = load_object_contexts_from_evidence(
            evidence_path,
            limit=limit_objects,
            min_evidence_count=min_evidence_count,
            include_class_objects=include_class_objects,
        )
    else:
        raise ValueError("Either evidence_path or objects must be provided.")
    candidates = resolve_object_contexts(
        contexts,
        client=client,
        max_candidates=max_candidates,
        include_relationships=include_relationships,
    )
    paths = write_candidate_queue(output_dir, candidates, prefix=output_prefix)
    return {"contexts": contexts, "candidates": candidates, "paths": paths}
