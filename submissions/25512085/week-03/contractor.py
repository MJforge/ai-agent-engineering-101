"""LLM contractor definitions and the provisional bidding prompt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from protocol import (
    BID_THRESHOLD,
    Announcement,
    BidAttempt,
    BidParseError,
    parse_bid,
)


ModelCaller = Callable[[str, str], str]


DEFAULT_BID_POLICY = (
    "Identify the single most important ability for the announced task: "
    "calculation, writing, or coding. Use your numeric score for that ability "
    "as confidence. You must set bid=true when confidence is at least "
    f"{BID_THRESHOLD}, and bid=false when confidence is below {BID_THRESHOLD}. "
    "You must respond even when bid=false."
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
    abilities: Mapping[str, int]
    bid_policy: str = DEFAULT_BID_POLICY
    extra_instruction: str = ""

    def system_prompt(self) -> str:
        ability_text = ", ".join(
            f"{ability}={score}"
            for ability, score in self.abilities.items()
        )
        parts = [
            f"You are contractor {self.name} in a contract net.",
            f"Your abilities are: {ability_text}.",
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
