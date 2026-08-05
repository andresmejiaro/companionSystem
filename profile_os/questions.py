"""LT Rita's weighted exam-question practice service.

Question content lives in the ordinary approved ``exam_questions`` dynamic
store.  This module adds the small amount of domain behavior that generic
record queries should not own: stable question/option identities, weighted
draws, 24-hour attempt continuity, grading, and audited answer-key repair.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import unicodedata
import uuid
from pathlib import Path

from .dynstores import DynamicStores, validate_record
from .errors import DynStoreConflict, DynStoreNotFound, SchemaError
from .storage import Store, normalize_profile_lookup


QUESTION_PROFILE = "lt_rita"
QUESTION_STORE = "exam_questions"
INITIAL_WEIGHT = 4.0
MIN_WEIGHT = 0.25
MAX_WEIGHT = 64.0
ATTEMPT_TTL_SECONDS = 24 * 60 * 60
OPTION_LABELS = ("A", "B", "C", "D", "E")

QUESTION_SCHEMA = {"fields": {
    "question_key": {"type": "string"},
    "source_refs": {"type": "object_list"},
    "domain": {"type": "string"},
    "sub_skill": {"type": "string", "required": False},
    "prompt": {"type": "string"},
    "option_a_id": {"type": "string"},
    "option_a_text": {"type": "string"},
    "option_a_explanation": {"type": "string"},
    "option_b_id": {"type": "string"},
    "option_b_text": {"type": "string"},
    "option_b_explanation": {"type": "string"},
    "option_c_id": {"type": "string"},
    "option_c_text": {"type": "string"},
    "option_c_explanation": {"type": "string"},
    "option_d_id": {"type": "string"},
    "option_d_text": {"type": "string"},
    "option_d_explanation": {"type": "string"},
    "option_e_id": {"type": "string", "required": False},
    "option_e_text": {"type": "string", "required": False},
    "option_e_explanation": {"type": "string", "required": False},
    "extracted_correct_option_ids": {"type": "string_list"},
    "effective_correct_option_ids": {"type": "string_list"},
    "overall_explanation": {"type": "string"},
    "answer_status": {"type": "string"},
    "answer_revision_note": {"type": "string", "required": False},
    "answer_revision_count": {"type": "integer"},
    "weight": {"type": "number"},
    "correct_count": {"type": "integer"},
    "wrong_count": {"type": "integer"},
}}

# The first shipped bank had domains but not sub-skill tags.  This exact
# definition lets ``ensure_store`` recognize and migrate that known version
# without accepting arbitrary incompatible schemas.
LEGACY_QUESTION_SCHEMA = {"fields": {
    name: spec for name, spec in QUESTION_SCHEMA["fields"].items()
    if name != "sub_skill"
}}

QUESTION_SCHEMA_PURPOSE = (
    "Deduplicated exam-independent practice questions for LT Rita. Stable option"
    " identities support randomized presentation, weighted review, grading, and"
    " audited answer-key correction."
)

QUESTION_ATTEMPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS question_attempts (
    code TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    draw TEXT NOT NULL,
    answers TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    graded_at REAL
);
CREATE INDEX IF NOT EXISTS idx_question_attempts_expiry
ON question_attempts(profile_id, expires_at);
CREATE TABLE IF NOT EXISTS question_answer_audit (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    question_record_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_answer TEXT NOT NULL,
    after_answer TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempt_code TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_answer_audit_record
ON question_answer_audit(profile_id, question_record_id, created_at);
CREATE TABLE IF NOT EXISTS question_grade_audit (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    attempt_code TEXT NOT NULL,
    before_answers TEXT NOT NULL,
    after_answers TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_grade_audit_attempt
ON question_grade_audit(profile_id, attempt_code, created_at);
"""


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def repair_extracted_text(value: str) -> str:
    """Restore whitespace lost when adjacent source blocks were flattened."""
    value = " ".join(str(value).split())
    value = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", value)
    value = re.sub(
        r"(?<=[.!?])(?=(?:stop_reason|max_tokens|allowedTools|permissionMode)\b)",
        " ", value,
    )
    value = re.sub(r"\b(Correct option:|Incorrect options:|References:)(?=\S)", r"\1 ", value)
    return value.strip()


