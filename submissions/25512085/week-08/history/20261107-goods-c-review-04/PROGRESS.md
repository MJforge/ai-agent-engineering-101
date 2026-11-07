# Compound negative minimum-grade review / revision06

- AGENTS/status and previous REPORT/control verified before edits. Start ad35d38.
- Preserve existing dirty logs/report, untracked originals and all prior failed logs.
- Reproduce initial/later with/without price; consume supported minimum suffix,
  reject unsupported compound negatives globally only when a grade is specified.
- Review bounded clause grammar and harmless product-name substrings.
- Only local changes / remote exclusive candidate validation. No production
  replacement, restart, signal, state/key access, RPC/POST, install or push.
- Resume verified HEAD 5024fd7 and the existing dirty seller patch; no rollback.
- baseline-focused 119 pass; red-focused 124 / 38 subtest-inclusive failures.
- Interrupted final-focused completed: 124 / one fixture failure. `B급` has always
  selected the cheap cohort, so bare `B급 이상` legitimately selects B6, not A72.
  Correct the new fixture rather than change existing selection/price behavior.
- Interrupted final-all has no BACKPORT_TEST_RESULT and stops during an unrelated
  restock test. Preserve it as incomplete, not a test pass or assertion failure.
