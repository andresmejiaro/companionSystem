import json
import re

import pytest

from profile_os.extract_udemy_questions import ExtractionError, extract_questions
from profile_os.questions import load_question_files


def _answer(text, explanation, *, correct=False, learner_correct=False):
    label = ""
    if correct:
        value = "Your answer is correct" if learner_correct else "Correct answer"
        label = f'<span data-purpose="answer-result-header-user-label">{value}</span>'
    return f"""
    <section>
      <div data-purpose="answer">{label}<div data-purpose="answer-body">
        <div id="answer-text">{text}</div></div></div>
      <aside><h4>Explanation</h4><div id="question-explanation">{explanation}</div></aside>
    </section>"""


def _question(number, status="Skipped", correct=1):
    answers = "".join(
        _answer(
            f"Option {letter}",
            f"Why option {letter} is {'right' if index == correct else 'wrong'}.",
            correct=index == correct,
            learner_correct=status == "Correct" and index == correct,
        )
        for index, letter in enumerate("ABCD")
    )
    return f"""
    <article>
      <header><span>Question {number}</span>
        <span data-purpose="question-result-header-status-label">{status}</span>
        <div id="question-prompt"><p>Choose the valid item {number}.</p><ul><li>Keep this detail</li></ul></div>
      </header>
      <div>{answers}</div>
      <div id="overall-explanation"><p>Overall rationale {number}.</p></div>
      <div data-purpose="domain-pane"><div>Domain</div><div>Architecture</div></div>
    </article>"""


def test_extracts_semantic_result_markup_and_round_trips(tmp_path):
    questions = extract_questions(f"<html><body>{_question(1, 'Correct', 0)}{_question(2)}</body></html>")

    assert len(questions) == 2
    assert questions[0]["status"] == "Correct"
    assert questions[0]["prompt"] == "Choose the valid item 1.\n- Keep this detail"
    assert questions[0]["options"][0] == {
        "text": "Option A", "correct": True, "explanation": "Why option A is right."
    }
    assert questions[1]["options"][1]["correct"] is True
    assert questions[1]["overall_explanation"] == "Overall rationale 2."
    assert questions[1]["domain"] == "Architecture"

    path = tmp_path / "bank.json"
    path.write_text(json.dumps(questions), encoding="utf-8")
    assert len(load_question_files([path], exam="CCAR-F")) == 2


def test_rejects_collapsed_or_incomplete_page():
    html = """
    <article><span>Question 1</span>
      <span data-purpose="question-result-header-status-label">Skipped</span>
    </article>
    """
    with pytest.raises(ExtractionError, match="collapsed or incomplete"):
        extract_questions(html)


def test_rejects_missing_answer_key():
    html = _question(1).replace("Correct answer", "Not marked")
    with pytest.raises(ExtractionError, match="marked correct"):
        extract_questions(html)


def test_accepts_udemy_multi_select_correct_selection_label():
    html = _question(1, correct=0).replace(
        "Correct answer", "Correct selection", 1
    ).replace(
        '<div data-purpose="answer"><div data-purpose="answer-body">',
        '<div data-purpose="answer"><span data-purpose="answer-result-header-user-label">'
        'Correct selection</span><div data-purpose="answer-body">',
        1,
    ).replace("Choose the valid item 1.", "Which TWO items are valid?")

    question = extract_questions(html)[0]

    assert [option["correct"] for option in question["options"]] == [True, True, False, False]


def test_allows_overall_only_feedback_and_loader_derives_explanations(tmp_path):
    html = _question(1, correct=0)
    html = re.sub(r"<aside>.*?</aside>", "", html, flags=re.S)
    overall = (
        "Correct option: Option A Correct rationale is sufficiently detailed here. "
        "Incorrect options: Option B Wrong rationale for B is sufficiently detailed. "
        "Option C Wrong rationale for C is sufficiently detailed. "
        "Option D Wrong rationale for D is sufficiently detailed."
    )
    html = html.replace("<p>Overall rationale 1.</p>", overall)

    questions = extract_questions(html)

    assert all(option["explanation"] is None for option in questions[0]["options"])
    path = tmp_path / "overall-only.json"
    path.write_text(json.dumps(questions), encoding="utf-8")
    record = load_question_files([path], exam="CCAR-F")[0]
    assert record["option_b_explanation"].startswith("Wrong rationale for B")
