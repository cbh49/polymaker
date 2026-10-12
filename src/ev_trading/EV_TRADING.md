# EV trading: how we find +EV NFL opportunities

Both `@polymaker-deploy/src/ev_trading/` paths are the same directory. This file is the package map and the walkthrough of the path that actually produces +EV rows.

A shorter, older note lives at `fair_value/HOW_EV_COMPARISON_WORKS.md`. It covers the comparison math only. Fallback σ is now chosen from **role buckets** (a 0.5-yard market and a 100-yard market no longer share one prior). Trust this file for current behavior.

## What the system is trying to do

Sportsbooks are the **reference**. We do not bet DraftKings or FanDuel from this stack. Their quotes answer: *what probability would a sharp-ish two-way market assign to this event?*

Kalshi and Polymarket are **tradable**. For each of their contracts we ask: *if I buy this side at the ask, after fees, is my fair probability high enough that expected value is positive?*

A row is +EV when:

```
fair_prob(side) − ask − taker_fee  ≥  min_edge
```

The report cutoff is **1%** (`min_edge_pct` in `config/fair_value.json`). Ranking is `fee_adjusted_edge × confidence`, not raw edge alone. Execution is stricter: confidence ≥ 0.80 and fee-adjusted edge ≥ 5%, and only the liquid non-TD `tradable` bucket.

We always price the **ask** (what you pay to buy). The bid/ask midpoint is never the price we compare against.

## Two systems share this package

| System | Entry | Status |
|---|---|---|
| NFL fair value | `ev-trading nfl-ev` | The live +EV finder. Sportsbooks → fair probability → Kalshi / Polymarket asks. |
| Cross-venue quotes | `ev-trading fetch` / `compare` / `run` | Scaffold. Venue adapters return empty quote lists. Joins Polymarket, Kalshi, ProphetX, and SX Bet by normalized question text and flags mid gaps ≥ 3%. |

The rest of this document is the NFL fair-value path.

## Directory map

```
src/ev_trading/
  cli.py                  ev-trading commands
  config.py               [ev] block in config.toml (cross-venue knobs + API key slots)
  pipeline.py             fetch quotes → group → write output/ev/
  edge.py                 cross-venue mid-gap rows
  match.py                normalize question + outcome into a join key
  models.py               Venue, VenueQuote, CrossVenueRow
  nfl_odds.py             team/player name maps, median line, closest-alt pick
  nfl_refresh.py          scrape → aggregate → score, in one command

  supporting-lines/
    scrape_nfl_odds.py        RotoWire game odds (ML, spread, total, plus halves/quarters)
    scrape_nfl_prop_lines.py  RotoWire player props
    aggregate_nfl_odds.py     join books + Kalshi + Polymarket onto one slate
    nfl_game_odds.json        scraped game odds
    nfl_player_props.json     scraped props
    nfl_aggregated_odds.json  the file the pricer reads

  venues/
    base.py, __init__.py      VenueClient protocol; registry
    polymarket.py             Gamma wrapper; fetch_quotes returns []
    kalshi/api.py             signed Kalshi HTTP client
    kalshi/kalshi_nfl.py      this week's NFL series → nfl_week_markets.json
    kalshi/client.py          cross-venue stub (returns [])
    prophetx/, sxbet/         cross-venue stubs (return [])

  fair_value/
    pipeline.py           walk the aggregated slate, emit priced sides
    devig.py              multiplicative (default) and Shin vig removal
    consensus.py          same-strike average, or Normal / Poisson / NB fit
    key_numbers.py        NFL spread translation (extra mass on 3 and 7)
    freshness.py          kickoff clock and book-vs-venue age gap
    clv.py                closing-line value from the last pre-kickoff scrape
    tradable_pricer.py    asks, Kalshi + Polymarket fees, edge
    confidence.py         0–1 score from books, fit, depth, line gap
    report.py             split liquid / thin / TD / informational / rejected
    models.py             BookPoint, FittedDistribution, PricedOpportunity
    config.py             defaults; overlay is config/fair_value.json
    ev_ledger.py          Convex claim so the same side is not bought twice
    kalshi_execute.py     IOC buys for high-confidence Kalshi rows
    polymarket_execute.py FAK buys for high-confidence Polymarket rows
```

