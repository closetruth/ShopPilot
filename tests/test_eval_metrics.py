"""r20 Task 8：确定性指标。不调模型、不连库。"""

from app.evals import metrics


def test_extract_citations_dedup_and_order():
    text = "看 [P-RET-001-3]，还有 [P-SHIP-002] 与 [P-RET-001-3]"
    assert metrics.extract_citations(text) == ["P-RET-001-3", "P-SHIP-002"]


def test_doc_id_of():
    assert metrics.doc_id_of("P-RET-001-3") == "P-RET-001"
    assert metrics.doc_id_of("P-RET-001") == "P-RET-001"


def test_doc_hit_passes_when_expected_doc_cited():
    case = {
        "expected_intent": "knowledge",
        "expected_doc_ids": ["P-RET-001"],
        "expected_behavior": "answer",
    }
    out = metrics.score_case(
        case,
        intent="knowledge",
        answer="运费由买家承担 [P-RET-001-3]",
        insufficient=False,
        faq_lookup=lambda fid: {"id": fid},
    )
    assert out["passed"] is True
    assert out["checks"] == {"intent": True, "doc_hit": True, "citation_valid": True}


def test_fabricated_citation_fails():
    case = {"expected_doc_ids": ["P-RET-001"], "expected_behavior": "answer"}
    out = metrics.score_case(
        case,
        intent="knowledge",
        answer="[P-FAKE-999-1]",
        insufficient=False,
        faq_lookup=lambda fid: None,
    )
    assert out["checks"]["citation_valid"] is False
    assert out["passed"] is False


def test_refusal_case_requires_no_citation():
    case = {"expected_behavior": "refuse"}
    out = metrics.score_case(
        case,
        intent="knowledge",
        answer="这个问题我查不到",
        insufficient=True,
        faq_lookup=lambda fid: None,
    )
    assert out["checks"] == {"refusal": True, "no_false_citation": True}
    assert out["passed"] is True


def test_aggregate_handles_empty():
    assert metrics.aggregate([])["pass_rate"] == 0.0
    assert metrics.aggregate([])["total"] == 0