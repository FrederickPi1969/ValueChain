from __future__ import annotations

import re
from typing import Any

from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.multilingual.evidence import (
    evidence_failure_reason,
    normalize_match_text,
)
from valuechain.financial_ie.multilingual.prompts import (
    MODALITIES,
    RELATION_DIRECTIONS,
    RELATION_TYPES,
)
from valuechain.financial_ie.pilot_prompts import (
    PRIMARY_INDUSTRIES,
    SIGNAL_CATEGORIES,
    STRATEGIC_DOMAINS,
    VALUE_CHAIN_ROLES,
)


SCHEMA_VERSION = "multilingual-financial-ie-v0.3"
DIRECTIONS = ("positive", "negative", "mixed", "neutral")
TEMPORAL_SCOPES = ("current", "historical", "future", "unspecified")
CERTAINTIES = ("explicit", "strongly_implied")


def empty_profile() -> dict[str, Any]:
    return {
        "business_summary_native": "",
        "business_summary_en": "",
        "primary_industry": None,
        "strategic_domains": [],
        "value_chain_roles": [],
        "products_services_native": [],
        "end_markets_native": [],
        "operating_geographies_native": [],
        "evidence": [],
        "evidence_valid": True,
        "translation_status": "not_requested",
    }


def normalize_profile(payload: Any, chunks: dict[str, DocumentChunk]) -> dict[str, Any]:
    root = payload if isinstance(payload, dict) else {}
    row = root.get("profile", root)
    if not isinstance(row, dict):
        return empty_profile()
    evidence = _normalize_evidence(row.get("evidence"), chunks, "quote_native", limit=4)
    english_summary = clean_text(row.get("business_summary_en"), 900)
    return {
        "business_summary_native": clean_text(row.get("business_summary_native"), 900),
        "business_summary_en": english_summary,
        "primary_industry": allowed_value(row.get("primary_industry"), PRIMARY_INDUSTRIES),
        "strategic_domains": allowed_list(row.get("strategic_domains"), STRATEGIC_DOMAINS, 5),
        "value_chain_roles": allowed_list(row.get("value_chain_roles"), VALUE_CHAIN_ROLES, 5),
        "products_services_native": text_list(row.get("products_services_native"), 8, 180),
        "end_markets_native": text_list(row.get("end_markets_native"), 6, 180),
        "operating_geographies_native": text_list(
            row.get("operating_geographies_native"), 6, 140
        ),
        "evidence": evidence,
        "evidence_valid": all(item["evidence_valid"] for item in evidence),
        "translation_status": "model_generated_unverified" if english_summary else "not_available",
    }


