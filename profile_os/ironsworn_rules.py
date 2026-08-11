"""Read indexed Ironsworn text from a companion's Lodestar file corpus."""

from __future__ import annotations

import re
import unicodedata

_INDEX_ROW = re.compile(
    r"^\|\s*(?P<move>[^|]+?)\s*\|\s*(?P<when>.*?)\s*\|\s*(?P<line>\d+)\s*\|\s*$"
)


class MoveNotFoundError(LookupError):
    """Raised when an indexed move name cannot be resolved."""


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def _indexed_moves(index_text: str) -> list[dict]:
    moves = []
    for row in index_text.splitlines():
        match = _INDEX_ROW.match(row)
        if match:
            moves.append({
                "move": match.group("move").strip(),
                "when_it_applies": match.group("when").strip(),
                "line": int(match.group("line")),
            })
    if not moves:
        raise ValueError("Ironsworn move index contains no move rows")
    return moves


def _section_at_line(lines: list[str], line_number: int) -> str:
    start = line_number - 1
    if start < 0 or start >= len(lines):
        raise ValueError(f"indexed line {line_number} is outside the source document")
    if not lines[start].startswith("### "):
        raise ValueError(f"indexed line {line_number} does not start a level-three heading")
    end = next(
        (position for position in range(start + 1, len(lines))
         if lines[position].startswith("### ")),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip()


def get_move(move_name: str, *, index_text: str, compendium_text: str) -> dict:
    """Return one complete indexed move section.

    The index's line number selects duplicate headings such as the normal and
    scene-challenge versions of Face Danger. The end boundary is the next
    indexed move, so category headings between moves are not lost.
    """
    requested = _key(move_name)
    if not requested:
        raise MoveNotFoundError("move name must not be empty")

    moves = _indexed_moves(index_text)
    exact = [item for item in moves if _key(item["move"]) == requested]
    if not exact:
        suggestions = [item["move"] for item in moves if requested in _key(item["move"])]
        suffix = f"; possible matches: {', '.join(suggestions)}" if suggestions else ""
        raise MoveNotFoundError(f"Ironsworn move {move_name!r} not found{suffix}")

    selected = exact[0]
    lines = compendium_text.splitlines()
    text = _section_at_line(lines, selected["line"])
    return {
        "move": selected["move"],
        "when_it_applies": selected["when_it_applies"],
        "text": text,
        "source": "Ironsworn-Lodestar-Moves-Compendium.md",
    }


def get_oracle(oracle_name: str, *, index_text: str, omnibus_text: str) -> dict:
    """Return one complete oracle table or section named by the oracle index."""
    requested = _key(oracle_name)
    if not requested:
        raise MoveNotFoundError("oracle name must not be empty")
    oracles = _indexed_moves(index_text)
    exact = [item for item in oracles if _key(item["move"]) == requested]
    if not exact:
        suggestions = [item["move"] for item in oracles if requested in _key(item["move"])]
        suffix = f"; possible matches: {', '.join(suggestions)}" if suggestions else ""
        raise MoveNotFoundError(f"Ironsworn oracle {oracle_name!r} not found{suffix}")
    selected = exact[0]
    text = _section_at_line(
        omnibus_text.splitlines(), selected["line"]
    )
    return {
        "oracle": selected["move"],
        "use_for": selected["when_it_applies"],
        "text": text,
        "source": "Ironsworn-Lodestar-Oracle-Omnibus.md",
    }
