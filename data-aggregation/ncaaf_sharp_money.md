# College Football sharp money

How we combine college football (NCAAF / CFB) betting sources and turn them into sharp-money plays. The finder is [`find_sharp_money.py`](find_sharp_money.py); the combined slate is built by [`scrape_ncaaf_betting_splits.py`](scrape_ncaaf_betting_splits.py).

```
scrape_ncaaf_betting_splits.py
        │
        ▼
output/ncaaf_betting_splits.json
        │
        ▼
find_sharp_money.py --market all
        │
        ▼
output/ncaaf_sharp_money.json
output/ncaaf_sharp_money.csv
```

NCAAF is treated like MLB for splits (three handle/public sources) and like WNBA for the primary book (DraftKings, not PlayerProps). Pinnacle is not published for CFB and is skipped. Markets evaluated: **moneyline, spread, and total**.

```bash
python scrape_ncaaf_betting_splits.py
python find_sharp_money.py \
  --input output/ncaaf_betting_splits.json \
  --out output/ncaaf_sharp_money.json \
  --csv output/ncaaf_sharp_money.csv \
  --market all
```

Slate dates are Pacific (`America/Los_Angeles`). NCAAF keeps a **+6 day window** so weekend boards stay together.

---

## Sources and what they are for

Two groups: **required splits sources** that can qualify a play, and **line / exchange sources** that confirm it. A source with a missing reading is ignored (not a vote against). Opposite-direction votes usually discard the game, except the SBD-override path below.

### Required splits sources (qualify the play)

These three must be on the Pacific slate, and at least one game must have overlapping fields from all three (`slate_alignment.py`: `NCAAF_REQUIRED`). Each source votes for a side when `handle % − public %` is positive on that side.

