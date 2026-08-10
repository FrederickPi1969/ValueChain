from __future__ import annotations

import re
import unicodedata

from valuechain.financial_ie.multilingual.types import LanguagePack


LANGUAGE_ALIASES = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "cmn": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant": "zh-Hant",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "ko": "ko",
    "kr": "ko",
    "kor": "ko",
}


PACKS: dict[str, LanguagePack] = {
    "zh-Hans": LanguagePack(
        code="zh-Hans",
        native_name="简体中文",
        profile_queries=(
            "公司主营业务 主要产品 服务 客户 市场 业务模式",
            "行业地位 核心竞争力 产业链 经营范围",
        ),
        signal_queries=(
            ("demand_and_revenue", "收入 营业收入 销量 订单 在手订单 需求 增长 下降"),
            ("pricing_and_margin", "价格 毛利率 利润率 成本 原材料 涨价 降价"),
            ("capital_allocation", "资本开支 投资 扩产 回购 分红 融资"),
            ("capacity_and_supply", "产能 产量 供应 短缺 生产基地 库存 原材料"),
            ("customer_concentration", "前五大客户 主要客户 客户集中度 销售占比"),
            ("supplier_or_infrastructure_dependency", "供应商 采购 依赖 单一来源 云服务 数据中心 电力"),
            ("regulatory_or_geopolitical", "监管 出口管制 关税 制裁 地缘政治 政策"),
            ("technology_and_product", "研发 新产品 技术 工艺 专利 产业化"),
            ("partnership_or_mna", "合作 战略协议 收购 并购 合资 合营"),
            ("liquidity_and_balance_sheet", "现金流 债务 负债 流动性 授信 偿债"),
        ),
        section_cues=(
            ("business", ("公司业务概要", "主营业务", "业务与产品", "经营范围")),
            ("mdna", ("经营情况讨论与分析", "管理层讨论与分析", "经营情况")),
            ("risk", ("风险因素", "可能面对的风险", "重大风险提示")),
            ("supply_chain", ("主要客户", "主要供应商", "采购情况", "生产与采购")),
            ("research", ("研发投入", "研究开发", "核心技术")),
        ),
        hypothetical_markers=("可能", "若", "如果", "风险", "无法保证", "不排除"),
        forward_markers=("预计", "计划", "拟", "将", "未来", "目标"),
    ),
    "zh-Hant": LanguagePack(
        code="zh-Hant",
        native_name="繁體中文",
        profile_queries=(
            "公司主要業務 產品 服務 客戶 市場 營運模式",
            "產業地位 核心競爭力 供應鏈 經營範圍",
        ),
        signal_queries=(
            ("demand_and_revenue", "營業收入 營收 銷量 訂單 需求 成長 衰退"),
            ("pricing_and_margin", "價格 毛利率 利潤率 成本 原物料"),
            ("capital_allocation", "資本支出 投資 擴產 庫藏股 股利 融資"),
            ("capacity_and_supply", "產能 產量 供應 短缺 生產基地 存貨 原料"),
            ("customer_concentration", "主要客戶 客戶集中度 銷售占比"),
            ("supplier_or_infrastructure_dependency", "供應商 採購 依賴 單一來源 雲端 資料中心 電力"),
            ("regulatory_or_geopolitical", "監管 出口管制 關稅 制裁 地緣政治 政策"),
            ("technology_and_product", "研發 新產品 技術 製程 專利"),
            ("partnership_or_mna", "合作 策略協議 收購 併購 合資 投資"),
            ("liquidity_and_balance_sheet", "現金流 負債 流動性 授信 償債"),
        ),
        section_cues=(
            ("business", ("主要業務", "營運概況", "業務內容", "經營範圍")),
            ("mdna", ("營運情形", "財務業務", "經營績效")),
            ("risk", ("風險因素", "重大風險", "風險管理")),
            ("supply_chain", ("主要客戶", "主要供應商", "採購情形")),
            ("material_event", ("重大訊息", "主旨", "說明")),
        ),
        hypothetical_markers=("可能", "若", "如果", "風險", "無法保證", "恐"),
        forward_markers=("預計", "計畫", "擬", "將", "未來", "目標"),
    ),
    "ja": LanguagePack(
        code="ja",
        native_name="日本語",
        profile_queries=(
            "事業の内容 主要な製品 サービス 顧客 市場 事業セグメント",
            "経営方針 競争力 事業環境 バリューチェーン",
        ),
        signal_queries=(
            ("demand_and_revenue", "売上高 受注 受注残 需要 増加 減少 販売数量"),
            ("pricing_and_margin", "価格 売上総利益 利益率 原価 原材料 コスト"),
            ("capital_allocation", "設備投資 投資 配当 自己株式 資金調達"),
            ("capacity_and_supply", "生産能力 供給不足 生産拠点 在庫 原材料 調達"),
            ("customer_concentration", "主要顧客 顧客集中 売上高 割合"),
            ("supplier_or_infrastructure_dependency", "仕入先 供給者 依存 単一調達 クラウド データセンター 電力"),
            ("regulatory_or_geopolitical", "規制 輸出管理 関税 制裁 地政学 政策"),
            ("technology_and_product", "研究開発 新製品 技術 製造プロセス 特許"),
            ("partnership_or_mna", "提携 協業 買収 合併 合弁 ライセンス"),
            ("liquidity_and_balance_sheet", "キャッシュフロー 有利子負債 流動性 借入金 財務制限条項"),
        ),
        section_cues=(
            ("business", ("事業の内容", "事業の概況", "主要な事業", "企業の概況")),
            ("mdna", ("経営者による財政状態", "経営成績等の状況", "経営方針")),
            ("risk", ("事業等のリスク", "リスク情報", "リスク管理")),
            ("supply_chain", ("主要な顧客", "主要な仕入先", "生産、受注及び販売")),
            ("research", ("研究開発活動", "研究開発")),
        ),
        hypothetical_markers=("可能性", "おそれ", "場合", "リスク", "懸念", "保証でき"),
        forward_markers=("見込", "予定", "計画", "将来", "目標", "方針"),
    ),
    "ko": LanguagePack(
        code="ko",
        native_name="한국어",
        profile_queries=(
            "사업의 내용 주요 제품 서비스 고객 시장 사업 부문",
            "사업 개요 경쟁력 산업 가치사슬 영업 현황",
        ),
        signal_queries=(
            ("demand_and_revenue", "매출 매출액 수주 수주잔고 수요 증가 감소 판매량"),
            ("pricing_and_margin", "가격 매출총이익 이익률 원가 원재료 비용"),
            ("capital_allocation", "설비투자 투자 배당 자기주식 자금조달"),
            ("capacity_and_supply", "생산능력 공급 부족 생산시설 재고 원재료 조달"),
            ("customer_concentration", "주요 고객 고객 집중 매출 비중"),
            ("supplier_or_infrastructure_dependency", "공급업체 매입처 의존 단일 공급 클라우드 데이터센터 전력"),
            ("regulatory_or_geopolitical", "규제 수출 통제 관세 제재 지정학 정책"),
            ("technology_and_product", "연구개발 신제품 기술 공정 특허"),
            ("partnership_or_mna", "전략적 제휴 협력 인수 합병 합작 라이선스"),
            ("liquidity_and_balance_sheet", "현금흐름 차입금 부채 유동성 신용한도 재무약정"),
        ),
        section_cues=(
            ("business", ("사업의 내용", "사업의 개요", "주요 제품 및 서비스")),
            ("mdna", ("영업의 개황", "경영진단 및 분석", "매출 및 수주상황")),
            ("risk", ("위험관리", "위험 요인", "리스크")),
            ("supply_chain", ("원재료 및 생산설비", "주요 매입처", "주요 고객")),
            ("research", ("연구개발활동", "연구개발")),
        ),
        hypothetical_markers=("가능성", "위험", "경우", "우려", "보장할 수 없"),
        forward_markers=("예상", "계획", "예정", "향후", "목표", "전망"),
    ),
}