def _option_explanations(question: dict, overall: str) -> list[str]:
    supplied = [option.get("explanation") for option in question["options"]]
    if all(isinstance(item, str) and item.strip() for item in supplied):
        return [repair_extracted_text(item) for item in supplied]

    # Marrek keeps the rationale in one complete block.  Every option text is
    # repeated verbatim there, so the interval until the next option is the
    # option-specific rationale.  Refuse the import if that invariant breaks.
    positions: list[tuple[int, int, int]] = []
    for index, option in enumerate(question["options"]):
        text = repair_extracted_text(option["text"])
        start = overall.find(text)
        if start < 0:
            raise SchemaError(
                f"cannot derive explanation for source question {question.get('question_number')}"
                f" option {index + 1}")
        positions.append((start, start + len(text), index))
    positions.sort()
    explanations = [""] * len(supplied)
    for position, (_, text_end, original_index) in enumerate(positions):
        end = positions[position + 1][0] if position + 1 < len(positions) else len(overall)
        segment = overall[text_end:end]
        segment = re.sub(r"^(?:\s*Incorrect options:\s*)", "", segment)
        segment = re.sub(r"\s*Incorrect options:\s*$", "", segment)
        segment = re.sub(r"\s*References:.*$", "", segment)
        explanations[original_index] = segment.strip(" .") + "."
    if any(len(item) < 20 for item in explanations):
        raise SchemaError(
            f"derived explanation is incomplete for source question {question.get('question_number')}")
    return explanations


def question_record(question: dict, source_name: str) -> dict:
    """Convert one extracted question into a validated dynamic-store record."""
    prompt = repair_extracted_text(question.get("prompt", ""))
    overall = repair_extracted_text(question.get("overall_explanation", ""))
    options = question.get("options")
    if not prompt or not overall or not isinstance(options, list) or not 4 <= len(options) <= 5:
        raise SchemaError(f"malformed question {question.get('question_number')} in {source_name}")
    option_texts = [repair_extracted_text(option.get("text", "")) for option in options]
    if any(not text for text in option_texts) or len(set(option_texts)) != len(option_texts):
        raise SchemaError(f"missing or duplicate option in {source_name}")
    explanations = _option_explanations(question, overall)
    option_ids = [_stable_id("opt", _normalized(text)) for text in option_texts]
    correct = [option_ids[index] for index, option in enumerate(options)
               if option.get("correct") is True]
    requested_correct = 2 if re.search(r"(?i)\bwhich\s+(?:two|2)\b", prompt) else 1
    if len(correct) != requested_correct:
        raise SchemaError(
            f"answer count mismatch in {source_name} question {question.get('question_number')}")
    identity = json.dumps(
        {"prompt": _normalized(prompt), "options": sorted(_normalized(text) for text in option_texts)},
        sort_keys=True,
    )
    data = {
        "question_key": _stable_id("q", identity),
        "source_refs": [{"bank": source_name, "position": question.get("question_number")}],
        "domain": repair_extracted_text(question.get("domain", "")),
        "prompt": prompt,
        "extracted_correct_option_ids": correct,
        "effective_correct_option_ids": list(correct),
        "overall_explanation": overall,
        "answer_status": "active",
        "answer_revision_count": 0,
        "weight": INITIAL_WEIGHT,
        "correct_count": 0,
        "wrong_count": 0,
    }
    sub_skill = repair_extracted_text(question.get("sub_skill", ""))
    if sub_skill:
        data["sub_skill"] = sub_skill
    for index, label in enumerate(OPTION_LABELS[:len(options)]):
        prefix = f"option_{label.lower()}"
        data[f"{prefix}_id"] = option_ids[index]
        data[f"{prefix}_text"] = option_texts[index]
        data[f"{prefix}_explanation"] = explanations[index]
    validate_record(QUESTION_SCHEMA, data)
    return data


