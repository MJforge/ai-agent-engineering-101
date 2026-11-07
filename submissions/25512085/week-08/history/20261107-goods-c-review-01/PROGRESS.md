# P1 partial-condition parsing review

- Start HEAD 72afb71. AGENTS/status and previous candidate/report/control read first.
- Existing dirty logs/report and untracked originals remain untouched/unstaged.
- Reproduce initial/later turns, with/without price, before fixing condition coverage.
- Keep C-only/A72/no-price-change scope. Revision03 isolated candidate only;
  no production replacements, restarts, signals, state/key reads, RPC/POST or push.
