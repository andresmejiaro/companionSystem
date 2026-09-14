"""Extract Udemy practice-test result HTML into FableCompanion question JSON.

Usage:
    python -m profile_os.extract_udemy_questions INPUT_HTML OUTPUT_JSON

The input must be a fully expanded saved result page. Output is accepted by
``profile_os.questions.load_question_files``.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


class ExtractionError(ValueError):
    """Raised when a page cannot be extracted completely and unambiguously."""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["_Node | str"] = field(default_factory=list)
    parent: "_Node | None" = None


class _TreeParser(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def _nodes(node: _Node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _nodes(child)


def _matches(node: _Node, key: str, value: str) -> bool:
    return node.attrs.get(key) == value


def _find_all(node: _Node, key: str, value: str) -> list[_Node]:
    return [candidate for candidate in _nodes(node) if _matches(candidate, key, value)]


def _plain_text(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        parts.append(_plain_text(child) if isinstance(child, _Node) else child)
    return " ".join("".join(parts).split())


def _rich_text(node: _Node) -> str:
    """Render common rich-text blocks readably without retaining unsafe HTML."""
    parts: list[str] = []

    def visit(current: _Node, list_depth: int = 0) -> None:
        if current.tag == "br":
            parts.append("\n")
            return
        if current.tag == "li":
            parts.append("\n" + "  " * list_depth + "- ")
        elif current.tag in {"p", "div", "pre", "blockquote", "h1", "h2", "h3", "h4"}:
            parts.append("\n")
        for child in current.children:
            if isinstance(child, str):
                parts.append(html.unescape(child))
            else:
                visit(child, list_depth + (1 if current.tag in {"ul", "ol"} else 0))
        if current.tag in {"p", "div", "pre", "blockquote", "li", "h1", "h2", "h3", "h4"}:
            parts.append("\n")

    visit(node)
    lines = [" ".join(line.split()) for line in "".join(parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _one(node: _Node, key: str, value: str, context: str) -> _Node:
    matches = _find_all(node, key, value)
    if len(matches) != 1:
        raise ExtractionError(f"{context}: expected one [{key}={value!r}], found {len(matches)}")
    return matches[0]


def _question_root(status_node: _Node) -> _Node | None:
    current = status_node.parent
    while current is not None:
        prompts = _find_all(current, "id", "question-prompt")
        domains = _find_all(current, "data-purpose", "domain-pane")
        if len(prompts) == 1 and len(domains) == 1:
            return current
        current = current.parent
    return None


def _option_root(answer: _Node) -> _Node | None:
    current = answer.parent
    while current is not None:
        answers = _find_all(current, "data-purpose", "answer")
        explanations = _find_all(current, "id", "question-explanation")
        if len(answers) == 1 and len(explanations) == 1:
            return current
        if len(answers) > 1:
            return None
        current = current.parent
    return None


def extract_questions(source: str) -> list[dict]:
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    status_nodes = _find_all(parser.root, "data-purpose", "question-result-header-status-label")
    if not status_nodes:
        raise ExtractionError("no question result headers found; save an expanded results page")

    questions: list[dict] = []
    seen_roots: set[int] = set()
    for status_node in status_nodes:
        root = _question_root(status_node)
        if root is None:
            raise ExtractionError("question result is collapsed or incomplete")
        if id(root) in seen_roots:
            raise ExtractionError("ambiguous duplicate status labels in one question")
        seen_roots.add(id(root))
        header_text = _plain_text(status_node.parent or status_node)
        number_match = re.search(r"\bQuestion\s+(\d+)(?!\d)", header_text, re.IGNORECASE)
        if not number_match:
            raise ExtractionError("question header is missing its number")
        number = int(number_match.group(1))
        context = f"question {number}"
        prompt = _rich_text(_one(root, "id", "question-prompt", context))

        answers = _find_all(root, "data-purpose", "answer")
        feedback = _find_all(root, "id", "question-explanation")
        if feedback and len(feedback) != len(answers):
            raise ExtractionError(f"{context}: option feedback is incomplete")
        options: list[dict] = []
        for answer in answers:
            explanation = None
            if feedback:
                option_root = _option_root(answer)
                if option_root is None:
                    raise ExtractionError(f"{context}: option feedback is ambiguous")
                explanation = _rich_text(
                    _one(option_root, "id", "question-explanation", context))
            labels = [_plain_text(item).casefold() for item in
                      _find_all(answer, "data-purpose", "answer-result-header-user-label")]
            correct_labels = {
                "correct answer", "correct selection", "your answer is correct",
            }
            correct = any(label in correct_labels for label in labels)
            options.append({
                "text": _rich_text(_one(answer, "id", "answer-text", context)),
                "correct": correct,
                "explanation": explanation,
            })
        if not 4 <= len(options) <= 5:
            raise ExtractionError(f"{context}: expected 4 or 5 options, found {len(options)}")
        if sum(option["correct"] for option in options) not in {1, 2}:
            raise ExtractionError(f"{context}: expected one or two marked correct options")

        domain_node = _one(root, "data-purpose", "domain-pane", context)
        domain = _rich_text(domain_node)
        domain = re.sub(r"^Domain\s*", "", domain, count=1, flags=re.IGNORECASE).strip()
        overall = _rich_text(_one(root, "id", "overall-explanation", context))
        if not prompt or not domain or not overall or any(not option["text"] for option in options):
            raise ExtractionError(f"{context}: required content is empty")
        if feedback and any(not option["explanation"] for option in options):
            raise ExtractionError(f"{context}: required content is empty")
        questions.append({
            "question_number": number,
            "status": _plain_text(status_node),
            "prompt": prompt,
            "options": options,
            "overall_explanation": overall,
            "domain": domain,
        })

    numbers = [question["question_number"] for question in questions]
    if len(numbers) != len(set(numbers)):
        raise ExtractionError("duplicate question numbers")
    expected = list(range(1, len(numbers) + 1))
    if sorted(numbers) != expected:
        raise ExtractionError(f"question numbers must be contiguous 1..{len(numbers)}")
    return sorted(questions, key=lambda question: question["question_number"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_html", type=Path)
    parser.add_argument("output_json", type=Path)
    args = parser.parse_args()
    questions = extract_questions(args.input_html.read_text(encoding="utf-8"))
    args.output_json.write_text(
        json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Extracted {len(questions)} questions to {args.output_json}")


if __name__ == "__main__":
    main()
