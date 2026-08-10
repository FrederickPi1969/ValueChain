from __future__ import annotations

import json

from valuechain.financial_ie.models import DocumentChunk
from valuechain.financial_ie.pilot_prompts import (
    PRIMARY_INDUSTRIES,
    SIGNAL_CATEGORIES,
    STRATEGIC_DOMAINS,
    VALUE_CHAIN_ROLES,
)


RELATION_TYPES = (
    "supplier_dependency",
    "customer_dependency",
    "manufacturing_dependency",
    "cloud_or_hosting_dependency",
    "data_center_dependency",
    "power_or_utility_dependency",
    "distribution_or_channel_dependency",
    "strategic_partner",
    "co_investment",
    "licensing_dependency",
    "facility_or_geographic_exposure",
    "subsidiary_or_control",
    "concentration_risk",
)

MODALITIES = ("current_fact", "historical_fact", "forward_looking", "risk_hypothetical")
RELATION_DIRECTIONS = (
    "subject_depends_on_object",
    "object_depends_on_subject",
    "subject_controls_object",
    "object_controls_subject",
    "subject_invests_in_object",
    "object_invests_in_subject",
    "subject_exposed_to_object",
    "bidirectional",
)

SIGNAL_DEFINITIONS = {
    "demand_and_revenue": "reported demand, sales, revenue, orders, bookings, or backlog",
    "pricing_and_margin": "price, input cost, gross margin, operating margin, or profitability movement",
    "capital_allocation": "capex, investment, financing, buyback, dividend, or debt allocation",
    "capacity_and_supply": "capacity, utilization, production, inventory, shortage, or supply availability",
    "customer_concentration": "named major customer or measured customer concentration",
    "supplier_or_infrastructure_dependency": "operating reliance on suppliers, cloud, data center, network, or power",
    "regulatory_or_geopolitical": "law, policy, export control, tariff, sanction, or geopolitical exposure",
    "technology_and_product": "R&D, product launch, technical milestone, process transition, or obsolescence",
    "partnership_or_mna": "partnership, acquisition, disposal, merger, or joint venture event",
    "liquidity_and_balance_sheet": "cash flow, liquidity, borrowing, covenant, leverage, or solvency",
}

RELATION_DEFINITIONS = {
    "supplier_dependency": "subject buys or relies on an input supplied by object",
    "customer_dependency": "object is a customer, buyer, revenue counterparty, or demand source for subject",
    "manufacturing_dependency": "object manufactures, fabricates, packages, or assembles for subject",
    "cloud_or_hosting_dependency": "subject relies on object's cloud or hosting service",
    "data_center_dependency": "subject relies on object's data-center or colocation facility",
    "power_or_utility_dependency": "subject relies on object for electricity, fuel, water, or utility service",
    "distribution_or_channel_dependency": "object distributes, resells, or provides a sales channel for subject",
    "strategic_partner": "active operating or technology collaboration; exclude licenses, customers, suppliers, and investments",
    "co_investment": "equity investment, joint investment, or investment vehicle relationship",
    "licensing_dependency": "patent, software, brand, manufacturing-right, or technology license",
    "facility_or_geographic_exposure": "material operating facility or country exposure",
    "subsidiary_or_control": "explicit ownership, subsidiary, parent, consolidation, or control relationship",
    "concentration_risk": "explicit customer, supplier, geography, or facility concentration",
}


