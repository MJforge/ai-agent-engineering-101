"""LLM contractor definitions and the provisional bidding prompt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from protocol import Announcement, BidAttempt, BidParseError, parse_bid


ModelCaller = Callable[[str, str], str]


DEFAULT_BID_POLICY = (
    "Bid only when the task clearly matches your declared skill. "
    "Set confidence from 0 to 100 based on the strength of that match."
)

OUTPUT_CONTRACT = (
    'Reply with exactly one JSON object and nothing else: '
    '{"bid": true or false, "confidence": 0-100, '
    '"reason": "one short sentence"}. '
    "Do not solve the task. Do not use a Markdown code fence."
)


@dataclass(frozen=True)
class Contractor:
    name: str
    skill: str
    bid_policy: str = DEFAULT_BID_POLICY
    extra_instruction: str = ""

    def system_prompt(self) -> str:
        parts = [
            f"You are contractor {self.name} in a contract net.",
            f"Your declared skill is: {self.skill}.",
            self.bid_policy,
            OUTPUT_CONTRACT,
        ]
        if self.extra_instruction:
            parts.append(self.extra_instruction)
        return " ".join(parts)

    def request_bid(
        self,
        announcement: Announcement,
        call_model: ModelCaller,
    ) -> BidAttempt:
        """Ask the model for one bid and retain both raw and parsed forms."""
        raw = call_model(self.system_prompt(), announcement.to_message())
        try:
            parsed = parse_bid(
                raw,
                contractor=self.name,
                task_id=announcement.task_id,
            )
        except BidParseError as exc:
            return BidAttempt(
                contractor=self.name,
                task_id=announcement.task_id,
                raw=raw,
                parsed=None,
                parse_error=str(exc),
            )
        return BidAttempt(
            contractor=self.name,
            task_id=announcement.task_id,
            raw=raw,
            parsed=parsed,
        )