Polymarket odds are scraped outside this package, by `data-aggregation/scrape_polymarket_odds.py`, into `data-aggregation/output/polymarket_nfl_odds.json`. `nfl_refresh.py` calls that script.

Config that changes the comparison: `config/fair_value.json`. Report output: `output/ev/nfl_fair_value.json`.

## End-to-end flow

```
RotoWire games ─┐
RotoWire props ─┼─ parallel ─► aggregate onto one JSON per game
Kalshi week    ─┤                 (books stay individual;
Polymarket NFL ─┘                  one Kalshi + one Polymarket line each)
                        │
                        ▼
              de-vig each book
                        │
                        ▼
         fair P at the venue's strike
         (weighted average, or a fitted distribution)
                        │
                        ▼
         edge = fair − ask − fee
                        │
                        ▼
         drop bad sides and bad extrapolations
                        │
                        ▼
         keep edge ≥ 1%, split buckets, rank by edge × confidence
```

Run it:

```
ev-trading nfl-ev --refresh
```

`--refresh` re-scrapes RotoWire, Kalshi, and Polymarket, rebuilds the aggregate, then scores. Without `--refresh`, scoring uses the existing `supporting-lines/nfl_aggregated_odds.json`.

## 1. Pull the four sources

`nfl_refresh.run_nfl_refresh` starts four jobs in parallel. They do not depend on each other.

| Job | What it writes |
|---|---|
| RotoWire game odds | `supporting-lines/nfl_game_odds.json` — full game, halves, and quarters, every book on the page |
| RotoWire player props | `supporting-lines/nfl_player_props.json` |
| Kalshi NFL week | `venues/kalshi/nfl_week_markets.json` |
| Polymarket NFL | `data-aggregation/output/polymarket_nfl_odds.json` |

RotoWire props that enter the join:

| RotoWire id | Canonical stat |
|---|---|
| `passyds` | `passing_yards` |
| `rushyds` | `rushing_yards` |
| `recyds` | `receiving_yards` |
| `rushrec` | `rushing_receiving_yards` |
| `anytd` | `anytime_td` |
| `firsttd` | `first_td` |
| `twotd` | `2plus_td` |

Kalshi series pulled for the week: `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`, plus `KXNFLANYTD`, `KXNFLFIRSTTD`, `KXNFL2TD`, `KXNFLPASSYDS`, `KXNFLRECYDS`, `KXNFLRSHYDS`, `KXNFLRRYDS`. Season-long `KXNFLWINS` is skipped. Public market data does not need auth; signed headers are attached only if a request returns 401.

The fair-value config also knows how to price receptions, interceptions, and TD *counts* (`passing_tds`, `rushing_tds`, `receiving_tds`) if those markets show up in the aggregate. The current scrapers do not emit them.

Halves and quarters are scraped and then ignored. `load_rotowire_games` keeps `period == "game"` only.

## 2. Join everything onto one slate

`aggregate_nfl_odds.aggregate_nfl_slate` is the matcher. Output is `supporting-lines/nfl_aggregated_odds.json`: one object per game, with `markets.moneyline`, `markets.spread`, `markets.total`, and `player_props[]`.

Games join on canonical away/home abbreviations (`JAC`→`JAX`, `LAR`→`LA`, `WSH`→`WAS`, and the same for Kalshi ticker codes). Kalshi games whose subtitle does not match this week's RotoWire slate are dropped.

Each game gets a `kickoff` in UTC. RotoWire `gameDate` (`2026-10-04 13:00:00`) is US Eastern wall time, so 1:00pm ET becomes 17:00 UTC during daylight time. Gamma `startTime` is the backup when `gameDate` is missing. Kalshi `close_time` is resolution time and is not used as kickoff. The aggregate also keeps each source's scrape time (`books_quoted_at`, `props_quoted_at`, `kalshi_quoted_at`, `polymarket_quoted_at`) and copies those onto every priced row.

Players join on a normalized name (punctuation, suffixes like Jr/III stripped). If that misses, a unique first-initial + last-name match is accepted. Defense/special-teams names are skipped.

