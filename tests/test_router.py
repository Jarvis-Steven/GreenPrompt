"""Tests for Jarvis's routing functions."""

import unittest

from backend.router import (
    classify_from_model_output,
    classify_with_rules,
    choose_model,
    next_model,
)


class RouterTests(unittest.TestCase):

    def test_simple_arithmetic_is_easy(self):
        result = classify_with_rules("What is 12 + 8?")
        self.assertEqual(result["difficulty"], "easy")

    def test_unrecognized_prompt_is_unsure(self):
        result = classify_with_rules("Explain solar panels")
        self.assertIsNone(result["difficulty"])

    def test_pick_mode_respects_selection(self):
        result = choose_model("hard", "pick", "small")
        self.assertEqual(result, "small")

    def test_failed_answer_escalates(self):
        result = next_model("small", "failed", "success")
        self.assertEqual(result, "medium")

    def test_big_model_stops(self):
        result = next_model("big", "failed", "success")
        self.assertIsNone(result)

    def test_valid_classifier_output(self):
        raw_text = '{"difficulty": "medium", "reason": "Needs explanation."}'

        result = classify_from_model_output(
            "Explain solar panels",
            raw_text,
        )

        self.assertEqual(result["difficulty"], "medium")
        self.assertEqual(result["reason"], "Needs explanation.")

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ValueError):
            classify_from_model_output(
                "Explain solar panels",
                "This looks medium.",
            )

    def test_invalid_difficulty_is_rejected(self):
        raw_text = '{"difficulty": "very hard", "reason": "Complex task."}'

        with self.assertRaises(ValueError):
            classify_from_model_output(
                "Explain solar panels",
                raw_text,
            )


if __name__ == "__main__":
    unittest.main()