PROFILE_SYSTEMS = {
    "zh-Hans": """你从中国公司监管披露中抽取公司业务画像。只使用提供的原文块，输出一个 JSON 对象，不得使用常识补全。所有重要结论必须引用同一块中的简体中文原句。缺失信息返回 null 或空数组。英文内容仅作为衍生翻译，不能代替中文证据。""",
    "zh-Hant": """你從繁體中文監管披露中抽取公司業務畫像。只使用提供的原文區塊，輸出一個 JSON 物件，不得以常識補全。所有重要結論必須引用同一區塊中的繁體中文原句。缺少資訊時回傳 null 或空陣列。英文內容只是衍生翻譯，不能取代原文證據。""",
    "ja": """日本語の法定開示から企業プロフィールを抽出します。提供されたチャンクだけを使用し、JSON オブジェクトを一つだけ返してください。一般知識で補完せず、重要な主張には同じチャンク内の日本語の原文引用が必要です。不明な項目は null または空配列にしてください。英訳は派生情報であり、証拠ではありません。""",
    "ko": """한국어 규제 공시에서 기업 프로필을 추출합니다. 제공된 청크만 사용하고 JSON 객체 하나만 반환하십시오. 외부 지식으로 보완하지 말고, 중요한 주장마다 동일 청크의 정확한 한국어 원문 인용을 제시하십시오. 근거가 없으면 null 또는 빈 배열을 사용하십시오. 영어 번역은 파생 정보이며 증거가 아닙니다.""",
}

SIGNAL_SYSTEMS = {
    "zh-Hans": """你是做公开市场研究的金融信息抽取器。只根据提供的简体中文披露抽取重大信号和关系，输出一个 JSON 对象。原文证据必须逐字来自指定块。严格区分当前事实、历史事实、前瞻计划和假设风险；假设风险不能转成当前依赖。宁可返回空数组，也不要生成通用套话或外部知识。""",
    "zh-Hant": """你是公開市場研究用的金融資訊抽取器。只根據提供的繁體中文披露抽取重大訊號與關係，輸出一個 JSON 物件。原文證據必須逐字出自指定區塊。嚴格區分目前事實、歷史事實、前瞻計畫與假設風險；假設風險不可改寫成目前依賴。寧可回傳空陣列，也不要產生通用敘述或外部知識。""",
    "ja": """公開株式調査向けの金融情報抽出器です。提供された日本語開示だけから重要シグナルと関係を抽出し、JSON オブジェクトを一つ返してください。引用は指定チャンクの原文と完全に対応させます。現在の事実、過去の事実、将来計画、仮定的リスクを厳密に区別し、仮定的リスクを現在の依存関係に変換してはいけません。一般論なら空配列を返してください。""",
    "ko": """상장주식 분석용 금융 정보 추출기입니다. 제공된 한국어 공시만으로 중요한 신호와 관계를 추출하고 JSON 객체 하나를 반환하십시오. 인용문은 지정 청크의 정확한 원문이어야 합니다. 현재 사실, 과거 사실, 미래 계획, 가정적 위험을 엄격히 구분하고 가정적 위험을 현재 의존 관계로 바꾸지 마십시오. 일반적인 문구뿐이면 빈 배열을 반환하십시오.""",
}


MODALITY_EXAMPLES = {
    "zh-Hans": (
        "“公司目前向甲公司采购核心部件” => current_fact；"
        "“若供应商中断供货，经营可能受影响” => risk_hypothetical，不能据此建立当前甲公司关系。"
    ),
    "zh-Hant": (
        "「公司目前向甲公司採購核心零組件」=> current_fact；"
        "「若供應商中斷供貨，營運可能受影響」=> risk_hypothetical，不可據此建立目前甲公司關係。"
    ),
    "ja": (
        "「現在A社から主要部品を調達している」=> current_fact。"
        "「供給者が停止した場合、業績に影響する可能性がある」=> risk_hypothetical であり、現在のA社関係ではない。"
    ),
    "ko": (
        "‘현재 A사에서 핵심 부품을 조달한다’ => current_fact. "
        "‘공급 중단 시 영업에 영향을 받을 수 있다’ => risk_hypothetical이며 현재 A사 관계가 아니다."
    ),
}


