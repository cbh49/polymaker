# How fair-value EV comparison works

This is the comparison engine only: sportsbook quotes in, a fair probability out, then Kalshi / Polymarket asks scored against that fair. It does not describe scraping, matching players, or picking which Kalshi ticker is attached to a row.

Code lives in `src/ev_trading/fair_value/`. Config is `config/fair_value.json`. Output is `output/ev/nfl_fair_value.json`.

## What the system is trying to do

Sportsbooks are **reference**. We never want to bet DraftKings or FanDuel from this stack. They exist to answer: *what probability would a sharp-ish two-way market assign to this event?*

Kalshi and Polymarket are **tradable**. For each of their contracts we ask: *if I buy this side at the ask, after fees, is my fair probability high enough that EV is positive?*

A row is +EV when:

```
fair_prob(side) - ask - taker_fee  >=  min_edge
```

Default `min_edge` is 1% (`min_edge_pct`). Ranking is `fee_adjusted_edge × confidence`, not raw edge alone.

We always price the **ask** (what you pay to buy). Never the bid/ask midpoint.

On Kalshi:

- Buying YES uses `yes_ask`.
- Buying NO uses `no_ask`, or `1 - yes_bid` when `no_ask` is missing.

That is why Kirk Cousins `yes_bid: 0.13`, `yes_ask: 0.19` matches a Kalshi UI of YES 19¢ / NO 87¢. The 13¢ is the YES bid; NO ask is `1 - 0.13`.

On Polymarket:

- Buying Over uses `over_ask` (Gamma `bestAsk` on the Over token).
- Buying Under uses `under_ask`, or `1 - over_bid` when the under ask is missing.
- Gamma `outcomePrices` are mids. They always sum to 1 and are **not** tradable. A 48¢-wide book can print Over 26¢ mid while the ask is 50¢.

## Flow

```
sportsbook quotes (per book, per market)
        │
        ▼
  1. convert odds → implied probs, then de-vig
        │
        ▼
  2. build a fair P(over / home / yes) at a strike
        │     same-strike average  OR  distribution fit, then P(X > venue_line)
        ▼
  3. attach that fair p to each Kalshi / Polymarket side
        │
        ▼
  4. edge = fair - ask - fee
        │
        ▼
  5. keep rows with edge ≥ min_edge; split liquid vs thin; rank
```

Sportsbook-only gaps (DK vs consensus, etc.) go into `informational` and are marked **not actionable**.

## Step 1 — De-vig each book

Each book’s two-way quote still has juice. `-110 / -110` is not 50/50; both sides imply ~52.4%.

Default method is **multiplicative**: take the two implied probabilities and divide each by their sum so they add to 1. Shin is implemented but not the default.

One-sided markets (anytime TD, first TD) have no under. Those get a flat haircut: `p / 1.05` (`one_sided_overround`).

Books are not equal. Circa is weighted 1.5, Pinnacle 2.0, most others 1.0. The consensus probability is a **weighted average** of de-vigged book probabilities, not an equal-weight average.

After this step every book is a point: `(line, fair_over, weight)`.

## Step 2 — Fair probability at a strike

This is the whole game, and where same-line vs different-line markets diverge.

### Same strike

If every book’s line is within `0.26` of each other **and** the Kalshi/Polymarket line is also that close, we do **not** fit a distribution. Fair p is just the weighted average of the de-vigged overs (or homes, or yeses).

This is the right model for:

- moneylines
- most spreads when everyone is at -3.5
- anytime TD / first TD / 2+ TD (`same_strike` in config, always)

Example: books at -3.5, Kalshi at -3.5, de-vigged home probs 0.50–0.54 → fair home ≈ 0.52. Compare that to Kalshi’s home ask.

### Different strike

Kalshi player props are usually a fixed threshold (`225+`, `10+`) that is **not** the sportsbook main. Passing-yard books might sit at 211.5 / 213.5 / 218.5 while Kalshi is 224.5.

Then the engine tries to turn the book points into a distribution over the stat `X` and evaluate **P(X > venue_line)**.

Per-stat family (`stat_distributions`):

| Stat | Model | Meaning of fair p |
|---|---|---|
| passing / rushing / receiving yards, totals, spreads | Normal | P(X > line) from Φ |
| receptions | Negative binomial (falls back to Poisson if σ² ≤ μ) | P(X > line) |
| INTs, pass/rush/rec TDs | Poisson | P(X > line) |
| ML, anytime TD, first TD, 2+ TD | Same-strike average | no line translation |

**Normal fit (the yardage path):**

Books give pairs `(line, P(over))`. Convert `P(under) = 1 - P(over)` to a z-score, then regress:

```
line ≈ μ + σ z
```

