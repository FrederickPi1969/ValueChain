from __future__ import annotations

import re

from valuechain.models import Passage


KEYWORD_WEIGHTS: dict[str, float] = {
    "rely on": 2.5,
    "depend on": 2.5,
    "dependent on": 2.5,
    "third-party": 1.8,
    "third party": 1.8,
    "supplier": 2.0,
    "sole source": 3.0,
    "limited number": 2.5,
    "foundry": 2.8,
    "wafer": 2.2,
    "advanced packaging": 2.8,
    "assembly": 1.3,
    "contract manufacturer": 2.5,
    "cloud": 2.0,
    "hosting": 1.8,
    "data center": 2.4,
    "colocation": 2.2,
    "power": 1.8,
    "electricity": 2.2,
    "cooling": 1.8,
    "network": 1.5,
    "interconnection": 2.0,
    "capacity": 1.5,
    "customer concentration": 3.0,
    "material agreement": 2.6,
    "master services agreement": 3.0,
    "services agreement": 2.4,
    "supply agreement": 3.0,
    "purchase agreement": 2.6,
    "reseller agreement": 2.6,
    "distribution agreement": 2.6,
    "cloud services agreement": 2.8,
    "colocation agreement": 2.8,
    "capacity agreement": 2.8,
    "data center lease": 2.8,
    "lease agreement": 2.2,
    "power purchase agreement": 3.0,
    "interconnection agreement": 2.4,
    "strategic partnership": 2.6,
    "strategic collaboration agreement": 3.0,
    "joint investment": 2.6,
    "license": 1.6,
    "license agreement": 2.6,
    "subsidiary": 2.0,
    "subsidiaries": 2.2,
    "wholly owned": 2.2,
    "export controls": 1.8,
}

SECTION_PRIORITY: dict[str, float] = {
    "item_1_business": 0.5,
    "item_1a_risk_factors": 1.0,
    "item_7_mdna": 0.4,
    "part_i_item_2_mdna": 0.4,
    "part_ii_item_1a_risk_factors": 1.0,
    "item_1_01_material_agreement": 1.0,
    "item_2_02_results": 0.4,
    "item_7_01_reg_fd": 0.5,
    "item_8_01_other_events": 0.5,
    "item_9_01_exhibits": 0.5,
    "item_3d_risk_factors": 1.0,
    "item_4_company_information": 0.5,
    "item_5_operating_review": 0.4,
    "foreign_report_full_text": 0.6,
    "event_report_full_text": 0.6,
    "exhibit_10_material_contract": 1.4,
    "exhibit_21_subsidiaries": 2.0,
    "exhibit_99_1_investor_or_earnings": 0.8,
    "exhibit_99_investor_or_event_material": 0.8,
}


def score_passage(passage: Passage) -> Passage:
    text = passage.text.lower()
    terms: list[str] = []
    score = SECTION_PRIORITY.get(passage.section, 0.0)
    for term, weight in KEYWORD_WEIGHTS.items():
        if re.search(rf"\b{re.escape(term)}\b", text):
            terms.append(term)
            score += weight
    if "risk" in passage.section:
        score += 0.4
    passage.relevance_score = round(score, 3)
    passage.relevance_terms = terms
    return passage


def filter_candidates(passages: list[Passage], min_score: float = 2.0) -> list[Passage]:
    scored = [score_passage(passage) for passage in passages]
    return [passage for passage in scored if passage.relevance_score >= min_score]
