# P1 partial-condition parsing review

- Start HEAD 72afb71. AGENTS/status and previous candidate/report/control read first.
- Existing dirty logs/report and untracked originals remain untouched/unstaged.
- Reproduce initial/later turns, with/without price, before fixing condition coverage.
- Keep C-only/A72/no-price-change scope. Revision03 isolated candidate only;
  no production replacements, restarts, signals, state/key reads, RPC/POST or push.
- Baseline focused 111 pass. Six new regression methods reproduce 49 failures
  (including subtests), covering both main counterexamples and duplicate clauses.
- Per-marker coverage, trailing-operator checks and duplicate-clause rejection:
  green-focused 117 pass. Preserve both red and green logs before boundary probes.