def normalize_signal_relation_payload(
    payload: Any,
    chunks: dict[str, DocumentChunk],
    issuer_name: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = payload if isinstance(payload, dict) else {}
    return normalize_signals(root.get("signals"), chunks), normalize_relations(
        root.get("relations"), chunks, issuer_name
    )


def normalize_signals(value: Any, chunks: dict[str, DocumentChunk]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    signals: list[dict[str, Any]] = []
    for raw in value[:6]:
        if not isinstance(raw, dict):
            continue
        category = allowed_value(raw.get("category"), SIGNAL_CATEGORIES)
        statement = clean_text(raw.get("statement_native"), 1000)
        if not category or not statement:
            continue
        chunk_id = clean_text(raw.get("chunk_id"), 180)
        quote = clean_text(raw.get("evidence_quote_native"), 1200)
        chunk = chunks.get(chunk_id)
        failure = evidence_failure_reason(bool(chunk), quote, chunk.text if chunk else "")
        modality = allowed_value(raw.get("modality"), MODALITIES)
        english_statement = clean_text(raw.get("statement_en"), 1000)
        signals.append(
            {
                "category": category,
                "headline_native": clean_text(raw.get("headline_native"), 220),
                "headline_en": clean_text(raw.get("headline_en"), 220),
                "statement_native": statement,
                "statement_en": english_statement,
                "direction": allowed_value(raw.get("direction"), DIRECTIONS) or "neutral",
                "modality": modality,
                "significance": bounded_int(raw.get("significance"), 1, 5),
                "significance_rationale_en": clean_text(
                    raw.get("significance_rationale_en"), 420
                ),
                "confidence": bounded_float(raw.get("confidence"), 0.0, 1.0),
                "chunk_id": chunk_id,
                "evidence_quote_native": quote,
                "evidence_valid": not failure,
                "evidence_failure_reason": failure,
                "source_section": chunk.section_hint if chunk else "",
                "translation_status": (
                    "model_generated_unverified" if english_statement else "not_available"
                ),
                "review_status": (
                    "candidate" if not failure and modality else "needs_review"
                ),
            }
        )
    return _deduplicate(signals, "category", "statement_native")


def normalize_relations(
    value: Any,
    chunks: dict[str, DocumentChunk],
    issuer_name: str = "",
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    relations: list[dict[str, Any]] = []
    for raw in value[:6]:
        if not isinstance(raw, dict):
            continue
        relation_type = allowed_value(raw.get("relation_type"), RELATION_TYPES)
        subject = clean_text(raw.get("subject_native"), 240)
        object_name = clean_text(raw.get("object_native"), 280)
        if not relation_type or not subject or not object_name:
            continue
        chunk_id = clean_text(raw.get("chunk_id"), 180)
        quote = clean_text(raw.get("evidence_quote_native"), 1200)
        chunk = chunks.get(chunk_id)
        failure = evidence_failure_reason(bool(chunk), quote, chunk.text if chunk else "")
        modality = allowed_value(raw.get("modality"), MODALITIES)
        temporal_scope = allowed_value(raw.get("temporal_scope"), TEMPORAL_SCOPES)
        certainty = allowed_value(raw.get("certainty"), CERTAINTIES)
        direction = allowed_value(raw.get("direction"), RELATION_DIRECTIONS)
        semantic_warning = relation_semantic_warning(
            relation_type, object_name, quote, direction, issuer_name
        )
        relations.append(
            {
                "subject_native": subject,
                "subject_en": clean_text(raw.get("subject_en"), 240),
                "object_native": object_name,
                "object_en": clean_text(raw.get("object_en"), 280),
                "relation_type": relation_type,
                "direction": direction,
                "modality": modality,
                "temporal_scope": temporal_scope,
                "certainty": certainty,
                "confidence": bounded_float(raw.get("confidence"), 0.0, 1.0),
                "chunk_id": chunk_id,
                "evidence_quote_native": quote,
                "evidence_valid": not failure,
                "evidence_failure_reason": failure,
                "source_section": chunk.section_hint if chunk else "",
                "semantic_warning": semantic_warning,
                "review_status": (
                    "candidate"
                    if not failure
                    and modality
                    and temporal_scope
                    and certainty
                    and direction
                    and not semantic_warning
                    else "needs_review"
                ),
            }
        )
    return _deduplicate(relations, "relation_type", "subject_native", "object_native")


def _normalize_evidence(
    value: Any,
    chunks: dict[str, DocumentChunk],
    quote_key: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in value[:limit]:
        if not isinstance(raw, dict):
            continue
        chunk_id = clean_text(raw.get("chunk_id"), 180)
        quote = clean_text(raw.get(quote_key), 1200)
        chunk = chunks.get(chunk_id)
        failure = evidence_failure_reason(bool(chunk), quote, chunk.text if chunk else "")
        rows.append(
            {
                "chunk_id": chunk_id,
                "quote_native": quote,
                "evidence_valid": not failure,
                "evidence_failure_reason": failure,
                "source_section": chunk.section_hint if chunk else "",
            }
        )
    return rows


def clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def text_list(value: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = [clean_text(item, item_limit) for item in value]
    return list(dict.fromkeys(item for item in cleaned if item))[:limit]


def allowed_value(value: Any, allowed: tuple[str, ...]) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else None


def allowed_list(value: Any, allowed: tuple[str, ...], limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    rows = [allowed_value(item, allowed) for item in value]
    return list(dict.fromkeys(item for item in rows if item))[:limit]


def bounded_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def bounded_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = minimum
    return round(max(minimum, min(maximum, parsed)), 3)


def _deduplicate(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(normalize_match_text(str(row.get(name) or "")) for name in keys)
        unique.setdefault(key, row)
    return list(unique.values())


CONTROL_CUES = (
    "子公司",
    "附属公司",
    "控股",
    "控制",
    "納入合併",
    "纳入合并",
    "子会社",
    "親会社",
    "支配",
    "連結対象",
    "종속기업",
    "종속회사",
    "자회사",
    "지배",
    "연결대상",
    "subsidiary",
    "controlled",
)
LICENSE_CUES = (
    "许可",
    "許可",
    "授权",
    "授權",
    "ライセンス",
    "使用権",
    "許諾",
    "라이선스",
    "사용권",
    "license",
)
INVESTMENT_CUES = (
    "投资",
    "投資",
    "出资",
    "出資",
    "取得股份",
    "取得",
    "持股",
    "股權",
    "出資",
    "투자",
    "출자",
    "지분",
    "investment",
)
ORDINAL_OBJECT_RE = re.compile(
    r"^(?:第?[一二三四五六七八九十]+名|第?\d+名|排名第?\d+|[一二三四五]大)$"
)
ANONYMIZED_OBJECT_RE = re.compile(
    r"^(?:(?:VEN|VENDOR|SUP|SUPPLIER|CUS|CUSTOMER|客户|客戶|供应商|供應商)[-_]?)\d{3,}$",
    re.IGNORECASE,
)
MULTI_ENTITY_OBJECT_RE = re.compile(r"、|，|,\s+|;\s*")
GENERIC_CONTROL_OBJECTS = {
    "主要子公司",
    "子公司",
    "主要附屬公司",
    "附屬公司",
    "主要子会社",
    "子会社",
    "주요자회사",
    "자회사",
    "주요종속회사",
    "종속회사",
}
EXPECTED_DIRECTIONS = {
    "supplier_dependency": {"subject_depends_on_object"},
    "customer_dependency": {"subject_depends_on_object"},
    "manufacturing_dependency": {"subject_depends_on_object"},
    "cloud_or_hosting_dependency": {"subject_depends_on_object"},
    "data_center_dependency": {"subject_depends_on_object"},
    "power_or_utility_dependency": {"subject_depends_on_object"},
    "distribution_or_channel_dependency": {"subject_depends_on_object"},
    "strategic_partner": {"bidirectional"},
    "co_investment": {"subject_invests_in_object", "object_invests_in_subject"},
    "licensing_dependency": {"subject_depends_on_object"},
    "facility_or_geographic_exposure": {"subject_exposed_to_object"},
    "concentration_risk": {"subject_exposed_to_object"},
    "subsidiary_or_control": {"subject_controls_object", "object_controls_subject"},
}
RELATION_CUES = {
    "supplier_dependency": (
        "供应",
        "供應",
        "采购",
        "採購",
        "仕入",
        "調達",
        "供給",
        "공급",
        "조달",
        "매입",
        "supplier",
        "procure",
    ),
    "customer_dependency": (
        "客户",
        "客戶",
        "销售",
        "銷售",
        "顧客",
        "売上",
        "販売",
        "依拠",
        "매출처",
        "고객",
        "판매",
        "의존",
        "customer",
        "revenue",
    ),
    "manufacturing_dependency": ("代工", "製造委託", "ファウンドリ", "위탁생산", "foundry"),
    "cloud_or_hosting_dependency": ("云服务", "雲端", "クラウド", "클라우드", "hosting"),
    "data_center_dependency": ("数据中心", "資料中心", "データセンター", "데이터센터"),
    "power_or_utility_dependency": ("电力", "電力", "電気", "전력", "utility"),
    "distribution_or_channel_dependency": (
        "经销",
        "經銷",
        "渠道",
        "販売代理",
        "유통",
        "판매채널",
        "distributor",
    ),
    "strategic_partner": (
        "战略合作",
        "策略合作",
        "合作",
        "合作伙伴",
        "提携",
        "協業",
        "파트너십",
        "전략적 제휴",
        "협력",
        "partner",
    ),
    "co_investment": INVESTMENT_CUES,
    "licensing_dependency": LICENSE_CUES,
    "facility_or_geographic_exposure": (
        "生产基地",
        "生產基地",
        "拠点",
        "施設",
        "사업장",
        "생산시설",
        "facility",
    ),
    "subsidiary_or_control": CONTROL_CUES,
    "concentration_risk": ("集中", "占比", "比例", "割合", "비중", "concentration"),
}


def relation_semantic_warning(
    relation_type: str,
    object_name: str,
    quote: str,
    direction: str | None,
    issuer_name: str = "",
) -> str:
    compact_object = re.sub(r"\s+", "", object_name)
    if ORDINAL_OBJECT_RE.match(compact_object):
        return "non_entity_ordinal_object"
    if ANONYMIZED_OBJECT_RE.match(compact_object):
        return "anonymized_counterparty_object"
    if MULTI_ENTITY_OBJECT_RE.search(object_name):
        return "multi_entity_object_requires_split"
    normalized_object = normalize_match_text(object_name)
    normalized_quote = normalize_match_text(quote)
    normalized_issuer = normalize_match_text(issuer_name)
    if (
        normalized_object
        and normalized_object not in normalized_quote
        and normalized_object != normalized_issuer
    ):
        return "object_not_explicit_in_quote"
    if relation_type == "subsidiary_or_control" and not any(cue in quote for cue in CONTROL_CUES):
        return "control_not_explicit_in_quote"
    if relation_type == "subsidiary_or_control" and compact_object in GENERIC_CONTROL_OBJECTS:
        return "generic_control_object"
    if relation_type == "subsidiary_or_control" and direction not in {
        "subject_controls_object",
        "object_controls_subject",
    }:
        return "invalid_control_direction"
    if relation_type == "strategic_partner" and any(cue in quote for cue in LICENSE_CUES):
        return "license_misclassified_as_partner"
    if relation_type == "co_investment" and not any(cue in quote for cue in INVESTMENT_CUES):
        return "investment_not_explicit_in_quote"
    if direction and direction not in EXPECTED_DIRECTIONS.get(relation_type, set()):
        return "direction_inconsistent_with_relation_type"
    cues = RELATION_CUES.get(relation_type, ())
    folded_quote = quote.casefold()
    if cues and not any(cue.casefold() in folded_quote for cue in cues):
        return "relation_type_cue_missing"
    return ""


def refresh_relation_review(row: dict[str, Any], issuer_name: str = "") -> None:
    warning = relation_semantic_warning(
        str(row.get("relation_type") or ""),
        str(row.get("object_native") or ""),
        str(row.get("evidence_quote_native") or ""),
        str(row.get("direction") or "") or None,
        issuer_name,
    )
    row["semantic_warning"] = warning
    row["review_status"] = (
        "candidate"
        if row.get("evidence_valid")
        and row.get("modality")
        and row.get("temporal_scope")
        and row.get("certainty")
        and row.get("direction")
        and not warning
        else "needs_review"
    )
