from valuechain.mention_constrained_extraction import MentionConstrainedExtractor
from valuechain.models import EntityMention, Passage, RelationEvidence


def passage() -> Passage:
    return Passage("p", "NVDA", "1045810", "NVIDIA Corporation", "10-K", "a", "2026-01-01", "", "", "Business", 1, "We purchase memory from Samsung.", "test", "1")


def record(obj: str) -> RelationEvidence:
    p = passage()
    return RelationEvidence(p.company_name, obj, "supplier_dependency", "subject_depends_on_object", "current_fact", "high", "as_disclosed", p.text, .8, "test", p.ticker, p.cik, p.form, p.filing_date, p.accepted_timestamp, p.accession_number, p.source_document_url, p.section, p.passage_id, p.paragraph_offset, p.parser_name, p.parser_version)


class StaticExtractor:
    def __init__(self, records): self.records = records
    def extract(self, _passage): return self.records


def test_named_object_must_be_mentioned_and_alias_is_normalized():
    samsung = EntityMention("Samsung", "company", "Samsung Electronics Co., Ltd", mention_kind="named_entity", passage_id="p")
    extractor = MentionConstrainedExtractor(StaticExtractor([record("Samsung"), record("Alphabet Inc.")]), {"p": [samsung]})
    rows = extractor.extract(passage())
    assert [row.object for row in rows] == ["Samsung Electronics Co., Ltd"]
    assert extractor.diagnostics[0]["action"] == "drop_unmentioned_named_object"


def test_generic_dependency_object_is_retained_without_named_mention():
    extractor = MentionConstrainedExtractor(StaticExtractor([record("limited number of suppliers")]), {"p": []})
    assert extractor.extract(passage())[0].object == "limited number of suppliers"


def test_exhibit_footnote_marker_matches_the_underlying_legal_name():
    entity = EntityMention("Xilinx Development Corporation", "organization", "Xilinx Development Corporation", mention_kind="named_entity", passage_id="p")
    extractor = MentionConstrainedExtractor(StaticExtractor([record("Xilinx Development Corporation (1")]), {"p": [entity]})
    assert extractor.extract(passage())[0].object == "Xilinx Development Corporation"