def load_question_files(paths: list[str | Path]) -> list[dict]:
    records: dict[str, dict] = {}
    for raw_path in paths:
        path = Path(raw_path)
        questions = json.loads(path.read_text())
        if not isinstance(questions, list):
            raise SchemaError(f"{path.name} must contain a JSON array")
        for raw in questions:
            data = question_record(raw, path.stem)
            existing = records.get(data["question_key"])
            if existing is None:
                records[data["question_key"]] = data
                continue
            if set(existing["extracted_correct_option_ids"]) != set(
                    data["extracted_correct_option_ids"]):
                existing["answer_status"] = "conflict"
            existing["source_refs"].extend(data["source_refs"])
    return list(records.values())


class QuestionPractice:
    def __init__(self, store: Store, dynstores: DynamicStores):
        self._store = store
        self._dyn = dynstores
        self.db.executescript(QUESTION_ATTEMPT_SCHEMA)

    @property
    def db(self):
        return self._store.db

    def _profile_id(self, companion_name: str) -> str:
        if not isinstance(companion_name, str) or not companion_name.strip():
            raise SchemaError("companion_name is required")
        resolution = self._store.resolve_profile(companion_name)
        if resolution["status"] != "resolved" or resolution["resolved_profile_id"] != QUESTION_PROFILE:
            raise SchemaError("exam-question tools are available only for lt_rita")
        return QUESTION_PROFILE

    def ensure_store(self, proposed_by: str = "operator") -> dict:
        try:
            definition = self._dyn.get(QUESTION_PROFILE, QUESTION_STORE)
        except DynStoreNotFound:
            definition = self._dyn.propose(
                QUESTION_PROFILE, QUESTION_STORE, QUESTION_SCHEMA_PURPOSE,
                proposed_by, QUESTION_SCHEMA)
            definition = self._dyn.approve(QUESTION_PROFILE, QUESTION_STORE, actor=proposed_by)
        if definition["schema"] == LEGACY_QUESTION_SCHEMA:
            # This is a system-owned store with a known additive migration.
            # Archive/re-propose preserves the generic store lifecycle and
            # makes v2 explicit; records are then validated by the v2 schema.
            self._dyn.archive(QUESTION_PROFILE, QUESTION_STORE, actor=proposed_by)
            definition = self._dyn.propose(
                QUESTION_PROFILE, QUESTION_STORE, QUESTION_SCHEMA_PURPOSE,
                proposed_by, QUESTION_SCHEMA)
            definition = self._dyn.approve(QUESTION_PROFILE, QUESTION_STORE,
                                           actor=proposed_by)
            with self.db:
                self.db.execute(
                    "UPDATE dynamic_records SET schema_version=?"
                    " WHERE profile_id=? AND store_name=?",
                    (definition["version"], QUESTION_PROFILE, QUESTION_STORE))
        elif definition["schema"] != QUESTION_SCHEMA:
            raise DynStoreConflict("exam_questions exists with an incompatible schema")
        if definition["status"] != "approved":
            raise DynStoreConflict("exam_questions is not approved")
        return definition

    def import_records(self, records: list[dict]) -> dict:
        definition = self.ensure_store()
        existing_rows = self.db.execute(
            "SELECT id, data FROM dynamic_records WHERE profile_id=? AND store_name=?",
            (QUESTION_PROFILE, QUESTION_STORE)).fetchall()
        existing = {json.loads(row["data"])["question_key"]: row for row in existing_rows}
        created = updated = 0
        with self.db:
            for data in records:
                validate_record(QUESTION_SCHEMA, data)
                row = existing.get(data["question_key"])
                if row is None:
                    self.db.execute(
                        "INSERT INTO dynamic_records (id, profile_id, store_name, schema_version,"
                        " data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), QUESTION_PROFILE, QUESTION_STORE,
                         definition["version"],
                         json.dumps(data), time.time(), None))
                    created += 1
                    continue
                current = json.loads(row["data"])
                # Re-import updates source content without erasing learning or
                # a human/companion answer-key correction.
                preserved = {key: current[key] for key in (
                    "effective_correct_option_ids", "answer_status", "answer_revision_note",
                    "answer_revision_count", "weight", "correct_count", "wrong_count")
                    if key in current}
                merged = {**data, **preserved}
                refs = {json.dumps(ref, sort_keys=True): ref
                        for ref in current.get("source_refs", []) + data["source_refs"]}
                merged["source_refs"] = list(refs.values())
                self.db.execute(
                    "UPDATE dynamic_records SET data=?, updated_at=? WHERE id=?",
                    (json.dumps(merged), time.time(), row["id"]))
                updated += 1
        return {"created": created, "updated": updated, "total": len(records)}

    @staticmethod
    def _options(data: dict) -> list[dict]:
        result = []
        for label in OPTION_LABELS:
            prefix = f"option_{label.lower()}"
            if f"{prefix}_id" not in data:
                continue
            result.append({
                "label": label,
                "id": data[f"{prefix}_id"],
                "text": data[f"{prefix}_text"],
                "explanation": data[f"{prefix}_explanation"],
            })
        return result

    def draw(self, companion_name: str, where: dict | None = None,
             count: int = 1) -> dict:
        profile_id = self._profile_id(companion_name)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
            raise SchemaError("count must be an integer between 1 and 20")
        definition = self._dyn.get(profile_id, QUESTION_STORE)
        fields = definition["schema"]["fields"]
        where = where or {}
        if not isinstance(where, dict):
            raise SchemaError("where must be an object")
        unknown = set(where) - set(fields)
        if unknown:
            raise SchemaError(f"unknown query fields: {sorted(unknown)}")
        rows = self.db.execute(
            "SELECT * FROM dynamic_records WHERE profile_id=? AND store_name=?",
            (profile_id, QUESTION_STORE)).fetchall()
        candidates = [self._dyn._record_dict(row) for row in rows]
        candidates = [record for record in candidates
                      if record["data"].get("answer_status") in {"active", "overridden"}
                      and self._dyn._matches(record["data"], where)
                      and float(record["data"].get("weight", 0)) > 0]
        if count > len(candidates):
            raise SchemaError("not enough eligible questions for requested count")
        chooser = random.SystemRandom()
        selected = []
        pool = [(record, float(record["data"]["weight"])) for record in candidates]
        while len(selected) < count:
            threshold = chooser.random() * sum(weight for _, weight in pool)
            running = 0.0
            for index, (record, weight) in enumerate(pool):
                running += weight
                if threshold < running:
                    selected.append(record)
                    pool.pop(index)
                    break
        now = time.time()
        code = uuid.uuid4().hex
        draw_items = []
        public_items = []
        markdown = []
        for position, record in enumerate(selected, 1):
            data = record["data"]
            options = self._options(data)
            label_map = {option["label"]: option["id"] for option in options}
            draw_items.append({
                "position": position, "record_id": record["id"],
                "question_ref": data["question_key"], "label_map": label_map,
            })
            public_items.append({
                "position": position, "question_ref": data["question_key"],
                "domain": data["domain"], "prompt": data["prompt"],
                "options": [{"label": option["label"], "text": option["text"]}
                            for option in options],
            })
            markdown.append(f"### Question {position} of {count}\n\n{data['prompt']}\n\n" +
                            "\n".join(f"- **{option['label']}.** {option['text']}"
                                      for option in options))
        with self.db:
            self.db.execute(
                "INSERT INTO question_attempts"
                " (code, profile_id, draw, status, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?)",
                (code, profile_id, json.dumps(draw_items), "pending", now,
                 now + ATTEMPT_TTL_SECONDS))
        return {
            "attempt_code": code,
            "expires_at": now + ATTEMPT_TTL_SECONDS,
            "markdown": "\n\n".join(markdown),
            "questions": public_items,
        }

    def _attempt(self, code: str, profile_id: str, require_pending: bool = False):
        row = self.db.execute(
            "SELECT * FROM question_attempts WHERE code=? AND profile_id=?",
            (code, profile_id)).fetchone()
        if row is None:
            raise SchemaError("unknown attempt_code")
        if row["expires_at"] < time.time() and row["status"] == "pending":
            with self.db:
                self.db.execute("UPDATE question_attempts SET status='expired' WHERE code=?", (code,))
            raise DynStoreConflict("attempt_code expired after 24 hours")
        if require_pending and row["status"] != "pending":
            raise DynStoreConflict(f"attempt_code is already {row['status']}")
        return row

    def grade(self, companion_name: str, attempt_code: str,
              answers: list[dict]) -> dict:
        profile_id = self._profile_id(companion_name)
        attempt = self._attempt(attempt_code, profile_id, require_pending=True)
        prepared = self._prepare_answers(attempt, profile_id, answers)
        return self._apply_grade(attempt_code, prepared)

    def _prepare_answers(self, attempt, profile_id: str, answers: list[dict]):
        draw = json.loads(attempt["draw"])
        if not isinstance(answers, list):
            raise SchemaError("answers must be an array")
        by_position = {answer.get("position"): answer for answer in answers
                       if isinstance(answer, dict)}
        expected = {item["position"] for item in draw}
        if set(by_position) != expected or len(by_position) != len(answers):
            raise SchemaError("provide exactly one answer for every drawn question position")

        prepared = []
        for item in draw:
            answer = by_position[item["position"]]
            labels = answer.get("selected")
            if not isinstance(labels, list) or not labels or not all(
                    isinstance(label, str) for label in labels):
                raise SchemaError(f"position {item['position']} requires selected option labels")
            normalized_labels = [label.strip().upper() for label in labels]
            if len(set(normalized_labels)) != len(normalized_labels):
                raise SchemaError(f"position {item['position']} repeats an option")
            unknown = set(normalized_labels) - set(item["label_map"])
            if unknown:
                raise SchemaError(f"position {item['position']} has unknown options: {sorted(unknown)}")
            row = self.db.execute(
                "SELECT * FROM dynamic_records WHERE id=? AND profile_id=? AND store_name=?",
                (item["record_id"], profile_id, QUESTION_STORE)).fetchone()
            if row is None:
                raise SchemaError("drawn question no longer exists")
            data = json.loads(row["data"])
            selected_ids = [item["label_map"][label] for label in normalized_labels]
            correct = (data.get("answer_status") in {"active", "overridden"}
                       and set(selected_ids) == set(data["effective_correct_option_ids"]))
            prepared.append((item, row, data, normalized_labels, selected_ids, correct))
        return prepared

    def _apply_grade(self, attempt_code: str, prepared: list[tuple]) -> dict:
        results = []
        answer_history = []
        with self.db:
            for item, row, data, labels, selected_ids, correct in prepared:
                active = data.get("answer_status") in {"active", "overridden"}
                if active:
                    if correct:
                        data["correct_count"] += 1
                        data["weight"] = max(MIN_WEIGHT, float(data["weight"]) / 2)
                    else:
                        data["wrong_count"] += 1
                        data["weight"] = min(MAX_WEIGHT, float(data["weight"]) * 2)
                    self.db.execute(
                        "UPDATE dynamic_records SET data=?, updated_at=? WHERE id=?",
                        (json.dumps(data), time.time(), row["id"]))
                answer_history.append({
                    "position": item["position"], "record_id": row["id"],
                    "selected_option_ids": selected_ids,
                })
                results.append(self._grade_result(item["position"], data, labels,
                                                  selected_ids, correct, active))
            self.db.execute(
                "UPDATE question_attempts SET answers=?, status='graded', graded_at=? WHERE code=?",
                (json.dumps(answer_history), time.time(), attempt_code))
        markdown = "\n\n".join(result["markdown"] for result in results)
        return {"attempt_code": attempt_code, "markdown": markdown, "results": results}

    def regrade(self, companion_name: str, attempt_code: str,
                answers: list[dict], reason: str) -> dict:
        """Correct a consumed grading attempt and replay affected learning stats.

        Answer-key revisions and grading corrections are deliberately separate
        audit trails: this changes the learner's submitted selections, never
        the source question's answer key.
        """
        profile_id = self._profile_id(companion_name)
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaError("reason is required")
        attempt = self._attempt(attempt_code, profile_id)
        if attempt["status"] != "graded":
            raise DynStoreConflict("attempt_code must already be graded before correction")
        prepared = self._prepare_answers(attempt, profile_id, answers)
        answer_history = [{"position": item["position"], "record_id": row["id"],
                           "selected_option_ids": selected_ids}
                          for item, row, _, _, selected_ids, _ in prepared]
        affected_record_ids = {row["id"] for _, row, _, _, _, _ in prepared}
        now = time.time()
        with self.db:
            # Keep the original graded_at: learning weight is order-dependent,
            # and a correction must not pretend the attempt happened later.
            self.db.execute(
                "UPDATE question_attempts SET answers=? WHERE code=?",
                (json.dumps(answer_history), attempt_code))
            self.db.execute(
                "INSERT INTO question_grade_audit"
                " (id, profile_id, attempt_code, before_answers, after_answers, reason, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), profile_id, attempt_code, attempt["answers"] or "[]",
                 json.dumps(answer_history), reason.strip(), now))
            for record_id in affected_record_ids:
                self._recompute_stats(record_id)
        results = [self._grade_result(item["position"], data, labels, selected_ids, correct,
                                      data.get("answer_status") in {"active", "overridden"})
                   for item, _, data, labels, selected_ids, correct in prepared]
        return {"attempt_code": attempt_code, "markdown": "\n\n".join(
            result["markdown"] for result in results), "results": results, "corrected": True}

    def weaknesses(self, companion_name: str) -> dict:
        """Return a compact, ranked diagnostic view without exporting the bank."""
        profile_id = self._profile_id(companion_name)
        rows = self.db.execute(
            "SELECT data FROM dynamic_records WHERE profile_id=? AND store_name=?",
            (profile_id, QUESTION_STORE)).fetchall()
        grouped: dict[tuple[str, str | None], dict] = {}
        for row in rows:
            data = json.loads(row["data"])
            domain = str(data.get("domain") or "Unspecified").strip() or "Unspecified"
            # Existing banks predate sub-skill tagging. Keeping it nullable
            # makes the report useful now and automatically more granular
            # once a future schema migration supplies that field.
            sub_skill = data.get("sub_skill")
            sub_skill = str(sub_skill).strip() if sub_skill else None
            bucket = grouped.setdefault((domain, sub_skill), {
                "domain": domain, "sub_skill": sub_skill,
                "wrong_count": 0, "times_shown": 0, "question_count": 0,
            })
            bucket["wrong_count"] += int(data.get("wrong_count", 0))
            bucket["times_shown"] += int(data.get("correct_count", 0)) + int(data.get("wrong_count", 0))
            bucket["question_count"] += 1
        items = sorted(grouped.values(), key=lambda item: (
            -item["wrong_count"], -item["times_shown"], item["domain"],
            item["sub_skill"] or ""))
        return {"items": items}

    def _grade_result(self, position: int, data: dict, labels: list[str],
                      selected_ids: list[str], correct: bool, active: bool) -> dict:
        if not active:
            markdown = f"### Question {position}\n\nAnswer key nullified; no score recorded."
            return {"position": position, "status": "nullified", "markdown": markdown}
        if correct:
            return {"position": position, "status": "correct", "markdown": "OK"}
        option_by_id = {option["id"]: option for option in self._options(data)}
        effective = set(data["effective_correct_option_ids"])
        details = []
        for label, option_id in zip(labels, selected_ids):
            if option_id not in effective:
                option = option_by_id[option_id]
                details.append(f"**Why {label} is wrong:** {option['explanation']}")
        for option_id in effective - set(selected_ids):
            option = option_by_id[option_id]
            details.append(
                f"**Missed correct option ({option['text']}):** {option['explanation']}")
        markdown = (f"### Question {position} — Incorrect\n\n" + "\n\n".join(details) +
                    f"\n\n**Full explanation:** {data['overall_explanation']}")
        return {"position": position, "status": "wrong", "markdown": markdown}

    def revise_answer(self, companion_name: str, attempt_code: str, position: int,
                      action: str, selected: list[str] | None, reason: str) -> dict:
        profile_id = self._profile_id(companion_name)
        if action not in {"override", "nullify", "restore_extracted"}:
            raise SchemaError("action must be override, nullify, or restore_extracted")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaError("reason is required")
        attempt = self._attempt(attempt_code, profile_id)
        item = next((entry for entry in json.loads(attempt["draw"])
                     if entry["position"] == position), None)
        if item is None:
            raise SchemaError("question position is not part of this attempt")
        row = self.db.execute(
            "SELECT * FROM dynamic_records WHERE id=? AND profile_id=? AND store_name=?",
            (item["record_id"], profile_id, QUESTION_STORE)).fetchone()
        if row is None:
            raise SchemaError("question no longer exists")
        data = json.loads(row["data"])
        before = list(data["effective_correct_option_ids"])
        if action == "override":
            labels = [label.strip().upper() for label in (selected or [])]
            if not labels or len(set(labels)) != len(labels):
                raise SchemaError("override requires distinct selected option labels")
            unknown = set(labels) - set(item["label_map"])
            if unknown:
                raise SchemaError(f"unknown options: {sorted(unknown)}")
            data["effective_correct_option_ids"] = [item["label_map"][label] for label in labels]
            data["answer_status"] = "overridden"
        elif action == "nullify":
            data["effective_correct_option_ids"] = []
            data["answer_status"] = "nullified"
        else:
            data["effective_correct_option_ids"] = list(data["extracted_correct_option_ids"])
            data["answer_status"] = "active"
        data["answer_revision_note"] = reason.strip()
        data["answer_revision_count"] += 1
        with self.db:
            self.db.execute(
                "UPDATE dynamic_records SET data=?, updated_at=? WHERE id=?",
                (json.dumps(data), time.time(), row["id"]))
            self.db.execute(
                "INSERT INTO question_answer_audit"
                " (id, profile_id, question_record_id, action, before_answer, after_answer,"
                " reason, attempt_code, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), profile_id, row["id"], action, json.dumps(before),
                 json.dumps(data["effective_correct_option_ids"]), reason.strip(),
                 attempt_code, time.time()))
        self._recompute_stats(row["id"])
        refreshed = self._dyn.get_record(profile_id, QUESTION_STORE, row["id"])
        return {
            "question_ref": data["question_key"], "action": action,
            "answer_status": refreshed["data"]["answer_status"],
            "weight": refreshed["data"]["weight"],
            "correct_count": refreshed["data"]["correct_count"],
            "wrong_count": refreshed["data"]["wrong_count"],
        }

    def _recompute_stats(self, record_id: str) -> None:
        row = self.db.execute(
            "SELECT * FROM dynamic_records WHERE id=? AND profile_id=? AND store_name=?",
            (record_id, QUESTION_PROFILE, QUESTION_STORE)).fetchone()
        data = json.loads(row["data"])
        correct_count = wrong_count = 0
        weight = INITIAL_WEIGHT
        if data["answer_status"] in {"active", "overridden"}:
            attempts = self.db.execute(
                "SELECT answers FROM question_attempts WHERE profile_id=? AND status='graded'"
                " ORDER BY graded_at, created_at",
                (QUESTION_PROFILE,)).fetchall()
            effective = set(data["effective_correct_option_ids"])
            for attempt in attempts:
                for answer in json.loads(attempt["answers"] or "[]"):
                    if answer["record_id"] != record_id:
                        continue
                    if set(answer["selected_option_ids"]) == effective:
                        correct_count += 1
                        weight = max(MIN_WEIGHT, weight / 2)
                    else:
                        wrong_count += 1
                        weight = min(MAX_WEIGHT, weight * 2)
        data.update(weight=weight, correct_count=correct_count, wrong_count=wrong_count)
        self.db.execute(
            "UPDATE dynamic_records SET data=?, updated_at=? WHERE id=?",
            (json.dumps(data), time.time(), record_id))
