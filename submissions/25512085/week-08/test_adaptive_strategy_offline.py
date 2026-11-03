"""Adaptive policy regressions: fake observations and isolated temporary state only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import market_buyer as buyer


class MissingStarsTests(unittest.TestCase):
    def test_null_stars_do_not_crash_or_become_ratings(self):
        memory = {"shop": {"recent": [{"stars": None}, {"stars": 5}]}}
        result = buyer.summarize_shop({"name": "shop", "url": "https://offline.invalid",
                                      "reviews": [{"stars": None}]}, memory)
        self.assertEqual(result["mem_avg"], 5)
        self.assertEqual(result["mem_count"], 2)
        self.assertFalse(result["dishonest"])

    def test_null_stars_archive_without_inventing_score(self):
        memory = {"shop": {"recent": [{"stars": None}] * 5}}
        with tempfile.TemporaryDirectory(prefix="adaptive-stars-") as tmp:
            path = Path(tmp) / "memory.json"
            with patch.object(buyer, "MEMORY_FILE", path):
                buyer.update_memory(memory, "shop", {"stars": 4})
            saved = json.loads(path.read_text(encoding="utf-8"))["shop"]
        self.assertEqual(saved.get("archived_count", 0), 0)
        self.assertEqual(len(saved["recent"]), 5)


if __name__ == "__main__":
    unittest.main()
