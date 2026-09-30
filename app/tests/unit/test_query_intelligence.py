from app.modules.search.query_intelligence import query_variants


def test_variants_only_normalise_known_terms_without_facts():
    variants = query_variants("Can a probation employee get a housing loan")
    assert variants[0] == "Can a probation employee get a housing loan"
    assert any("probationary employment" in value for value in variants)
    assert any("home loan" in value for value in variants)


def test_identifiers_and_clauses_are_never_expanded():
    assert query_variants("What does section 5.2 of CR-2025-018 say") == ["What does section 5.2 of CR-2025-018 say"]
