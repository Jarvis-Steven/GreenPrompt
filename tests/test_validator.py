"""Jarvis: test supported correct/incorrect answers and unchecked cases."""
"""Tests for Jarvis's answer quality checks."""

import unittest

from backend.validator import check_answer


class ValidatorTests(unittest.TestCase):

    def test_empty_answer_fails(self):
        result = check_answer("What is 12 + 8?", "   ")
        self.assertEqual(result["status"], "failed")

    def test_correct_addition_passes(self):
        result = check_answer("What is 12 + 8?", "20")
        self.assertEqual(result["status"], "passed")

    def test_incorrect_addition_fails(self):
        result = check_answer("What is 12 + 8?", "21")
        self.assertEqual(result["status"], "failed")

    def test_correct_subtraction_passes(self):
        result = check_answer("What is 12 - 8?", "4")
        self.assertEqual(result["status"], "passed")

    def test_correct_multiplication_passes(self):
        result = check_answer("What is 3 * 7?", "21")
        self.assertEqual(result["status"], "passed")

    def test_incorrect_multiplication_fails(self):
        result = check_answer("What is 3 * 7?", "20")
        self.assertEqual(result["status"], "failed")

    def test_negative_result_passes(self):
        result = check_answer("What is 3 - 7?", "-4")
        self.assertEqual(result["status"], "passed")

    def test_unsupported_question_is_unchecked(self):
        result = check_answer(
            "Explain solar panels",
            "They convert sunlight into electricity.",
        )
        self.assertEqual(result["status"], "unchecked")

    def test_sentence_answer_is_unchecked(self):
        result = check_answer(
            "What is 12 + 8?",
            "The answer is 20.",
        )
        self.assertEqual(result["status"], "unchecked")


class RealModelFormattingTests(unittest.TestCase):
    """PROPOSAL: formats real Groq answers actually come back in.

    Every string here was observed in a live run on 2026-10-09.
    """

    def test_narrow_no_break_spaces_in_an_equation(self):
        # "12 + 8 = 20" with U+202F between every token.
        result = check_answer("What is 12 + 8?", "12 + 8 = 20")
        self.assertEqual(result["status"], "passed")

    def test_wrong_equation_still_fails(self):
        result = check_answer("What is 12 + 8?", "12 + 8 = 21")
        self.assertEqual(result["status"], "failed")

    def test_typographic_multiplication_sign(self):
        result = check_answer("What is 7 * 6?", "7 × 6 = 42")
        self.assertEqual(result["status"], "passed")

    def test_typographic_minus_sign_and_trailing_period(self):
        result = check_answer("What is 100 - 37?", "100 − 37 = 63.")
        self.assertEqual(result["status"], "passed")

    def test_bold_markers_are_stripped(self):
        self.assertEqual(check_answer("What is 12 + 8?", "**20**")["status"], "passed")

    def test_bold_markers_on_a_wrong_answer_still_fail(self):
        self.assertEqual(check_answer("What is 12 + 8?", "**21**")["status"], "failed")

    def test_trailing_period_on_a_bare_number(self):
        self.assertEqual(check_answer("What is 12 + 8?", "20.")["status"], "passed")

    def test_thousands_commas(self):
        self.assertEqual(check_answer("What is 1000 + 234?", "1,234")["status"], "passed")

    def test_the_word_equals_is_accepted(self):
        # Observed live: "12 + 8 equals **20**."
        result = check_answer("What is 12 + 8?", "12 + 8 equals **20**.")
        self.assertEqual(result["status"], "passed")

    def test_the_word_equals_with_a_wrong_result_fails(self):
        result = check_answer("What is 12 + 8?", "12 + 8 equals 21")
        self.assertEqual(result["status"], "failed")

    def test_latex_wrapped_arithmetic_is_read(self):
        # Observed live: the small model answers hard arithmetic in LaTeX.
        result = check_answer("What is 48213 * 97?", r"\(48213 \times 97 = 4{,}676{,}661\)")
        self.assertEqual(result["status"], "passed")

    def test_latex_wrong_answer_still_fails(self):
        # The whole point: stripping presentation must not excuse a wrong digit.
        result = check_answer("What is 48213 * 97?", r"\(48213 \times 97 = 4{,}676{,}662\)")
        self.assertEqual(result["status"], "failed")

    def test_latex_thin_space_separators(self):
        # 26\,806\,677 and 49,\!121,\!557
        self.assertEqual(
            check_answer("What is 86753 * 309?", r"\(86\,753 \times 309 = 26\,806\,677\)")["status"],
            "passed")
        self.assertEqual(
            check_answer("What is 7919 * 6203?", r"\(7919 \times 6203 = 49,\!121,\!557\)")["status"],
            "passed")

    def test_latex_display_brackets_and_cdot(self):
        result = check_answer("What is 9999 * 9999?", r"\[9999 \cdot 9999 = 99,980,001\]")
        self.assertEqual(result["status"], "passed")

    def test_dollar_delimited_math(self):
        self.assertEqual(check_answer("What is 12 + 8?", "$12 + 8 = 20$")["status"], "passed")
        self.assertEqual(check_answer("What is 12 + 8?", "$12 + 8 = 21$")["status"], "failed")

    def test_latex_prose_still_stays_unchecked(self):
        # Stripping LaTeX must not turn a sentence into a checkable claim.
        result = check_answer("What is 12 + 8?", r"The answer is \(20\).")
        self.assertEqual(result["status"], "unchecked")

    def test_latex_equation_for_a_different_question_is_not_borrowed(self):
        result = check_answer("What is 12 + 8?", r"\(9 + 9 = 18\)")
        self.assertEqual(result["status"], "unchecked")

    def test_equation_for_a_different_question_is_not_borrowed(self):
        # The equation does not restate this question, so there is no clear number.
        result = check_answer("What is 12 + 8?", "9 + 9 = 18")
        self.assertEqual(result["status"], "unchecked")

    def test_prose_mentioning_a_number_stays_unchecked(self):
        # Guards the existing contract: no false correctness claims.
        for answer in ("The answer is 20.", "I think it is probably 20 or so.",
                       "Adding 12 and 8 gives you twenty."):
            with self.subTest(answer=answer):
                self.assertEqual(check_answer("What is 12 + 8?", answer)["status"], "unchecked")


if __name__ == "__main__":
    unittest.main()