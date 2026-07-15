from valuechain.financial_ie.multilingual.languages import (
    canonical_language,
    get_language_pack,
    infer_section,
    native_script_ratio,
)


def test_language_aliases_keep_simplified_and_traditional_distinct() -> None:
    assert canonical_language("zh") == "zh-Hans"
    assert canonical_language("zh-TW") == "zh-Hant"
    assert canonical_language("ja") == "ja"
    assert canonical_language("ko") == "ko"


def test_language_detection_uses_native_scripts_when_metadata_is_absent() -> None:
    assert canonical_language("", "当社の事業の内容") == "ja"
    assert canonical_language("", "회사의 주요 사업 내용") == "ko"
    assert canonical_language("", "這是公司的營運資訊") == "zh-Hant"


def test_localized_section_inference() -> None:
    assert infer_section("第五节 主要客户与主要供应商", get_language_pack("zh")) == "supply_chain"
    assert infer_section("３【事業等のリスク】", get_language_pack("ja")) == "risk"
    assert infer_section("3. 원재료 및 생산설비", get_language_pack("ko")) == "supply_chain"


def test_native_script_ratio_is_language_specific() -> None:
    assert native_script_ratio("主要產品與服務 cloud", "zh-Hant") > 0.5
    assert native_script_ratio("주요 제품과 서비스 cloud", "ko") > 0.5
    assert native_script_ratio("English only", "ja") == 0