If that regression is usable (enough z-variance, σ inside per-stat bounds), σ is **fitted** from the books. Fair at Kalshi’s line is `1 - Φ((kalshi_line - μ) / σ)`.

If the books are all on the **same** number, σ is not identified. Slope is ~0. The code then **falls back**:

1. Take this market’s mean book line as a location.
2. Invent a σ.
3. Shift μ so the model still matches the observed P(over) at that book line.
4. Evaluate P(over) at the Kalshi/Polymarket line anyway.

That fallback σ is the fragile part. See “Known holes” below.

The pipeline only takes the distribution path when book lines are clustered **but** the venue line is a different strike. Same books + same venue line → same-strike average, no σ at all.

## Step 3 — Map fair p onto a tradable contract

For an over/under prop:

| Contract side | Fair used | Price used |
|---|---|---|
| over / YES | P(X > venue_line) | yes_ask (Kalshi) or over_ask (Polymarket) |
| under / NO | 1 − that | no_ask, or `1 - yes_bid` / `1 - over_bid` |

Spreads: fair is P(home covers); away is `1 - fair`. Moneylines: P(home wins) vs P(away wins), each Kalshi ticker priced as its own yes/no.

`line_delta` is `venue_line - consensus_line`. It is **not** the edge. It only feeds confidence (bigger gap → lower confidence) and, recently, a side filter.

### Side filter (over/under only)

If the venue line is materially worse for a side, and σ was **not** fitted from multiple book strikes, that side is not emitted:

- Kalshi line **higher** than the book main → over is the worse number, under is the better number.
- Kalshi line **lower** → under is worse, over is better.
- “Material” means `|Δline| / max(|book_line|, 1) > 1.0` (`unfitted_max_rel_delta`).

So Cousins books at 0.5 vs Kalshi 9.5: `|9|/0.5 = 18` → **do not even price the over**. The under can still be priced if NO is cheap enough.

A nearby passing alt (213 vs 224.5) is only ~5% relative, so both sides still get priced. If books actually identify σ (`sigma_source = fitted`), both sides are priced even on a large gap.

This filter does **not** apply to moneylines, ATD, or spreads.

## Step 4 — Edge and fees

```
raw_edge              = fair_prob - ask
fee_adjusted_edge     = fair_prob - (ask + fee)
EV per contract       = 1 * fair_prob - (ask + fee)   # same as fee-adjusted
```

Kalshi taker fee (July 2026 general schedule):

```
fee ≈ round_up(0.07 × P × (1 − P))   per contract, in dollars
```

Peak near 50¢ (~1.75¢ per contract), small in the tails. Polymarket currently uses 0 bps + a small gas assumption spread over 10 contracts.

A 19¢ yes with fair 0.39 looks like +20¢ of raw edge **before** asking whether 0.39 is a sane fair.

## Step 5 — What gets into the report

Keep if `fee_adjusted_edge >= 1%`.

Split:

- `tradable` — volume ≥ 50 or liquidity ≥ 100 (Kalshi often only has volume).
- `tradable_low_liquidity` — same +EV math, thin book. Fill is not assumed.
- `informational` — sportsbook vs consensus, `actionable: false`.

Sort tradable by `rank_score = fee_adjusted_edge × confidence`.

Confidence is a 0–1 blend of:

| Input | Weight | Idea |
|---|---|---|
| number of books | 0.30 | more books → better |
| fit quality | 0.25 | R², or book agreement on same-strike |
| venue depth | 0.30 | volume / liquidity |
| line delta | 0.15 | farther from the main → less trust |

`low_confidence` is a separate flag: fewer than 4 books, bad R², or σ was the fallback default rather than fitted.

Low confidence does **not** drop the row. It only cuts the rank score and sets the flag.

## Worked pictures

### A. Same-strike spread (this is what the model is good at)

Books all at CIN -3.5, de-vigged home ~52%. Kalshi home ask 51¢. Fair 0.52 − 0.51 − fee ≈ small +EV home, or nothing. No distribution, no invented σ. You are comparing **the same number** at two prices.

### B. Nearby passing alt (intended different-strike path)

Books 211.5–218.5, consensus ~213. Kalshi `225+` (line 224.5). Books disagree enough, or σ fallback is in the same order as passing yards (~60). P(over 224.5) comes out a bit under 50%, say ~42%. If Kalshi yes is 20¢, that can be a real +EV over on a slightly harder number. If yes is 42¢, it is roughly fair.

### C. Cousins rush 10+ (the failure mode)

Sportsbooks: rush yards **0.5**, under juiced (de-vigged over ~43–45%).

Kalshi: **10+** (line 9.5), yes 19¢, no 87¢.