**Sportsbook lines stay individual.** Each book is its own entry, with American odds and a raw implied probability. Consensus *line* for the join is the **median** of those book lines, not a weighted average.

**Kalshi and Polymarket contribute one contract per market**, not every alternate:

- Yardage props, spreads, and totals: the alternate whose line is closest to the sportsbook median.
- Moneylines and yes/no TDs (no numeric line to match): the contract whose mid is closest to 50¢.
- `line_delta` on the venue quote is `venue_line − median_book_line`. It is a distance, not an edge.

That closest-alt choice is why a Kalshi `225+` passing market can sit next to books at 213.5. The pricer then has to translate probability across that gap. It does not pretend the strikes are the same.

## 3. De-vig each book

Each book's two-way quote still has juice. `-110 / -110` is not 50/50; both sides imply about 52.4%.

`fair_value/devig.py`, default method **multiplicative**: divide each implied probability by the sum of the two so they add to 1. Shin (1993) is implemented and selectable with `devig_method: "shin"`. It is not the default. Inputs may be probabilities in (0, 1] or American odds.

One-sided markets (anytime TD, first TD, 2+ TD) have no under. Those get a flat haircut: `devig_one_sided` with `one_sided_overround = 1.05`, which is `p / (p + (1.05 − p))`.

Books are not equal. From `config/fair_value.json`:

| Book | Weight |
|---|---|
| Pinnacle | 2.0 |
| Circa / Circa Sports | 1.5 |
| DraftKings, FanDuel, MGM, Caesars, BetRivers, Betr, Fanatics, Hard Rock, theScore, and anything else | 1.0 |

After this step every book is a `BookPoint`: `(book, line, fair_over, weight)`. For spreads, `fair_over` means P(home covers). For moneylines, P(home wins). For yes/no props, P(yes).

## 4. Fair probability at the venue's strike

`fair_value/consensus.py`, called from `_fair_at_line` in `pipeline.py`. This is where same-line markets and different-line markets diverge.

### Same strike

If every book line is within `same_strike_line_tolerance` (**0.26**) of the others, **and** the Kalshi/Polymarket line is also that close to the weighted-mean book line, we do not fit a distribution. Fair probability is the weighted average of the de-vigged overs (or homes, or yeses).

Moneylines, anytime TD, first TD, and 2+ TD are forced onto this path (`stat_distributions` = `same_strike`) even when a line is present.

This is the comparison we trust: the same number, two prices.

Example: books all at CIN −3.5, de-vigged home probabilities 0.50–0.54 → fair home ≈ 0.52. Compare that to Kalshi's home ask. No invented σ.

### Different strike

Kalshi player props are often a fixed threshold (`225+`, `10+`) that is not the sportsbook main. Passing-yard books might sit at 211.5 / 213.5 / 218.5 while the closest Kalshi contract is 224.5.

The pipeline takes the distribution path when book lines are clustered but the venue line is a different strike. It turns the book points into a distribution over the stat `X` and evaluates **P(X > venue_line)**.

| Stat | Model | Fair p |
|---|---|---|
| Passing / rushing / receiving / rush+rec yards, totals | Normal | `1 − Φ((line − μ) / σ)` |
| Spreads | Key-number margin table | P(home covers), shifted to the venue line |
| Receptions | Negative binomial, or Poisson if σ² ≤ μ | `P(X > line)` |
| Interceptions, pass/rush/rec TD counts | Poisson | `P(X > line)` |
| Moneyline, anytime TD, first TD, 2+ TD | Same-strike average | no line translation |

**Spreads.** Same-strike spreads (books and the venue within 0.26) stay the weighted average. Off-strike spreads do not use a Normal. `key_numbers.py` builds an integer home-margin distribution with extra mass on 3, 7, 6, 10, 4, and 14, shifts it until it matches the de-vigged cover probability at the book line, then reads P(home covers) at the venue line. An integer book line (−3) is P(cover | no push). Kalshi and Polymarket are strict thresholds, so the push is not a win. `sigma_source` is `key_numbers`. A move wider than 3 points is rejected (`spread_span`) and does not go through the relative-line extrapolation veto. Polymarket spreads are priced at `home_line`, not at the consensus line. Totals still use the Normal.

