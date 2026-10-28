# How `find_sharp_money.py` works

It reads one slate of combined betting splits and writes the games where the money and the line disagree with the public. Default input is `output/mlb_betting_splits.json`. Default outputs are `output/mlb_sharp_money.json` and `output/mlb_sharp_money.csv`.

Moneyline is the default market. `--market spread`, `total`, `both` (moneyline + spread), or `all` (all three) change that.

## What it looks for

On each side of a market, **gap = handle % − public %**. A positive gap means a larger share of the dollars than of the tickets: the side bigger bets are on.

A play qualifies only when three things are true:

1. The active sources agree on which side has the positive gap.
2. The weighted gap on that side is at least 15.
3. The line moved toward that side and away from the public side (reverse line movement).

Games that fail step 1 are dropped. Games that pass step 1 and fail step 2 or 3 are still written, with `qualified: false`, so a later review can see why they missed.

## Sources by league

| League | Who votes | Primary feed |
| --- | --- | --- |
| WNBA, UFC | DraftKings only | `public_bet_pct` / `handle_bet_pct` |
| MLB | PlayerProps + SportsBettingDime + ScoresAndOdds | same fields, plus `sbd_*` and `sao_*` |
| NBA, NHL, NCAAF, NFL | DraftKings + SportsBettingDime + ScoresAndOdds | same fields, plus `sbd_*` and `sao_*` |

`CFB` is treated as `NCAAF`. SportsBettingDime and ScoresAndOdds are not scraped for WNBA or UFC, so those slates qualify on the single DraftKings vote. Set `SAO_VOTES = False` to drop ScoresAndOdds from the vote.

If DraftKings (or PlayerProps, for MLB) is null on every game, the league does not wait on that feed. SportsBettingDime and ScoresAndOdds both have to exist and agree. The public side then comes from SportsBettingDime, then ScoresAndOdds. Plays and alerts say `DK splits down: SBD + SAO` (or PlayerProps, for MLB).

VSiN gaps are still copied onto the output row (`vsin_gap`). `sao_gap` is copied the same way, whether or not ScoresAndOdds voted. VSiN does not vote. Pinnacle is not a source.

Weights, applied only to sources that agree: primary `1.0`, SportsBettingDime `0.75`, ScoresAndOdds `0.75`. The default composite is a weighted **average** of the gaps that agreed (`COMPOSITE_MODE = "average"`), so a third source does not inflate the number. `COMPOSITE_MODE = "sum"` is the previous weighted sum. The threshold stays 15 either way. A missing source is not a vote against, but it also does not count, so one source alone cannot qualify MLB/NBA/NHL/NCAAF/NFL.

Any source that votes the other side throws the game out (`REQUIRE_UNANIMOUS_DIRECTION`).

## Line movement

Public side is whichever side has the higher primary `public_bet_pct`. When the primary feed is null for the whole slate, that comparison uses SportsBettingDime's public %, then ScoresAndOdds. The script then asks which side the number moved toward. Reverse line movement is confirmed only when that side is the sharp side (the line moved against the public).

First complete source wins. Sources are never mixed into one open/live pair.

1. **EV Analytics**, when `eva_open` → `eva_line` actually moved.
2. **TheSpread** open → live, if that pair is complete.
3. **Polymarket** history (first point → last point), if liquidity is at least $10,000.
4. EV Analytics again if it has any prices but no detected move (the move is then missing).
5. Polymarket even when liquidity is under $10,000.

How “moved toward” is read:

- **Moneyline.** American odds converted to implied probability. The side whose price shortened more is the side the line moved toward.
- **Spread.** The point number. A spread that gets more negative (or less plus) moved toward that team. If the number did not move, TheSpread falls back to the juice (`open_odds` → `live_odds`). EV Analytics does not use that juice fallback.
- **Total.** A rising total moved toward Over. A falling total moved toward Under. Same juice fallback, TheSpread only.

Totals are stored internally as away = over and home = under, then labeled `over` / `under` on the way out.

A negative total, or a spread at ±100 or beyond, is treated as an American price that leaked into the line field and is ignored.

If the spread or total moved more than 1 point, `line_move` is added to `reject_reasons`. That does not disqualify the play.

## Tiers

| Tier | Rule |
| --- | --- |
| A | Every active source agrees, composite gap ≥ 15, reverse line movement confirmed |
| B | Coded for a slate with 3+ sources where all but one agree. Unanimous direction drops any opposing vote before tiering, and a silent source is not that case, so B does not fire. |
| A+ | A Tier A play whose exchange check also passes (below). This is an upgrade, not a separate gate. |

## After a play is found

These steps never remove a play.

**Moneyline confidence flags.** If the sharp side is +200 or longer, `low_volume_dog_flag` is set. If that dog’s spread-market composite is missing, flat, negative, or under 15, `ml_spread_divergence` is set too.

**Exchange confirmation.** Median no-vig fair price across `covers_odds` books, compared with the sportsbook fair. Edge is that difference in percentage points. UFC has no Covers scrape, so Polymarket’s implied probability is the exchange fair instead. Polymarket history confirms exchange line movement when the sharp side’s implied probability rose by more than 1 point.

Tier A becomes **A+** when all three are true: exchange edge ≥ 1.5, Polymarket history confirms, and Polymarket liquidity is at least $10,000.

**What to buy.** The published row stays a moneyline so the card still shows the moneyline splits. If a qualified moneyline’s no-vig fair is at or under 0.48, or above 0.60, `trade_market` is set to that team’s spread (`trade_line` / `trade_odds`). Exactly 0.60 stays a moneyline. If the fair is outside the band and there is no spread number, `trade_market` is `skip`.

A cheap moneyline below the league floor skips both markets, even when a spread number exists. NBA skips under 0.25. NCAAF and NFL (and CFB) skip under 0.20. Exactly on the floor still buys the spread. Other leagues still buy the spread all the way down through 0.48.

**Model confidence** is a 70–100 display score. It starts at 70 (78 for A+), then adds for a large gap, three agreeing sources, exchange edge, exchange line movement, and normal liquidity, and subtracts for a line-source conflict, a longshot dog, moneyline/spread divergence, and low Polymarket liquidity.

## What gets written

`process_slate` walks every game and every requested market, then sorts A+, A, B, then unqualified, each group by composite gap descending.

The JSON file includes config, `play_count` (qualified only), `candidate_count` (everything that had source agreement), and the full `plays` list. The CSV is that same list with `exchange_confirmation` flattened into columns.

Every agreed play is also upserted into `json-data/sharp_candidate_log.jsonl` (or `output/sharp_candidate_log.jsonl` if `json-data/` is not found). The key is league, slate date, event, market, and side, so a re-run replaces the earlier row.

Where both SportsBettingDime and ScoresAndOdds have a number, the run appends `{league, date, event, market, sbd_gap, sao_gap, sbd_public, sao_public}` to `json-data/source_correlation_log.jsonl` (same fallback path). That log is how we tell whether the two feeds are the same population.

Unless you pass `--no-discord`, `--no-x`, or `--no-convex`, the run then posts qualified alerts to Discord, X, and Convex. `--discord-dry-run`, `--x-dry-run`, and `--convex-dry-run` print those payloads without sending them.

## Run it

```bash
python find_sharp_money.py
python find_sharp_money.py --input output/nba_betting_splits.json \
    --out output/nba_sharp_money.json --csv output/nba_sharp_money.csv \
    --market all
```

Paths that start with `output/` are resolved next to this script, not from the shell’s current directory.
