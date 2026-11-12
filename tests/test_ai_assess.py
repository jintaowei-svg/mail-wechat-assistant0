"""Offline regression tests: fake messages, credentials, and AI responses only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailcore import Assessment, MailMessage, ai_assess


class AIAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "AI_BASE_URL": "https://ai.example.invalid/v1/",
            "AI_MODEL": "fictional-model",
            "AI_API_KEY": "fake-api-key-NOT-REAL",
        }
        self.msg = MailMessage(
            "Fictional Sender", "demo@example.invalid", "Demo invoice",
            "MAIL_BODY_PRIVATE_SENTINEL", None,
        )
        self.heuristic = Assessment(
            "important", "Rule summary", "Rule action", "tomorrow",
        )
        self.valid = {
            "priority": "important", "summary": "AI summary",
            "action": "AI action", "deadline": "Friday",
        }

    @staticmethod
    def envelope(content):
        return {"choices": [{"message": {"content": content}}]}

    def assess_response(self, response, heuristic=None):
        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), \
                patch("mailcore._post_json", return_value=response), \
                contextlib.redirect_stdout(output):
            result = ai_assess(self.msg, heuristic or self.heuristic)
        return result, output.getvalue()

    def test_valid_json_and_fenced_json(self):
        encoded = json.dumps(self.valid)
        for content in (encoded, f"```json\n{encoded}\n```", f"```\n{encoded}\n```"):
            with self.subTest(content=content[:12]):
                result, output = self.assess_response(self.envelope(content))
                self.assertEqual(result, Assessment("important", "AI summary", "AI action", "Friday"))
                self.assertEqual(output, "")

    def test_urgent_rule_cannot_be_downgraded(self):
        urgent = Assessment("urgent", "Rule summary", "Rule action")
        for priority in ("important", "ignore"):
            with self.subTest(priority=priority):
                content = json.dumps(dict(self.valid, priority=priority))
                result, _ = self.assess_response(self.envelope(content), urgent)
                self.assertEqual(result.priority, "urgent")

    def test_disabled_ai_never_makes_a_request(self):
        configurations = ({}, {"AI_MODEL": "demo"}, {"AI_BASE_URL": "https://example.invalid"},
                          dict(self.env, AI_BASE_URL="  "), dict(self.env, AI_MODEL="  "))
        for env in configurations:
            with self.subTest(configured_keys=sorted(env)), \
                    patch.dict(os.environ, env, clear=True), patch("mailcore._post_json") as post:
                self.assertIs(ai_assess(self.msg, self.heuristic), self.heuristic)
                post.assert_not_called()

    def test_http_network_and_timeout_errors_fall_back_safely(self):
        sensitive = "REMOTE_ERROR_PRIVATE_SENTINEL fake-api-key-NOT-REAL"
        errors = (
            HTTPError("https://example.invalid", 503, sensitive, {}, None),
            URLError(sensitive), TimeoutError(sensitive), ValueError(sensitive),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.dict(os.environ, self.env, clear=True), \
                        patch("mailcore._post_json", side_effect=error), \
                        contextlib.redirect_stdout(output):
                    self.assertIs(ai_assess(self.msg, self.heuristic), self.heuristic)
                self.assertIn(type(error).__name__, output.getvalue())
                self.assertNotIn(sensitive, output.getvalue())
                self.assertNotIn(self.env["AI_API_KEY"], output.getvalue())
                self.assertNotIn(self.msg.body, output.getvalue())

    def test_invalid_response_shapes_fall_back(self):
        responses = {
            "top-level-list": [], "top-level-null": None, "missing-choices": {},
            "empty-choices": {"choices": []}, "null-choices": {"choices": None},
            "string-choices": {"choices": "bad"},
            "null-choice": {"choices": [None]},
            "missing-message": {"choices": [{}]},
            "null-message": {"choices": [{"message": None}]},
            "list-message": {"choices": [{"message": []}]},
            "missing-content": {"choices": [{"message": {}}]},
            "null-content": self.envelope(None), "list-content": self.envelope([]),
            "object-content": self.envelope({}), "number-content": self.envelope(42),
            "empty-content": self.envelope(""), "blank-content": self.envelope(" \n "),
        }
        for name, response in responses.items():
            with self.subTest(shape=name):
                result, _ = self.assess_response(response)
                self.assertIs(result, self.heuristic)

    def test_non_object_model_json_falls_back(self):
        for content in ("[]", "null", '"hello"', "42", "false"):
            with self.subTest(content=content):
                result, _ = self.assess_response(self.envelope(content))
                self.assertIs(result, self.heuristic)

    def test_non_string_text_fields_reject_the_entire_assessment(self):
        for field in ("summary", "action", "deadline"):
            for value in (None, [], {}, 42, False):
                with self.subTest(field=field, value=value):
                    content = json.dumps(dict(self.valid, **{field: value}))
                    result, _ = self.assess_response(self.envelope(content))
                    self.assertIs(result, self.heuristic)

    def test_missing_and_empty_text_fields_preserve_rules(self):
        for obj in ({}, {"summary": "", "action": "", "deadline": ""},
                    {"summary": " \n ", "action": "\t", "deadline": " "}):
            with self.subTest(obj=obj):
                result, _ = self.assess_response(self.envelope(json.dumps(obj)))
                self.assertEqual(result, self.heuristic)

    def test_unknown_priority_preserves_rule_priority(self):
        content = json.dumps(dict(self.valid, priority="unknown"))
        result, _ = self.assess_response(self.envelope(content))
        self.assertEqual(result.priority, self.heuristic.priority)
        self.assertEqual(result.summary, "AI summary")

    def test_invalid_json_logging_does_not_expose_private_data(self):
        private_response = "RESPONSE_PRIVATE_SENTINEL " + self.msg.body + " " + self.env["AI_API_KEY"]
        result, output = self.assess_response(self.envelope(private_response))
        self.assertIs(result, self.heuristic)
        self.assertTrue(output)
        for private_value in ("RESPONSE_PRIVATE_SENTINEL", self.msg.body, self.env["AI_API_KEY"]):
            self.assertNotIn(private_value, output)


if __name__ == "__main__":
    unittest.main()