**Normal fit (yardage and totals).** Books give pairs `(line, P(over))`. Convert `P(under) = 1 − P(over)` to a standard-normal z-score and regress:

```
line ≈ μ + σ · z
```

σ is **fitted** when three things hold: z-variance ≥ `min_z_variance` (0.02), slope σ > 0, and σ lands inside the role bucket's bounds (or the global `sigma_bounds` if the stat has no bucket). Fair at the venue line is `1 − Φ((venue_line − μ) / σ)`.

If the books are all on one number, σ is not identified (slope ≈ 0). The fit then **falls back**:

1. Location is this market's weighted-mean book line.
2. σ comes from the **role bucket** for that line (below), not a single number for the whole stat.
3. μ is shifted so the model still matches the observed P(over) at the mean book line.
4. P(over) is evaluated at the Kalshi/Polymarket line anyway.

`sigma_source` on the fit is `"fitted"` or `"fallback"`. A fallback is an assumption. The veto in step 6 exists because of that.

**Role buckets** (`role_buckets` in config). The bucket is chosen from this market's own consensus line. It is a usage proxy, not snap share.

| Stat | Bucket | Line below | Fallback σ | Fitted σ must land in |
|---|---|---|---|---|
| Rushing yards | low volume | 5 | 6 | 2–10 |
| Rushing yards | committee | 40 | 18 | 8–28 |
| Rushing yards | workhorse | else | 32 | 15–55 |
| Passing yards | backup | 150 | 40 | 20–55 |
| Passing yards | starter | 260 | 55 | 35–80 |
| Passing yards | high volume | else | 62 | 35–90 |
| Receiving yards | low target share | 20 | 10 | 4–16 |
| Receiving yards | moderate | 50 | 18 | 8–28 |
| Receiving yards | high target share | else | 28 | 12–50 |
| Rush + rec yards | low / committee / workhorse | 20 / 70 / else | 12 / 24 / 38 | see config |
| Receptions | low / moderate / high | 2.5 / 6 / else | 1.0 / 1.6 / 2.2 | see config |

Totals have no role bucket. They keep a flat game residual (`default_sigma` 13.5) and bounds `[8, 22]`. Spreads do not use that residual.

**Poisson.** Each book line is inverted for a λ that reproduces that book's P(over). Consensus λ is the weighted average. Fair at the venue line is the Poisson survival function. These fits are labeled `sigma_source = "fitted"` because λ is identified from a single strike; there is no separate σ to invent.

## 5. Map fair p onto a contract, then subtract fees

`fair_value/tradable_pricer.py`.

| Contract | Fair used | Price used |
|---|---|---|
| Over / YES / home | P(over) or P(home) | Kalshi `yes_ask` (or `yes_ask_dollars`); Polymarket `over_ask` / `home.ask` |
| Under / NO / away | `1 −` that | Kalshi `no_ask`, or `1 − yes_bid` when `no_ask` is missing. Polymarket `under_ask`, or `1 − over_bid`. |

Integer Kalshi prices from 1 to 99 are treated as cents. Gamma `outcomePrices` are mids; they sum to 1 and are not tradable. A wide book can print a 26¢ mid while the ask is 50¢. The pricer never reads those mids.

```
raw_edge                  = fair_prob − ask
fee_adjusted_edge         = fair_prob − (ask + fee)
expected_value_per_contract = fair_prob × $1 − (ask + fee)
```

The last two are the same number. A contract pays $1 if the side wins.

Kalshi taker fee (July 2026 general schedule), per contract:

```
fee = round_up_to_$0.0001( 0.07 × P × (1 − P) )
```

It peaks near 50¢ (about 1.75¢) and is small in the tails. Polymarket uses 0 bps plus $0.02 of gas spread over 10 contracts ($0.002 per contract at the default).

## 6. What gets thrown out before the report

Two filters run before a side can become a row. Both live in `fair_value/pipeline.py`.

### Side filter (over/under only)

`_allow_ou_side`. If the venue line is materially worse for a side, and σ was **not** fitted from multiple book strikes, that side is not emitted.

