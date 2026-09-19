"""Offline tests for bid policy and manager behavior (no model/API calls)."""

from __future__ import annotations

import json
import unittest

from manager import run_round
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


if __name__ == "__main__":
    unittest.main()
