# Week 03 Report — Contract Net with LLM Contractors

## 1. Setup and reproduction

This experiment implements one manager and three LLM contractors using the
Contract Net sequence: announcement, bid, and award. The implementation is in
`manager.py`, `contractor.py`, `protocol.py`, `model_client.py`, and
`runner.py`. The precommitted six-task set is in `tasks.json`. `gold` is an
evaluation-only label and is not included in an announcement.

### Model and request settings

| Item | Setting |
|---|---|
| Provider | Local LM Studio native API |
| Endpoint | `http://127.0.0.1:1234/api/v1/chat` |
| Model | `qwen/qwen3.8-27b` |
| Temperature | `0.2` for every formal run |
| Reasoning | Off (`reasoning: "off"`) |
| Request state | Stateless (`store: false`) |
| Formal task set | Six English tasks: two calculation, two writing, and two coding gold assignments; three are mixed-skill tasks |

The manager announces every task to A, B, and C in that fixed order. Every
contractor must return one JSON object with `bid`, `confidence`, and `reason`.
`bid` must be true exactly when confidence is at least 70. The manager selects
the highest valid confidence; equal confidences keep the earlier contractor in
the A, B, C call order. A malformed bid is counted as no bid rather than
silently repaired. An award that differs from `gold` is retained as a
misaward.

The only condition change is the contractor profile:

| Condition | A | B | C |
|---|---|---|---|
| `baseline` | calculation specialist (90/40/50) | writing specialist (40/90/50) | coding specialist (50/40/90) |
| `homogeneous` | generalist (70/70/70) | generalist (70/70/70) | generalist (70/70/70) |
| `overconfident` | baseline A | baseline B | baseline C, plus instruction to always bid at confidence 95 or higher |

Ability order is calculation/writing/coding. Normal contractors are prompted
to start from the relevant ability score, to consider weaknesses in every
required ability for a mixed-skill task, and to give a short reason. The task
set, announcement format, manager policy, call order, model, and temperature
remain fixed across conditions.

To reproduce after starting the same model in LM Studio:

```powershell
$env:LMSTUDIO_BASE_URL = 'http://127.0.0.1:1234'
$env:AGENT_MODEL = 'qwen/qwen3.8-27b'
$env:AGENT_TEMPERATURE = '0.2'
python runner.py --condition baseline --runs 3 --tasks tasks.json --results results.csv --logs logs
python runner.py --condition homogeneous --runs 3 --tasks tasks.json --results results.csv --logs logs
python runner.py --condition overconfident --runs 3 --tasks tasks.json --results results.csv --logs logs
python -m unittest -v test_contract_net.py
```

## 2. Results

Each condition ran three times on the same six tasks. The table below copies
the formal records in `results.csv`; the earlier dry-run files are not included.

| Run | Condition | Tasks | Correct | Messages | Unassigned | Misawards | Parse failures |
|---|---|---:|---:|---:|---:|---:|---:|
| baseline-01 | baseline | 6 | 6 | 35 | 0 | 0 | 0 |
| baseline-02 | baseline | 6 | 6 | 35 | 0 | 0 | 0 |
| baseline-03 | baseline | 6 | 6 | 34 | 0 | 0 | 0 |
| homogeneous-01 | homogeneous | 6 | 1 | 42 | 0 | 5 | 0 |
| homogeneous-02 | homogeneous | 6 | 2 | 42 | 0 | 4 | 0 |
| homogeneous-03 | homogeneous | 6 | 3 | 42 | 0 | 3 | 0 |
| overconfident-01 | overconfident | 6 | 4 | 37 | 0 | 2 | 0 |
| overconfident-02 | overconfident | 6 | 5 | 38 | 0 | 1 | 0 |
| overconfident-03 | overconfident | 6 | 4 | 38 | 0 | 2 | 0 |

| Condition | Mean correct / 6 | Mean messages | Mean unassigned | Mean misawards |
|---|---:|---:|---:|---:|
| baseline | 6.00 | 34.67 | 0.00 | 0.00 |
| homogeneous | 2.00 | 42.00 | 0.00 | 4.00 |
| overconfident | 4.33 | 37.67 | 0.00 | 1.67 |

There were zero parse failures across 162 contractor calls (9 runs × 6 tasks ×
3 contractors). The message count differs because a `bid=false` response is
not counted as an accepted bid message, while every announcement and award is
always counted.

## 3. Smith (1980) comparison

This is a protocol-level comparison, not a claim to reproduce the original
distributed problem-solver environment in full. The Smith column follows the
course README's description: a manager allocates distributed work by
announcement, contractor bid, and award, with a contractor computing its bid
from a fixed rule. The reproduction changes the bid computation to an LLM's
self-judgment.

| Dimension | Smith (1980), as framed in the course | This reproduction |
|---|---|---|
| Nodes | A manager and distributed contractor/problem-solver nodes | One Python manager and three LLM-backed contractors (A, B, C) |
| Task announcement | The manager announces a task for negotiation | The manager sends the same `TASK_ANNOUNCEMENT` JSON to all three contractors |
| Bid production | A contractor computes a bid by a fixed rule | A contractor reads the task, its ability profile, and calibration prompt, then self-reports JSON `bid`, `confidence`, and `reason` |
| Bid honesty / reliability | Fixed bid rules make the criterion inspectable and can tie a bid to known local state | No mechanism verifies that confidence equals real capability. The parser only verifies JSON structure and the threshold rule. The overconfident condition deliberately violates calibration while remaining syntactically valid. |
| Allocation quality | Whether the negotiated allocation assigns work to an appropriate capable node | `correct` means the awarded contractor matches the precommitted `gold` label; `misawards` are awards to another contractor; `unassigned` records no valid bid |
| Negotiation cost | Communication required for announcements, bids, and awards | Per task: three announcements, zero to three accepted bids, and one award if assigned. The observed means were 34.67, 42.00, and 37.67 messages per six-task run. |
| Failure modes | Rule coverage or local information can be inadequate for a task | Confidence ties interact with fixed A→B→C ordering; generalists can all bid equally; an overconfident agent can dominate specialist bids; malformed JSON would become no bid (none occurred here). |

## 4. Interpretation

Specialist prompts produced perfect allocation in all baseline runs (mean
6.00/6) with the lowest mean communication cost (34.67 messages), so judged
bids helped when the ability profiles were distinct. For example,
`logs/baseline-01.txt` records awards A, B, C, A, B, and C for tasks 01–06,
matching all six gold labels. When all profiles became homogeneous, quality
fell to 2.00/6 and misawards rose to 4.00 per run even though no task was left
unassigned. The first homogeneous run shows the mechanism: task-02 is awarded
to A at confidence 80 although gold is B, and task-03 is awarded to A at
confidence 80 although gold is C (`logs/homogeneous-01.txt`, award lines
137 and 205). Equal self-reported confidence therefore exposed a deterministic
tie-order bias, while all three bids increased the message total to 42.00.
Overconfidence produced an intermediate 4.33/6, but it showed the central
honesty weakness more directly: in `logs/overconfident-01.txt`, C reports 95
for the mixed calculation/writing task-04 and wins it although gold is A
(lines 272–273); C likewise wins task-05 although gold is B (lines 340–341).
The protocol accepts those bids because they are well-formed and meet the
threshold; it has no check that a high LLM confidence represents the relevant
abilities. Thus this implementation's failure is not failed message parsing
(zero cases), but strategically or prompt-induced unreliable confidence.