- Venue line **higher** than the book main → the over is the worse number.
- Venue line **lower** → the under is the worse number.
- "Material" means `|Δline| / max(|book_line|, 1) > 1.0` (`unfitted_max_rel_delta`).

Books at 0.5 rushing yards vs Kalshi 9.5: `|9| / 1 = 9` (the denominator floors at 1) → the over is not priced. The under can still be priced if NO is cheap. A nearby passing alt (213 vs 224.5, about 5% relative) prices both sides. If `sigma_source == "fitted"`, both sides are priced even on a large gap.

Moneylines, anytime TD, and spreads do not use this filter.

### Different-strike veto

`different_strike_veto`. Same-strike rows (`fit is None`) are never vetoed. A different-strike row is rejected, with `veto_reason` set, when:

| Reason | When |
|---|---|
| `insufficient_books` | Fewer than 3 books (`min_books_hard_floor`) |
| `fit_quality_too_low` | Fitted σ but R² < 0.4 (`min_r2_for_fitted_sigma`). Also a fallback fit whose R² is under 0.4 **and** the venue line sits well outside the quoted book range. |
| `fallback_sigma_extrapolation` | Venue line is outside the min/max book strike, and that gap divided by `max(|lowest book line|, 1)` exceeds **0.4** (`max_fallback_extrapolation_ratio`). |
| `stale_reference` | Book scrape time is missing, the venue quote time is missing, or the books are more than **10 minutes** older than the venue quote. |
| `spread_span` | Spread translation would walk more than **3 points** off the weighted-mean book line. |

The 0.4 ratio is tuned so a receiving `50+` against books near 85.5 (about 41% relative, past the quoted range) is vetoed, while a `225+` passing alt against books at 211–218 (about 3%) still passes.

Rejected rows are kept on the report under `rejected` with `rank_score = 0`. They are not +EV candidates.

Walking a fitted curve *between* quoted book strikes is interpolation and is allowed. Walking it *past* the quoted range is the extrapolation case above.

## 7. What lands in the report

`fair_value/report.py` → `build_report`. Keep a side when `fee_adjusted_edge ≥ 1%`.

| Bucket | Meaning |
|---|---|
| `tradable` | Liquid, and not a yes/no TD. Volume ≥ 50 **or** liquidity ≥ 100 **or** 24h volume ≥ 50. This is the only bucket the executors will buy. |
| `tradable_low_liquidity` | Same +EV math, thin book. A fill at the ask is not assumed. |
| `tradable_td` / `tradable_td_low_liquidity` | Anytime, first, and 2+ TD (`stat` ends in `_td` but not `_tds`). Split out so TD yes/no does not mix with O/U props. |
| `informational` | A sportsbook's raw implied probability vs the consensus fair, when the absolute gap is ≥ 1%. `actionable` is always false. |
| `rejected` | Failed a veto: different-strike fit, stale quotes, or a spread span over 3 points. |

Sort tradable lists by `rank_score = fee_adjusted_edge × confidence`.

### Confidence

`fair_value/confidence.py`. A 0–1 blend. There is no hidden floor; a bad extrapolation can score 0.

| Input | Weight | Idea |
|---|---|---|
| Number of books | 0.30 | `n_books / 8`, capped at 1 |
| Fit quality | 0.25 | R² when a distribution was fit; otherwise `1 − agreement_std / 0.10` |
| Venue depth | 0.30 | Log scale from the liquidity/volume floor up to 5,000 |
| Line gap | 0.15 | `1 / (1 + |Δline| / 15)`. Extrapolation distance is folded into the same term, and a relative gap (yards past the book range divided by the lowest book line) is added on top. A 7-yard miss on a 7.5-yard market hurts more than a 6-yard miss on a 213-yard market. |

`low_confidence` is a separate flag: fewer than 4 books, R² < 0.75, or σ was the fallback rather than fitted. It does not drop the row. It lowers the rank score and marks the console row with ⚠.

`ev-trading nfl-ev` prints four tables (liquid, thin, TD, TD-thin) plus an informational table, and writes the full JSON including `rejected`.

## 8. A row, in words

On a tradable object:

- `fair_prob` — model probability this side wins.
- `fair_line` — sportsbook consensus strike (median from the join, or the weighted-mean line / fitted μ when that is what the pricer had).
- `market_line` — the Kalshi or Polymarket strike actually priced.
- `market_price` — the ask.
- `fee_adjusted_edge` / `edge_pct` — the +EV number after fees. `edge_pct` is the edge in percentage points.
- `line_delta` — `market_line − fair_line`. Distance between strikes, not edge.
- `sigma_source` — `fitted` or `fallback`. `null` on same-strike rows.
- `role_bucket` — which usage bucket supplied the fallback σ, when one applied.
- `is_extrapolation` / `extrapolation_distance` — whether the venue line sits outside the quoted book range, and by how much.
- `low_confidence` — fit / fallback / book-count warning.
- `rank_score` — edge × confidence. Sort order.
- `ticker` or `market_id` — what an executor would buy.

If `fair_line` is 0.5, `market_line` is 9.5, and `side` is `over`, the engine is claiming `fair_prob` equals P(over 9.5). That claim is only as good as the σ used to walk from 0.5 to 9.5, and the side filter plus the extrapolation veto are there to stop the bad versions of that claim from becoming recommendations.

## 9. Execution is a second, tighter gate

Finding a row and buying it are separate. `kalshi_execute.py` and `polymarket_execute.py` read **only** `report.tradable`. Low-liquidity and TD lists are ineligible.

A row is sent only when all of these hold:

- venue matches the executor
- `confidence ≥ 0.80`
- `fee_adjusted_edge ≥ 5%` (`DEFAULT_MIN_EDGE_PCT` in `ev_ledger.py`; the report itself still uses 1%)
- a ticker (Kalshi) or market id (Polymarket) is present
- the side maps onto YES/NO (`yes`/`over`/`home` vs `no`/`under`/`away`)

Default stake is **$10** per side.

Both executors also refuse the order, including on a dry run, when:

- `kickoff` is missing or unparseable
- kickoff is inside **5 minutes** (`DEFAULT_PREGAME_BUFFER_MINUTES`), or the game has already started
- `books_quoted_at` or `venue_quoted_at` is missing
- the book scrape is more than **10 minutes** older than the venue quote

Polymarket still checks Gamma `startTime` when that field is present. Either clock inside the buffer blocks the order. A missing Gamma time does not override a missing row kickoff.

Kalshi places an immediate-or-cancel order on the YES book: buying YES is a bid at the ask; buying NO is an ask at `1 − no_ask`. Size is the number of whole contracts whose cost at the ask does not exceed $10. The live ask is re-read first; if it has moved more than 1¢ from the report (`DEFAULT_MAX_ASK_SLIPPAGE`), the order is skipped.

Polymarket places a fill-and-kill CLOB buy. The token is resolved from Gamma (outcome name, or team name on a moneyline). The market must still be pre-game.

Both executors claim a Convex ledger key before sending:

```
nfl-ev|{venue}|{market_id}|{side}
```

Polymarket also claims `{slug}|{outcome}` so another bot cannot buy the same CLOB token. If the second claim fails, the first is released. Dry-run does not touch the ledger. Live mode fails closed when Convex is not configured. Fills append to `journal/nfl_kalshi_trades.jsonl` or `journal/nfl_polymarket_trades.jsonl`. Each fill also stores `fair_prob`, `market_line`, `stat`, `player`, `matchup`, `kickoff`, `books_quoted_at`, and `filled_at`.

## 10. Closing-line value

`fair_value/clv.py` runs at the end of `nfl-ev --refresh`. For each fill, it looks up that contract's fair on the fresh slate (including rows that are no longer +EV). It appends `journal/nfl_clv.jsonl` when the book scrape is still at or before kickoff and within **15 minutes** of it:

```
clv = close_fair_prob − fill_price
```

A later pre-kickoff scrape appends again. The latest line at or before kickoff is the close. A book scrape after kickoff is not a close.

## Worked pictures

**Same-strike spread.** Books all at −3.5, de-vigged home ≈ 52%. Kalshi home ask 51¢. Edge is about `0.52 − 0.51 − fee`. No distribution. This is the comparison the model is built for.