| Logical name | Site | Weight | Fields on each side | Role |
|---|---|---|---|---|
| `primary` | [DraftKings Network](https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/?tb_eg=NCAA+Football&tb_edate=n7days&tb_emt=0&itm_content=NCAA+Football) | **1.0** | `public_bet_pct`, `handle_bet_pct`, `live` / `live_odds` | Default handle/public source. PlayerProps has no CFB, so DK fills that role. Also defines the **public side** for reverse line movement (`public_bet_pct`). |
| `vsin` | [VSiN CFB splits](https://data.vsin.com/betting-splits/?source=DK&sport=CFB) | **1.5** | `vsin_public_bet_pct`, `vsin_handle_bet_pct`, `vsin_line` | Highest-weighted splits source. Moneyline fair probability prefers `vsin_line` when both sides are present. |
| `sbd` | [SportsBettingDime](https://www.sportsbettingdime.com/college-football/public-betting-trends/) (`/wp-json/adpt/v1/ncaafb-odds`) | **0.75** | `sbd_public_bet_pct`, `sbd_handle_bet_pct`, `sbd_line` | Third splits source. The HTML page may say splits are unavailable; the `ncaafb-odds` API still returns them. Displayed book line only — no open/live for RLM. |

Composite gap on the agreeing side:

```
composite = 1.5 × vsin_gap + 1.0 × primary_gap + 0.75 × sbd_gap
```

Missing gaps among the agreeing set are skipped, not treated as zero.

**SBD-only dissent (override):** if VSiN and DraftKings agree on a side *and* both individual gaps are ≥ `STRONG_SOURCE_GAP_THRESHOLD` (15 pp), but SBD votes the other way, the game is **not** discarded. Composite is recomputed from VSiN + DK only (SBD's gap is dropped from the sum, not zeroed). The play is capped at **Tier B** — Tier A still requires genuine 3-source unanimity. Output flags this path with `sbd_override: true` and `sbd_dissent_gap`. If SBD is missing or flat, existing two-source Tier B still applies (`sbd_override` stays false). If VSiN and DK disagree with each other, the game is still discarded.

### Line-movement sources (reverse line movement)

A play does not qualify unless **RLM confirms** the sharp side: the line moved *against* the public (DraftKings `public_bet_pct`) *and toward* the handle-heavy side.

Priority order (`RLM_SOURCE_PRIORITY`; first complete source wins). TheSpread and Polymarket are preferred over EVA because each is a same-book/same-market comparison — EVA is last resort specifically to avoid splicing `eva_open` against DraftKings `live`.

| Priority | Source | Site | Fields | Role |
|---|---|---|---|---|
| 1 | TheSpread | [NCAA public betting chart](https://www.thespread.com/ncaa-college-football-public-betting-chart/) | `open` / `live`, `open_odds` / `live_odds` | Same-book open → live. Used whenever both sides of the pair are present. NCAAF uses TheSpread for movement **and** SBD for handle/public. |
| 2 | Polymarket | Gamma / CLOB (`cfb-YYYY` series) | per-side `polymarket.history` (`ts`, `line`, `implied_prob_pct`), `liquidity` | Same-market first→last implied-prob move. Preferred over EVA when TheSpread is missing. Skipped as the *primary* RLM source when `liquidity` < `LOW_LIQUIDITY_THRESHOLD` ($10,000); the low-liq reading is still written to the play for review. |
| 3 | EV Analytics | [evanalytics.com/ncaaf/odds](https://evanalytics.com/ncaaf/odds) | `eva_open`, `eva_line`, `eva_odds`, `eva_history` | Last-resort fallback. Timestamped chart history; **no** public/handle %. Never blocks trading. Flagged via `rlm_source_used: "eva"`. |

Every qualifying play includes `rlm_source_used` (`thespread` / `polymarket` / `eva`). When TheSpread is missing and Polymarket and EVA disagree on direction, the play is **not** discarded: `rlm_source_conflict` is true, Polymarket's read is the deciding one (clearing market with liquidity), and EVA's read stays in `eva_line_moved_toward`.

How movement is read:

- **Moneyline** — American odds shortened (implied win probability up). TheSpread `open`/`live`, else Polymarket history implied-prob, else EVA `eva_open`/`eva_line` (never EVA open vs DK live).
- **Spread** — the point-spread number first (more negative = toward that side). If the number is unchanged, juice (`open_odds` → `live_odds`) is used. Polymarket uses implied-prob history on that side, same as moneyline.
- **Total** — a rising number confirms Over; a falling number confirms Under. Juice is the fallback when the total is flat. Polymarket implied-prob history applies here too.

### Exchange sources (enrichment only)

These never filter a play. They attach an `exchange_confirmation` block and can upgrade **Tier A → A+**.

| Source | Site | Fields | Role |
|---|---|---|---|
| Covers | [covers.com NCAAF odds](https://www.covers.com/sport/football/ncaaf/odds) | `covers_odds` (Polymarket, ProphetX, Novig, OG, Crypto.com, DraftKings Predictions) | Median no-vig fair from prediction-market books that have a two-way price. |
| Polymarket | Gamma / CLOB (`cfb-YYYY` series) | per-side `polymarket` (`implied_prob_pct`, `history`, `liquidity`, `volume_24hr`) | Fair-prob fallback if Covers has no de-vigable pair. History first→last implied-prob move (> 1 pp) is exchange RLM. Liquidity below $10,000 flags `low_liquidity`. |

A+ requires all of: Tier A, exchange edge ≥ **1.5 pp** vs sportsbook fair, exchange RLM confirmed, and not low liquidity.

### Not used for CFB

| Source | Why |
|---|---|
| Pinnacle | Not published for CFB. |
| PlayerProps.ai | No CFB splits; DraftKings Network is the primary instead. |

---

## How a slate is assembled

[`scrape_ncaaf_betting_splits.py`](scrape_ncaaf_betting_splits.py) scrapes each source, then merges onto DraftKings games (matchup string, then away/home names, then [`cfb_team_map.py`](cfb_team_map.py) abbreviations). Games that exist only on SBD / TheSpread / VSiN / EVA / Covers are added as extras if they fall in the +6 day window.

Field prefixes on each side of `moneyline` / `spread` / `total`:

```
public_bet_pct / handle_bet_pct / live / live_odds     DraftKings
sbd_public_bet_pct / sbd_handle_bet_pct / sbd_line     SportsBettingDime
open / open_odds / live / live_odds / diff             TheSpread
vsin_public_bet_pct / vsin_handle_bet_pct / vsin_line  VSiN
eva_line / eva_odds / eva_open / eva_history           EV Analytics
covers_odds                                            Covers (game-level)
polymarket                                             Polymarket (per side)
```

---

## How a play is chosen

For each game and each market (`moneyline`, `spread`, `total`):

1. **Gaps** — `handle % − public %` per source, left as raw percentage points.
2. **Direction** — a source votes for the side with the positive gap. Unanimous direction is required, with one exception: when VSiN and DK agree strongly (each gap ≥ 15) and only SBD votes the other way, keep the game (SBD override, Tier B cap). Neutral / missing is not dissent, so Tier B still fires when exactly two sources agree and the third is missing or flat. VSiN vs DK disagreement still discards.
3. **Composite** — weighted sum of agreeing sources only. On the SBD-override path, SBD is omitted from the sum entirely.
4. **RLM** — public side from DraftKings `public_bet_pct`; line move from TheSpread, else Polymarket history, else EVA. Must move against the public and toward the sharp side. `rlm_source_used` records which of the three was the pair.
5. **Tier** — only if RLM is confirmed:
   - **A** — all three sources agree and composite ≥ **15**
   - **B** — exactly two of three agree and composite ≥ **15**, *or* the SBD-override path
   - A single source never qualifies.
6. **Fair prob** — moneyline prefers VSiN American odds, else live / EVA. Spread and total use juice (`live_odds`, else `open_odds`, else `eva_odds`), de-vigged two-way.
7. **ML confidence (moneyline only, never filters)** — DraftKings, VSiN, and SBD all publish percentages only (no raw ticket counts or dollar handle). For a play whose American odds are ≥ `LOW_PROB_DOG_ODDS_THRESHOLD` (+200), set `low_volume_dog_flag`. As a proxy, also compare the *spread* market composite on the same side (even if that spread did not clear tiering). If the spread composite is missing, ≤ 0, or below the Tier B threshold while the ML signal is strong, set `ml_spread_divergence` and include `spread_composite_gap` plus a `confidence_note`.

   `trade-sharp` sizes these dogs to **win** the usual tier amount rather than staking it: a Tier A/A+ play stakes whatever USDC profits **$25** at the Polymarket ask (American +250 ≈ 28.6¢ → **$10** in, **$25** profit). Tier B uses the same to-win math against the $10 tier size. Favorites and unflagged plays still get the flat $25 / $10 stake.
8. **Exchange confirmation** — attach Covers / Polymarket fields; possibly upgrade A → A+. Polymarket history is still used here for the A+ exchange-RLM check (`EXCHANGE_RLM_MIN_PP`), separately from the primary RLM source order above.

Output is sorted A+ / A / B, then composite gap descending. CSV includes the same flag / provenance columns as JSON (`sbd_override`, `rlm_source_used`, `low_volume_dog_flag`, `ml_spread_divergence`, `confidence_note`, …).

---

## Worked example: UNC @ TCU moneyline

From `ncaaf_betting_splits.json` (away = UNC):

| Source | Handle % | Public % | Gap (handle − public) |
|---|---|---|---|
| DraftKings (`primary`) | 38 | 17 | **+21** |
| VSiN | 22 | 14 | **+8** |
| SBD | 23 | 6 | **+17** |

All three gaps are positive on UNC, so the side is unanimous away.

```
composite = 1.5×8 + 1.0×21 + 0.75×17 = 12 + 21 + 12.75 = 45.75
```

Public money is on TCU (DK public 83% home vs 17% away). TheSpread `open`/`live` pair was missing. The previous finder spliced EVA `eva_open` 280 against DK `live` 270 (cross-book) and called that RLM. That splice is gone: `open`/`live` on the play are the pair from `rlm_source_used`, never mixed across books. EVA itself is flat (280 → 280). Polymarket history is the preferred fallback, but liquidity on this board is well below $10,000 so it cannot be the primary RLM source (`polymarket_low_liquidity: true`). With TheSpread missing, Poly ineligible, and EVA flat, this board would **not** confirm RLM — which is the point of the provenance fix.

UNC is a +270 moneyline dog (`low_volume_dog_flag: true`). DK, VSiN, and SBD publish percentages only (no raw ticket or dollar handle), so the spread-composite proxy is attached for review (`ml_spread_divergence` / `confidence_note`) rather than treating the ML gap as equal to a corroborated ML+spread signal.

Covers books were present but had no two-way moneyline prices to de-vig, so `exchange_fair_prob` stayed null.

---

## Config (current defaults)

These live at the top of `find_sharp_money.py` and are copied into the JSON `config` block.

| Constant | Value | Meaning |
|---|---|---|
| `W_VSIN` / `W_PRIMARY` / `W_SBD` | 1.5 / 1.0 / 0.75 | Composite weights |
| `TIER_A_THRESHOLD` / `TIER_B_THRESHOLD` | 15.0 | Minimum composite gap |
| `REQUIRE_UNANIMOUS_DIRECTION` | true | Opposite-source vote discards the game, except the SBD-override path |
| `STRONG_SOURCE_GAP_THRESHOLD` | 15.0 | Per-source gap VSiN and DK must each clear for SBD-only dissent to be overridden (Tier B cap) |
| `LOW_PROB_DOG_ODDS_THRESHOLD` | +200 | Moneyline American odds at/beyond this set `low_volume_dog_flag` |
| `RLM_SOURCE_PRIORITY` | thespread, polymarket, eva | Primary RLM pair order; first complete source wins |
| `EXCHANGE_RLM_MIN_PP` | 1.0 | Polymarket history move to count as exchange RLM (A+ enrichment, not primary RLM) |
| `LOW_LIQUIDITY_THRESHOLD` | 10,000 | Polymarket liquidity flag; below this, Poly is not the primary RLM source |
| `TIER_A_PLUS_EDGE_PCT` | 1.5 | Minimum exchange edge (percentage points) for A+ |