def build_profile_prompt(
    company_name: str,
    filing_title: str,
    language: str,
    chunks: list[DocumentChunk],
) -> str:
    return f"""Company: {company_name}
Filing: {filing_title}
Source language: {language}

Allowed primary_industry values:
{json.dumps(PRIMARY_INDUSTRIES)}
Allowed strategic_domains values:
{json.dumps(STRATEGIC_DOMAINS)}
Allowed value_chain_roles values:
{json.dumps(VALUE_CHAIN_ROLES)}

Return exactly this schema:
{{
  "profile": {{
    "business_summary_native": "one or two factual sentences in the source language",
    "business_summary_en": "faithful English translation, or null",
    "primary_industry": "one allowed value or null",
    "strategic_domains": ["up to five allowed values"],
    "value_chain_roles": ["up to five allowed values"],
    "products_services_native": ["up to eight source-language items"],
    "end_markets_native": ["up to six source-language items"],
    "operating_geographies_native": ["up to six source-language items"],
    "evidence": [{{"chunk_id":"supplied id","quote_native":"exact source-language quote"}}]
  }}
}}

Do not infer a company profile from an event notice that does not describe the business. Use no more than four
evidence entries. Never translate or paraphrase quote_native.

SOURCE CHUNKS:
{render_chunks(chunks)}"""


def build_signal_relation_prompt(
    company_name: str,
    filing_title: str,
    filing_type: str,
    language: str,
    chunks: list[DocumentChunk],
    profile: dict[str, object],
) -> str:
    profile_context = {
        key: profile.get(key)
        for key in ("primary_industry", "strategic_domains", "value_chain_roles")
    }
    return f"""Company: {company_name}
Filing: {filing_title}
Filing type: {filing_type}
Source language: {language}
Profile context: {json.dumps(profile_context, ensure_ascii=False)}

Allowed signal categories:
{json.dumps(SIGNAL_CATEGORIES)}
Category definitions:
{json.dumps(SIGNAL_DEFINITIONS, ensure_ascii=False)}
Allowed relation types:
{json.dumps(RELATION_TYPES)}
Relation definitions:
{json.dumps(RELATION_DEFINITIONS, ensure_ascii=False)}
Allowed modalities:
{json.dumps(MODALITIES)}
Allowed relation directions:
{json.dumps(RELATION_DIRECTIONS)}

Return exactly this schema:
{{
  "signals": [{{
    "category": "one allowed category",
    "headline_native": "short factual source-language headline",
    "headline_en": "faithful English translation or null",
    "statement_native": "company-specific disclosed fact in the source language",
    "statement_en": "faithful English translation or null",
    "direction": "positive|negative|mixed|neutral",
    "modality": "current_fact|historical_fact|forward_looking|risk_hypothetical",
    "significance": "integer from 1 through 5",
    "significance_rationale_en": "why an ETF analyst may care, under 25 words",
    "confidence": 0.0,
    "chunk_id": "supplied id",
    "evidence_quote_native": "exact source-language quote"
  }}],
  "relations": [{{
    "subject_native": "disclosed company/entity name",
    "subject_en": "English name or null",
    "object_native": "named counterparty or explicitly disclosed generic dependency object",
    "object_en": "English name/translation or null",
    "relation_type": "one allowed relation type",
    "direction": "one allowed relation direction",
    "modality": "current_fact|historical_fact|forward_looking|risk_hypothetical",
    "temporal_scope": "current|historical|future|unspecified",
    "certainty": "explicit|strongly_implied",
    "confidence": 0.0,
    "chunk_id": "supplied id",
    "evidence_quote_native": "exact source-language quote"
  }}]
}}

Rules:
- Return at most six signals and six relations. Empty arrays are valid and preferable to boilerplate.
- A signal must be company-specific and decision-relevant. Do not extract meeting logistics or routine legal text.
- Significance rubric: 5 can alter an investment thesis; 4 is a material monitorable driver; 3 is useful context;
  2 is secondary; 1 is minor. Choose a value using this rubric rather than copying the schema example.
- Do not force category diversity. Use the definition that directly matches the evidence.
- A relation requires an explicit dependency, control, transaction, partnership, concentration, or exposure.
- Keep the actual disclosed subject. In a consolidated filing, a subsidiary's relationship remains attributed to
  that subsidiary unless the text explicitly attributes it to the parent issuer.
- Direction is semantic, not just JSON order: use subject_controls_object when the subject is the parent;
  object_controls_subject when the object is the parent; subject_depends_on_object for a subject relying on a
  supplier/customer/license; subject_invests_in_object for an investment; and bidirectional for mutual partnership.
- Acquiring assets or a business from a company does not mean the seller becomes a subsidiary. Use
  subsidiary_or_control only when ownership, control, subsidiary status, or consolidation is explicit.
- A commercial purchase, sale, foundry, supply, or manufacturing agreement is not by itself a strategic
  partnership. Classify it by the operating dependency and customer/supplier roles disclosed in the text.
- An unnamed object must be a meaningful category such as "major customers" or "single supplier"; table ranks
  such as "No. 1" or "first place" are not entities. Use concentration_risk for measured unnamed concentration.
- Put only one named legal entity in each relation object. Split a list of named counterparties into separate
  relations; do not emit anonymized vendor/customer codes as resolved entities.
- A signed agreement still in force is current_fact even when deliveries continue in the future. A completed/expired
  event is historical_fact; only an intention, forecast, or unsigned plan is forward_looking.
- Keep unnamed objects in their disclosed source-language form; do not invent a legal entity.
- evidence_quote_native must be one contiguous copy-pasted span from its chunk. Do not join table cells, rows,
  bullets, or sentences; do not add field labels; do not normalize punctuation. If a contiguous supporting span
  cannot be copied, omit the item. Before returning JSON, verify each quote occurs verbatim in its cited chunk.
- {MODALITY_EXAMPLES[language]}

SOURCE CHUNKS:
{render_chunks(chunks)}"""