**Nearby passing alt.** Books 211.5–218.5, consensus ≈ 213. Closest Kalshi is `225+` (line 224.5). If the books disagree enough to identify σ, or the fallback σ is in the starter/high-volume range (55–62), P(over 224.5) comes out a bit under 50%. A 20¢ yes can be real +EV on a slightly harder number. A 42¢ yes is roughly fair. Relative extrapolation vs a 211 book line is small, so the veto does not fire.

**QB rush yards 0.5 vs Kalshi 10+.** Books at 0.5 cannot identify σ. The role bucket for a line under 5 is `low_volume` with σ = 6, not the old workhorse σ of 32. The over is the worse number and `|Δline|` is far above the side-filter cutoff, so the over is not emitted. The under can still be priced. If the venue line also sits outside the quoted book range by more than 40% of the lowest book line, the different-strike veto drops the row entirely (`fallback_sigma_extrapolation`). One strike tells us P(X > 0.5). It does not tell us P(X > 9.5).

## Knobs that change who shows up

Defaults live on `FairValueConfig` in `fair_value/config.py`. `config/fair_value.json` overrides the keys it contains; a missing key keeps the coded default.

| Knob | Role |
|---|---|
| `devig_method` | `multiplicative` or `shin` |
| `book_weights` | Who pulls the consensus probability |
| `stat_distributions` | `same_strike`, `normal`, `poisson`, or `nbinom` |
| `role_buckets` | Fallback σ and fitted-σ bounds by consensus-line bucket |
| `default_sigma` / `sigma_bounds` | Used when a stat has no role bucket (totals, TD counts). Spreads use the key-number table |
| `min_z_variance` | Below this, the Normal regression is treated as unidentified |
| `min_r2` | Flags `low_confidence` (0.75). Does not drop the row. |
| `min_r2_for_fitted_sigma` | Veto threshold for a weak fit (0.4) |
| `min_books_for_fit` / `min_books_hard_floor` | 4 flags low confidence; 3 vetoes a different-strike row |
| `max_fallback_extrapolation_ratio` | 0.4 — drop fallback walks past the quoted book range |
| `unfitted_max_rel_delta` | 1.0 — skip the worse O/U side when σ is not fitted |
| `same_strike_line_tolerance` | 0.26 — "same number" vs "fit a distribution" |
| `min_edge_pct` | 1.0 — report cutoff. Executors use 5.0 in code. |
| `max_book_venue_lag_seconds` | 600 — reject when books are older than the venue quote by more than this |
| `spread_max_translation_points` | 3.0 — widest key-number spread walk that is still priced |
| `clv_window_minutes` | 15 — how close to kickoff a book scrape must be to count as the close |
| `min_volume` / `min_liquidity` | 50 / 100 — liquid vs low-liq bucket |
| `confidence_weights` | 0.30 / 0.25 / 0.30 / 0.15 |
| Kalshi / Polymarket fee blocks | Subtracted from edge |

## Limits worth remembering

1. **One strike cannot identify a distribution.** If every book is at 0.5, we know P(X > 0.5). Any probability at 9.5 is an assumption. The side filter and the extrapolation veto refuse to recommend the bad side of that assumption. They do not recover the true 10+ rate.

2. **Role buckets are a line heuristic.** A consensus line under 5 rushing yards is treated as low-volume (σ = 6). That is better than one σ for every rusher, and it is still not this player's snap share.

3. **Closest alternate, not every alternate.** The join keeps one Kalshi contract and one Polymarket contract per market: the line nearest the book median (or nearest 50¢ when there is no line). A +EV price on some other Kalshi strike in the same series is invisible until the join is changed.

4. **Confidence does not veto.** A `low_confidence: true` row still appears in the report if fee-adjusted edge clears 1%. Rank is reduced. The executor's 0.80 confidence floor is what keeps it from being bought.

5. **Report edge and trade edge differ.** 1% gets a row into JSON. 5% plus confidence ≥ 0.80 plus the liquid non-TD bucket is what gets an order.

6. **Informational rows are sportsbook-vs-consensus gaps.** They are marked not actionable. This stack does not bet the books.

7. **The cross-venue CLI is not this pipeline.** `ev-trading run` fetches `VenueQuote`s and looks for mid gaps. Those adapters currently return no quotes. NFL +EV does not go through them.
