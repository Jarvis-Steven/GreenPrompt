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


if __name__ == "__main__":
    unittest.main()