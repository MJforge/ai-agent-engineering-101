# Three-SKU public terms pilot — local only

## Scope and changed files

- `seller.py`: public requirement parsing/filtering, C0-first only for new unconstrained
  requests within G021aae64 / G0be9ed07 / G81ade83e; explicit A-only A0 first quote
  and explicit A72 refreshed option; fixed stored terms across numeric negotiation.
- `test_goods_terms_pilot_offline.py`: 28 synthetic regression methods, including
  grade/minimum grade, C0 exact selection, A0, A72, ambiguous/invalid requests,
  existing A72 commitment, instant/negotiated receipts, options and protected floors.
- `POLICY.md`, `GRADE_WARRANTY_POLICY.md`: scope, decision table, conservative pricing,
  hidden accept uncertainty, option consent, rejection boundaries and VM non-application.
- This history: snapshot/isolated runners, immutable raw logs, SHA256 manifests,
  isolated syntax/diff verification. Generated evidence trees and bytecode are ignored.
- `logs/goods-terms-local-20261107-01.log`: concise executed-command results.

No buyer/procurement/forum/config files changed. Existing dirty `logs/market-test.log`
and `test-report.json` remain unstaged; original untracked user files are not included.
No operational key/.env/.market/state data was read or copied. No install, market
HTTP/RPC, paid model, VM access/deployment, push or other agent was used.

## Pricing/consent decisions

- A0 is explicitly A, zero warranty, no breakage refund, actual refund probability 0.
- No automatic discount: use historical A fallback asks/targets; do not relabel A72
  settlement prices as A0 comparables. The reserve is `max(historical A reserve,
  actual warranty=0 cost/storage/contribution floor, default-warranty floor as a
  separate conservative guard)`. The default-warranty guard is not A0's actual risk.
- Without comparables, A0/A72 can have equal prices; A72 uses its own existing
  quote path when requested. No cheaper-A0 or guaranteed-contribution claim is made.
- An initial price cannot accept an undisclosed A0 default. First disclose one
  actionable A0 price plus the A72 option. An explicit `choose 72h warranty` or
  `72h 보증 선택` refreshes the quote and never signs on that turn; numeric-only
  responses do not select a new warranty or lower any existing commitment.
- Bare later minimum warranty requirements that exceed stored terms reject even
  without a price. Conflicts/unsupported explicit grade sets reject, not substitute.
- Silence cannot establish hidden buyer acceptance. Ambiguous quality/refund/service
  requests keep a conservative warrantied cohort, or reject if no such stock exists.
  This is a deliberately narrow rules parser, not comprehensive natural-language
  understanding or proof that hidden checkout rules will accept a quote.

## Executed verification and preserved failures

Commands below use `python -B history/20261107-goods-terms-01/check.py LABEL [MODE]`
from this week directory (actual invocations used the repository-relative full path).
Only tracked Python sources, the explicitly authored new test and nonsecret policy/
launcher/kit code are copied. HOME is fake, writes stay in fresh evidence/scratch,
live-repository reads and sockets are blocked. After `fix01`, subprocess execution
is also blocked. No test snapshot overwrites an earlier output.

| Label/mode | Discovered | Failures | Errors | Skips | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline/all | 499 | 0 | 0 | 0 | pass |
| red/all | 519 | 27 | 0 | 0 | expected failures, including subtests |
| fix01/all | 519 | 1 | 0 | 0 | A0 historical floor retention regression |
| focused02/focused | 97 | 0 | 0 | 0 | pass |
| after02/all | 524 | 2 | 0 | 0 | stricter runner blocked existing bash stubs |
| final03/all | 524 | 0 | 0 | 2 | pass, child-process tests explicitly skipped |
| focused04/focused | 99 | 0 | 0 | 0 | pass |
| final05/all | 526 | 0 | 0 | 2 | pass |
| duration-red06/focused | 100 | 6 | 0 | 0 | reproduced ignored days/zero/invalid duration cases |
| duration-green07/focused | 100 | 0 | 0 | 0 | pass |
| final08/all | 527 | 0 | 0 | 2 | **525 executed/pass; 2 skipped** |

The two skipped tests are unchanged `LauncherTests.test_bash_stub_preserves_auth_loop_and_other_python_forwarding`
and `LauncherTests.test_bash_stub_structure_failure_cannot_fall_back_to_vanilla`.
They require bash/child interpreters and are outside this strict no-child-process
seller verification. Their synthetic-stub executions passed in the earlier baseline.

`python -B .../verify_checks.py final08` compiled snapshot `seller.py` and the new
test via `py_compile.compile(..., doraise=True)` to unique ignored output: both pass.
`git diff 92729a6^ --check -- <changed source/docs/runners>`: exit 0.
Whole-change `git diff 92729a6^ --check`: exit 2 solely on immutable raw test/diff
log trailing whitespace. Logs are intentionally not cleaned. This is not reported
as a whole-diff pass. The snapshot digests are in `final08-sha256.json`.

The week-08 assignment README, local README and `scripts/check_week08.py` are absent;
no checker result is claimed. No live checkout/payment or VM verification was run.

## Commits (attempts preserved; no squash)

- `92729a6`: baseline and red regressions.
- `9141b64`: scoped selection/terms implementation; one floor failure retained.
- `8cfd094`: conservative floor/parser fixes and stricter isolation evidence.
- `c0106d2`: explicit option consent, documented policy and verification limits.
- `b4d2741`: failing natural-duration boundary regression.
- `16d9fbf`: duration validation fix; final 100 focused / 525 whole-suite passes.

Independent main-session review remains pending. VM deployment requires separate
approval and has **not** been performed.