What we *want*: 9.5 is a much harder over than 0.5. The interesting side, if any, is **under 9.5**, and only if NO is mispriced. Taking over 9.5 because 0.5 is a 45% over is backwards line shopping.

What the distribution fallback did before the side filter: books don’t identify σ, so it plugged in `default_sigma.rushing_yards = 32` (a workhorse-RB residual). Then

```
P(yards > 9.5 | μ ≈ 0.5, σ = 32) ≈ 0.39
```

19¢ vs 39% → fake +EV **over**. That is the row in `nfl_fair_value.json`.

Why 32? It is a single absolute SD for the *stat name* `rushing_yards`, not for this player. A 0.5 QB and a 100.5 RB share that prior. That is not a real football distribution.

## Config that actually changes the comparison

In `config/fair_value.json`:

| Knob | Role |
|---|---|
| `devig_method` | multiplicative vs Shin |
| `book_weights` | who pulls consensus p |
| `stat_distributions` | same-strike vs normal vs Poisson vs nbinom |
| `default_sigma` | absolute fallback SD when the fit fails (32 rush, 62 pass, 13.5 spread, …) |
| `typical_line` | **hack** to shrink that fallback SD toward this market’s mean (`σ × \|μ\| / typical`). 65 rush / 220 pass / 45 rec are not lines we expect every player to have. They are a stand-in for “the player these default SDs were imagined for.” |
| `sigma_scale_floor` | floor after that shrink (rush 8, pass 20, …) so a 0.5 line is not σ ≈ 0 |
| `sigma_bounds` | if fitted σ is outside this range, throw it away and use the fallback |
| `unfitted_max_rel_delta` | skip the worse O/U side when the venue line is this far (relative) and σ isn’t fitted |
| `same_strike_line_tolerance` | 0.26 — “same number” vs “need a distribution” |
| `min_edge_pct` | 1.0 — report cutoff |
| `min_volume` / `min_liquidity` | liquid vs low-liq bucket |
| Kalshi / Polymarket fee blocks | subtracted from edge |

Spreads and totals do **not** use `typical_line`. Game residual (margin SD ~13.5) is not a fraction of -3.5 vs -7. Player props do, which is why a QB 0.5 rush and an RB 100.5 rush get forced through the same `rushing_yards` bucket.

## What the JSON row means

On a tradable row:

- `fair_prob` — model P(this side wins).
- `fair_line` — sportsbook consensus strike.
- `market_line` — Kalshi/Polymarket strike actually being priced.
- `market_price` — ask you would pay.
- `fee_adjusted_edge` / `edge_pct` — the +EV number after fees.
- `line_delta` — `market_line - fair_line`, not edge.
- `low_confidence` — fit/fallback/book-count warning.
- `rank_score` — edge × confidence; this is sort order.

If `fair_line` is 0.5 and `market_line` is 9.5 and `side` is `over`, the engine is claiming P(over 9.5) is `fair_prob`. That claim is only as good as the σ it used to walk from 0.5 to 9.5.

## Known holes in the comparison (not in data ingest)

1. **Fallback σ is per stat type, not per player.** `rushing_yards → 32` is an RB-sized residual. It does not know Cousins is a 0.5-yard market and Derrick Henry is a 100-yard market. `typical_line` tries to rescale that 32 by `|this_line| / 65`, which still assumes “65 yards is what 32 was built for.” It is not a model of this player.

2. **`sigma_bounds` are the same kind of global.** Rushing fitted σ must land in `[15, 55]` or it is discarded. A QB market cannot produce a 15–55 yard SD. The fit always fails, and we hit the fallback.

3. **One-strike books cannot identify a distribution.** If every book is at 0.5, we know P(X > 0.5), not P(X > 9.5). Any number we put on 9.5 is an assumption. The side filter now refuses to *recommend the over* in that situation; it does not magically know the true 10+ rate.

4. **Favorable long-shot unders can still look +EV** if the tail model is tight. Under 9.5 at 87¢ may or may not show depending on the fallback σ. That is a pricing assumption, not a quoted market at 9.5.

5. **Confidence does not veto.** A garbage tail with `low_confidence: true` still appears if fee-adjusted edge clears 1%. Rank is reduced, not zeroed.

6. **Spreads vs player props share the same Normal machinery** but not the same meaning of σ. Spread σ is “points of margin.” Rush σ is “yards this player might vary.” Lumping them as “Normal with a default σ” is why the player-prop fallback is so sensitive to the table in config.

The comparison we actually trust today is: **same (or very close) strike, de-vigged sportsbook p vs Kalshi/Polymarket ask.** Different-strike player props are an extrapolation layer on top of that, and that layer is only as good as the σ we assign to *this* market.