SIMPLIFIED_ONLY = set("这为发会业东与门开关长产现资动务应实华国证万亿")
TRADITIONAL_ONLY = set("這為發會業東與門開關長產現資動務應實華國證萬億")


def canonical_language(value: str, text: str = "") -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[normalized]
    if re.search(r"[\uac00-\ud7a3]", text):
        return "ko"
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    traditional = sum(character in TRADITIONAL_ONLY for character in text)
    simplified = sum(character in SIMPLIFIED_ONLY for character in text)
    if traditional or simplified:
        return "zh-Hant" if traditional > simplified else "zh-Hans"
    raise ValueError(f"Unsupported or undetectable language: {value!r}")


def get_language_pack(value: str, text: str = "") -> LanguagePack:
    return PACKS[canonical_language(value, text)]


def normalize_unicode(value: str) -> str:
    value = unicodedata.normalize("NFKC", value.replace("\x00", " "))
    value = re.sub(r"[\t\r ]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def infer_section(text: str, pack: LanguagePack) -> str:
    probe = normalize_unicode(text[:800]).casefold()
    for section, cues in pack.section_cues:
        if any(cue.casefold() in probe for cue in cues):
            return section
    return ""


def native_script_ratio(text: str, language: str) -> float:
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    if language in {"zh-Hans", "zh-Hant"}:
        native = sum("\u3400" <= character <= "\u9fff" for character in letters)
    elif language == "ja":
        native = sum(
            "\u3040" <= character <= "\u30ff" or "\u3400" <= character <= "\u9fff"
            for character in letters
        )
    elif language == "ko":
        native = sum("\uac00" <= character <= "\ud7a3" for character in letters)
    else:
        native = 0
    return round(native / len(letters), 4)
