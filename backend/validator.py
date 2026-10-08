"""Owner: Jarvis. Supported deterministic checks; no false correctness claims.

PROPOSAL (branch proposed-fixes, by the backend member): only the input
normalisation below is new. The decision rules are unchanged:
  - a bare number answer is checked,
  - a prose sentence stays "unchecked",
  - a wrong number still fails.

Why: real model answers come back as "12 + 8 = 20" using narrow
no-break spaces (U+202F) and typographic operators (×, −). The old parser
called int() on that and every real answer fell through to "unchecked".
"""
import re

# Unicode spaces models emit instead of a plain space.
_UNICODE_SPACES = "               　​"

# Typographic operators models emit instead of ASCII ones.
_OPERATOR_ALIASES = {"×": "*", "⋅": "*", "·": "*",
                     "−": "-", "–": "-", "—": "-"}

# A bare number, optionally signed, with thousands commas and a trailing period.
_BARE_NUMBER = re.compile(r"^[+-]?\d[\d,]*\.?$")

# "a op b = n" anywhere in the answer.
_EQUATION = re.compile(r"(\d[\d,]*)\s*([+*-])\s*(\d[\d,]*)\s*=\s*([+-]?\d[\d,]*)")


def _normalise(text: str) -> str:
    """Unicode spaces to plain spaces, typographic operators to ASCII."""
    for space in _UNICODE_SPACES:
        text = text.replace(space, " ")
    for fancy, plain in _OPERATOR_ALIASES.items():
        text = text.replace(fancy, plain)
    return re.sub(r"\s+", " ", text).strip()


def _strip_formatting(text: str) -> str:
    """Remove bold/italic/code markers. Single '*' is left alone: it is multiplication."""
    text = text.replace("**", "").replace("__", "")
    return text.replace("`", "").replace("$", "").strip()


def _to_int(token: str):
    """'1,234' or '20.' to an int, or None when it is not a whole number."""
    try:
        return int(token.replace(",", "").rstrip("."))
    except (ValueError, AttributeError):
        return None


def _answer_number(answer: str, operands: tuple):
    """The number this answer asserts, or None when there is no single clear one.

    Accepts a bare number ("20", "**20**", "20.", "1,234") or an equation that
    restates this very question ("12 + 8 = 20"). A prose sentence that merely
    mentions a number is deliberately NOT accepted, so it stays "unchecked".
    """
    cleaned = _strip_formatting(_normalise(answer))

    if _BARE_NUMBER.match(cleaned):
        return _to_int(cleaned)

    first, operator, second = operands
    for match in _EQUATION.finditer(cleaned):
        if (_to_int(match.group(1)) == first
                and match.group(2) == operator
                and _to_int(match.group(3)) == second):
            return _to_int(match.group(4))
    return None


def check_answer(prompt: str, answer: str) -> dict:
    if not answer.strip():
        return {
            "status": "failed",
            "reason": "The model returned an empty answer."
        }

    question = _normalise(prompt).lower()

    match = re.fullmatch(
        r"(?:what is\s+)?(\d+)\s*([+*-])\s*(\d+)\s*\??",
        question
    )

    if match:
        first_number = int(match.group(1))
        operator = match.group(2)
        second_number = int(match.group(3))

        if operator == "+":
            expected_answer = first_number + second_number
        elif operator == "-":
            expected_answer = first_number - second_number
        else:
            expected_answer = first_number * second_number

        actual_answer = _answer_number(answer, (first_number, operator, second_number))
        if actual_answer is None:
            return {
                "status": "unchecked",
                "reason": "This addition check requires a whole-number answer."
            }

        if actual_answer == expected_answer:
            return {
                "status": "passed",
                "reason": "The addition result is correct."
            }

        return {
            "status": "failed",
            "reason": "The addition result is incorrect."
        }

    return {
        "status": "unchecked",
        "reason": "No correctness check is available for this answer yet."
    }
