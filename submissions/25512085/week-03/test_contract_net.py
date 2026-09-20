"""Offline tests for bid policy and manager behavior (no model/API calls)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from manager import run_round
from model_client import (
    LMStudioCaller,
    Meter,
    ModelSettings,
    extract_message_content,
    normalize_server_url,
)
from protocol import BidParseError, Task, parse_bid
from runner import build_team


def response(*, bid: bool, confidence: int, reason: str = "test") -> str:
    return json.dumps(
        {"bid": bid, "confidence": confidence, "reason": reason}
    )


class ContractNetTests(unittest.TestCase):
    def test_baseline_specialist_wins(self) -> None:
        answers = iter(
            [
                response(bid=True, confidence=90),
                response(bid=False, confidence=40),
                response(bid=False, confidence=50),
            ]
        )

        result = run_round(
            [Task(1, "Calculate the total.", "A")],
            build_team("baseline"),
            lambda _system, _user: next(answers),
            log=lambda _message: None,
        )

        self.assertEqual(result.correct, 1)
        self.assertEqual(result.messages, 5)  # 3 announcements + bid + award
        self.assertEqual(result.unassigned, 0)
        self.assertEqual(result.parse_fails, 0)

    def test_all_false_responses_are_unassigned(self) -> None:
        answers = iter([response(bid=False, confidence=40)] * 3)

        result = run_round(
            [Task(2, "An unmatched task.", "A")],
            build_team("baseline"),
            lambda _system, _user: next(answers),
            log=lambda _message: None,
        )

        self.assertEqual(result.messages, 3)  # all responded, none bid
        self.assertEqual(result.unassigned, 1)

    def test_bid_must_match_threshold(self) -> None:
        with self.assertRaises(BidParseError):
            parse_bid(
                response(bid=True, confidence=40),
                contractor="A",
                task_id=3,
            )

    def test_homogeneous_team_has_equal_profiles(self) -> None:
        profiles = [dict(contractor.abilities)
                    for contractor in build_team("homogeneous")]
        expected = {"calculation": 70, "writing": 70, "coding": 70}
        self.assertEqual(profiles, [expected, expected, expected])

    def test_lmstudio_url_normalization(self) -> None:
        self.assertEqual(
            normalize_server_url("http://127.0.0.1:1234/v1"),
            "http://127.0.0.1:1234",
        )

    def test_lmstudio_request_disables_reasoning(self) -> None:
        settings = ModelSettings(
            provider="lmstudio",
            model="qwen/qwen3.8-27b",
            temperature=0,
            server_url="http://127.0.0.1:1234",
            api_token=None,
            timeout_seconds=120,
        )
        payload = LMStudioCaller(settings, Meter()).build_payload("system", "user")
        self.assertEqual(payload["reasoning"], "off")
        self.assertFalse(payload["store"])

    def test_lmstudio_response_ignores_reasoning_item(self) -> None:
        payload = {
            "output": [
                {"type": "reasoning", "content": "hidden thought"},
                {"type": "message", "content": response(bid=True, confidence=90)},
            ]
        }
        content = extract_message_content(payload)
        self.assertEqual(json.loads(content)["confidence"], 90)

    def test_environment_accepts_openai_compatible_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_MODEL": "qwen/qwen3.8-27b",
                "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
            },
            clear=True,
        ):
            settings = ModelSettings.from_env()
        self.assertEqual(settings.server_url, "http://127.0.0.1:1234")
        self.assertEqual(settings.reasoning, "off")


if __name__ == "__main__":
    unittest.main()
