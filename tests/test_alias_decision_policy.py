from valuechain.alias_decision_policy import decide_alias_resolutions


def candidate(rank=1, lei="L1", confidence=.98, similarity=.99):
    return {"query_object": "Acme Inc.", "candidate_rank": rank, "lei": lei, "canonical_name": "Acme Inc.", "resolver_confidence": confidence, "name_similarity": similarity, "entity_status": "ACTIVE", "registration_status": "ISSUED"}


def selection(decision="select", rank=1, confidence=.9):
    assessments = [] if decision == "no_match" else [{"candidate_rank": rank, "assessment": "MATCH", "confidence": "HIGH", "reason": "Exact legal name.", "used_evidence": ["GLEIF legal name"]}]
    return {"query_object": "Acme Inc.", "decision": decision, "selected_candidate_rank": rank, "llm_confidence": confidence, "llm_reason": "Exact legal name.", "candidate_assessments": assessments}


def test_strong_unconflicted_selection_is_auto_accepted():
    assert decide_alias_resolutions([candidate()], [selection()])[0]["decision"] == "AUTO_ACCEPT"


def test_no_match_stays_unresolved():
    assert decide_alias_resolutions([candidate()], [selection("no_match", 0)])[0]["decision"] == "KEEP_UNRESOLVED"


def test_conflicting_or_uncertain_matches_need_human_review():
    rows = decide_alias_resolutions([candidate(), candidate(2, "L2", .93, .95)], [selection()])
    assert rows[0]["decision"] == "REVIEW"