def render_chunks(chunks: list[DocumentChunk]) -> str:
    return "\n\n".join(
        f"[chunk_id={chunk.chunk_id}; section={chunk.section_hint or 'unknown'}]\n{chunk.text}"
        for chunk in chunks
    )


EVIDENCE_REPAIR_SYSTEMS = {
    "zh-Hans": "只修复监管披露引用。不得修改结论、块编号或生成新结论。输出 JSON；每个修复引用必须是指定原文块中连续、逐字复制且足以支持结论的一段。",
    "zh-Hant": "只修復監管披露引用。不可修改結論、區塊編號或產生新結論。輸出 JSON；每個修復引用必須是指定原文區塊中連續、逐字複製且足以支持結論的一段。",
    "ja": "開示引用だけを修復します。主張やチャンクIDを変更せず、新しい主張を作らないでください。各引用は指定チャンクから連続して完全にコピーし、主張を十分に裏付ける必要があります。JSONのみを返します。",
    "ko": "공시 인용문만 수정합니다. 주장이나 청크 ID를 변경하거나 새 주장을 만들지 마십시오. 각 인용문은 지정 청크에서 연속된 원문을 그대로 복사하고 주장을 충분히 뒷받침해야 합니다. JSON만 반환하십시오.",
}


def build_evidence_repair_prompt(items: list[dict[str, str]]) -> str:
    rendered = []
    for item in items:
        rendered.append(
            "\n".join(
                (
                    f"[item_id={item['item_id']}; chunk_id={item['chunk_id']}]",
                    f"Claim: {item['claim_native']}",
                    f"Failed quote: {item['failed_quote']}",
                    "SOURCE CHUNK:",
                    item["source_chunk"],
                )
            )
        )
    return f"""Return this schema:
{{"repairs":[{{"item_id":"unchanged id","chunk_id":"unchanged id","quote_native":"one exact contiguous source span"}}]}}

Rules:
- Return one repair per item only when an exact supporting span exists; otherwise omit it.
- Copy characters directly. Never join non-adjacent cells or clauses and never use ellipsis.
- Keep the supplied item_id and chunk_id unchanged.

ITEMS:
{chr(10).join(rendered)}"""
