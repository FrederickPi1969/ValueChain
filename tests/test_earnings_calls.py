from valuechain.earnings_calls import EarningsCallSource, assess_source, public_excerpt_limit


def test_issuer_ir_source_is_allowed_but_not_redistributed() -> None:
    source = EarningsCallSource(
        issuer="NVIDIA",
        source_url="https://investor.nvidia.com/events-and-presentations/default.aspx",
        provider="issuer_ir_release",
        domain_kind="issuer_ir",
    )
    assert assess_source(source) == (True, "first_party_source")
    assert public_excerpt_limit(source) == 280


def test_unapproved_transcript_source_is_rejected() -> None:
    source = EarningsCallSource(
        issuer="NVIDIA",
        source_url="https://example.invalid/transcript",
        provider="unknown",
        domain_kind="third_party",
    )
    assert assess_source(source) == (False, "unapproved_source_kind")
