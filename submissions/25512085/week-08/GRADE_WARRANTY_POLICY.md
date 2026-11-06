# Grade warranty and demand-led forum policy

The seller states the certified grade, catalog list and actual price truthfully. The historical defaults are A72h full-refund warranty (the market's permitted maximum), B6h, and C0h with no breakage refund. The local three-SKU new-inquiry pilot below is the only A0 exception. Terms are stated before checkout and preserved in both instant and negotiated signed receipts. Food terms are unchanged. Explicit warranty requirements that cannot be met are rejected without issuing a receipt.

The market rules at https://market.ourolabs.ai/rules cap warranty at72h and state that goods can break within6h after sale. Thus72h is a truthful maximum duration, not a claim of lower defects or additional insured failure probability beyond the six-hour generator. No reviews or votes are required.

B's final floor uses actual unit cost, accrued1%-per-day storage, and expected full-refund probability at least15% (or higher empirical estimate), plus a positive target contribution of at least5% of cost. It applies during negotiation, old-stock markdowns and endgame. An actual full refund still loses the unit cost: positive expected profit is not guaranteed realized profit. No loss-making sale is justified by volume alone.

For small goods, the minimum expected contribution scales between500 and5000 instead of imposing5000 on every cheap unit. B's additional5%-of-cost target still applies. C prices use same-SKU, same-C-grade, zero-warranty settled comparables;6h comparables cannot silently substitute. A/B use full-window comparables (>=6h, economically equivalent under the current generator). Actual cost determines the floor.

Forum posts disclose only product name/SKU and availability. An explicit recent buyer request with matching certified stock can receive a thread reply after the market's5-minute minimum. A general notice requires new inquiries at our own shop, held stock, and a20-minute interval. At most3 SKUs appear; stale requests, absent stock and repeated notices for the same demand are skipped. No price, quantity, grade, warranty promotion or buyer budget is published. No post is reserved until the local outbox append succeeds. Feed observation covers only the latest100posts; no attribution claim follows from likes.

Inbound analytics previously waited for the shared state file lock on the A2A event loop. A bounded single-worker queue now handles those writes outside checkout; original observation timestamps are retained. State remains atomic JSON, with compact encoding to reduce serialization work. Queue saturation skips only analytics and never blocks checkout.

Buyer behavior cohorts and strategy research remain advisory. No customer-specific pricing/selection rule or buyer algorithm is activated by this change.

Before/after history: history/20261105-grade-warranty/. Buyer code, keys, identity and inventory are not replaced. The week08 checker is absent; use the offline regression suite and staged VM checks before seller-only restart.

## Local three-SKU new-inquiry pilot (2026-11-07, NOT deployed)

Applies only to G021aae64 / G0be9ed07 / G81ade83e, and only within the
historically selected SKU. Other SKUs, food, procurement and forum policy are unchanged.

| Public buyer request | New quote |
| --- | --- |
| No grade/warranty/quality/refund/certificate condition mentioned | Prefer certified C0 if held; otherwise historical selection and warranty |
| Explicit A only, including `A인가요?`, with no warranty or ambiguous quality/refund demand | Disclose A0/no breakage refund as the first quote; offer an explicit 72h option |
| Explicit grade, minimum grade (including B 이상), or warranty duration | Filter by those requirements; never substitute an unacceptable grade or duration |
| Ambiguous quality/refund/service/certificate request | Historical high-grade, warrantied fallback; if unavailable, reject without receipt |
| Invalid/conflicting/unsupported explicit grade or warranty syntax | Reject without receipt; a clarified new inquiry is required |

Silence is NOT evidence that hidden checkout acceptance permits C0 or A0. These
are public quotes, not a claim of guaranteed payment. No hidden acceptance rules
are bypassed. C remains refurbished and A remains A, with actual certificate and
disclosed duration; A0 must never be described as C refurbished.
Natural warranty durations include hours/days (시간/일); zero is an explicit
no-warranty condition, and negative/nonfinite/over-72h requirements are rejected.

A0 has zero paid-refund probability (`break_rate=0`). Its opening and concession
targets use the existing A fallback calculations, NOT A72 settled comparables.
Its reserve is the maximum of the historical A reserve, the actual zero-warranty
cost/storage/contribution floor, and the historical default-warranty floor as a
separate conservative guard. No automatic no-warranty discount or invented cost,
fee or discount is introduced. Without comparable evidence this can leave A0 and
A72 at the same price. A72 may use its own existing comparable path, so no claim
is made that A0 is always cheaper; a requested option receives a separate quote.

An initial numeric offer cannot instantly accept a not-yet-disclosed A0 default.
The first response quotes A0 and one actionable price, not two option prices.
Only an explicit `choose 72h warranty` / `72h 보증 선택` requests a refreshed A72
quote; that turn issues no receipt even if it contains a price. A later numeric
acceptance keeps the refreshed terms. Bare warranty requirements that exceed the
stored duration are rejected, even without a price. Existing A72/B6/C0/custom
stored commitments are never downgraded by a numeric response or a new default.

Independent-review hardening: a recognized grade token in rejection language
(e.g. `grade C is not acceptable`, `C급 말고`) is not a positive grade request.
Unsupported negated grade conditions reject without a receipt on initial and
later turns; the buyer must clarify rather than have the seller infer a complement.
`보증 최소 72시간` and `warranty of at least 72h` retain their minimum duration.
Other duration-bearing warranty syntax that is not parsed is rejected, never
reduced to "some warranty" or satisfied with B6 instead of the requested minimum.

The A72 choice is a full positive-command allowlist (`choose/select 72h warranty`,
optional `I`, or `72h/72시간 보증 선택`, optionally `합니다/할게요`; an optional
trailing `offer NUMBER` still only requests a fresh quote). A question mark,
negation, uncertainty or additional unsupported prose is not selection consent.
For a 72h option-related reply that fails this allowlist, including `72h 보증 선택
안 함`, retain the entire A0 session unchanged and ask for the explicit command.
Do not reprice, reset rounds, consume a numeric offer, sign, or release its unit.

Selection, opening, concession, final receipt and completion text use the same
stored unit/grade/warranty; the existing live actual-cost floor remains binding.
Receipt issuance is not payment confirmation. No VM access/deployment, market
RPC, posting, procurement, blanket discount or existing log edits occurred.
