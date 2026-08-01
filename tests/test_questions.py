import time

import pytest

from profile_os.dynstores import DynamicStores
from profile_os.errors import DynStoreConflict, SchemaError
from profile_os.questions import (INITIAL_WEIGHT, QUESTION_PROFILE, QuestionPractice,
                                  load_question_files, question_record)
from profile_os.storage import Store


def _raw_question(number=1, correct=(0,), option_count=4, explanations=True):
    options = []
    for index in range(option_count):
        options.append({
            "text": f"Option {index + 1}",
            "correct": index in correct,
            "explanation": f"Explanation for option {index + 1}." if explanations else None,
        })
    prompt = "Which TWO choices apply?" if len(correct) == 2 else "Which choice applies?"
    overall = "Full explanation. The answer is established."
    if not explanations:
        correct_text = options[correct[0]]["text"]
        wrong = [option for index, option in enumerate(options) if index not in correct]
        overall = (f"Correct option:{correct_text} Correct rationale explains why this"
                   " option is right. Incorrect options:")
        overall += "".join(f"{option['text']} Why this option is wrong." for option in wrong)
        overall += "References:https://example.test"
    return {
        "question_number": number, "status": "Skipped", "prompt": prompt,
        "options": options, "overall_explanation": overall, "domain": "Tools",
    }


@pytest.fixture
def practice(tmp_path):
    store = Store(tmp_path / "data")
    store.create_profile(
        QUESTION_PROFILE, "LT Rita", "identity", "role", family_id="rita",
        variant_label="LT", is_family_default=False)
    service = QuestionPractice(store, DynamicStores(store))
    yield service
    store.close()


def test_question_identity_is_exam_and_option_order_independent():
    first = _raw_question(number=1, correct=(0,))
    second = _raw_question(number=42, correct=(0,))
    second["options"] = list(reversed(second["options"]))
    a = question_record(first, "sundog1")
    b = question_record(second, "sundog2")
    assert a["question_key"] == b["question_key"]
    assert a["extracted_correct_option_ids"] == b["extracted_correct_option_ids"]


def test_marrek_explanations_are_derived_and_text_boundaries_repaired():
    data = question_record(_raw_question(explanations=False), "marrek1")
    assert data["option_b_explanation"] == "Why this option is wrong."
    assert "Incorrect options" not in data["option_a_explanation"]
    assert "Correct option: Option 1" in data["overall_explanation"]
    assert "References: https://example.test" in data["overall_explanation"]


def test_import_draw_grade_and_revise_recomputes_learning_state(practice):
    imported = practice.import_records([question_record(_raw_question(), "sundog1")])
    assert imported == {"created": 1, "updated": 0, "total": 1}
    draw = practice.draw("lt_rita", {"domain": {"contains": "tool"}})
    assert draw["expires_at"] - time.time() == pytest.approx(24 * 60 * 60, abs=2)
    assert "### Question 1 of 1" in draw["markdown"]
    wrong = practice.grade("LT Rita", draw["attempt_code"],
                           [{"position": 1, "selected": ["B"]}])
    assert wrong["results"][0]["status"] == "wrong"
    assert "Why B is wrong" in wrong["markdown"]
    assert "Full explanation" in wrong["markdown"]

    revised = practice.revise_answer(
        "lt_rita", draw["attempt_code"], 1, "override", ["B"], "source key is wrong")
    assert revised["answer_status"] == "overridden"
    assert revised["correct_count"] == 1
    assert revised["wrong_count"] == 0
    assert revised["weight"] == INITIAL_WEIGHT / 2


def test_correct_grade_returns_ok_and_halves_weight(practice):
    practice.import_records([question_record(_raw_question(), "sundog1")])
    draw = practice.draw("lt_rita")
    result = practice.grade("lt_rita", draw["attempt_code"],
                            [{"position": 1, "selected": ["A"]}])
    assert result["markdown"] == "OK"
    record = practice._dyn.query_records("lt_rita", "exam_questions")[0]
    assert record["data"]["correct_count"] == 1
    assert record["data"]["wrong_count"] == 0
    assert record["data"]["weight"] == 2


def test_nullified_question_is_not_drawn(practice):
    practice.import_records([question_record(_raw_question(), "sundog1")])
    draw = practice.draw("lt_rita")
    practice.revise_answer("lt_rita", draw["attempt_code"], 1,
                           "nullify", None, "broken question")
    with pytest.raises(SchemaError, match="not enough eligible"):
        practice.draw("lt_rita")


def test_unanswered_attempt_expires_and_cannot_be_replayed(practice):
    practice.import_records([question_record(_raw_question(), "sundog1")])
    draw = practice.draw("lt_rita")
    with practice.db:
        practice.db.execute("UPDATE question_attempts SET expires_at=? WHERE code=?",
                            (time.time() - 1, draw["attempt_code"]))
    with pytest.raises(DynStoreConflict, match="expired after 24 hours"):
        practice.grade("lt_rita", draw["attempt_code"],
                       [{"position": 1, "selected": ["A"]}])


def test_question_tools_reject_other_companions(practice):
    with pytest.raises(SchemaError, match="only for lt_rita"):
        practice.draw("rita")
