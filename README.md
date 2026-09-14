# Indian private markets deal tracker

Monitors Indian business publications for funding, M&A and IPO news, extracts
structured deal records, deduplicates across outlets, and appends **one row per
deal** to a Google Sheet.

Eight outlets covering one Series B produce one row with eight source links.

```
feeds/scrapers -> near-dup gate -> keyword filter -> LLM extraction
              -> fingerprint dedup -> embedding fallback -> Google Sheet
                                   \-> SQLite (14 days of records + vectors)
```

## Quick start

```bash
make install
export ANTHROPIC_API_KEY=sk-ant-...
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service_account.json)"
export GSHEET_ID=1AbC...

python -m dealtracker discover        # resolve feeds, write them into sources.yaml
python -m dealtracker initsheet       # create the header row
python -m dealtracker run             # the whole pipeline
```

## Commands

| Command | What it does |
|---|---|
| `discover` | Probes every source for an RSS/Atom feed; writes `type:`/`feed:` back into `sources.yaml` and prints resolved-vs-scraper. `--all` includes disabled sources, `--dry-run` reports without writing. |
| `fetch` | Fetch and print article bodies. No LLM calls. |
| `filter` | Fetch + relevance filter, showing what was kept and what was dropped. |
| `extract` | Fetch + filter + one LLM call per article, printing every record. Caches the articles so re-running costs no refetch (`--from-cache`). |
| `run` | The full pipeline. `--no-dedup` for calibration, `--dry-run` to touch nothing, `--no-sheet` to skip the write. |
| `initsheet` | Creates/validates the header row. |

Every command takes `--source NAME` (repeatable, and it overrides `enabled:`)
and `--tier N`.

## Configuration

Nothing operational lives in code.

- **`sources.yaml`** — one block per outlet. Adding an outlet is a config change,
  never a code change. `discover` rewrites only the `type:` and `feed:` lines, so
  your comments survive.
- **`config.yaml`** — FX rate, all thresholds, keyword lists, model, fetch
  etiquette, retention.

Relevance keywords accept raw regex when prefixed with `re:`, because the
interesting phrasings are discontinuous — no fixed phrase catches
"Navam Capital **leads** Rs 22 Cr **round** in DigitalPaani".

## Source status

19 sources are enabled. **14 work. 5 are blocked** and are left enabled and
flagged so every run reports them separately from real errors — if a block lifts
they resume with no config change.

| Blocked | Why |
|---|---|
| Moneycontrol | 403 to every non-browser UA, feeds and pages alike |
| Business Standard | same |
| Businesswire India | `robots.txt` disallows the RSS endpoint; 403 elsewhere |
| ANI News | 403 on every path |
| NDTV Profit | feed works but carries ~150 chars/entry; article pages 403 |

Getting these would need a paid API or evading bot detection. Neither is done here.

Three sources have no feed and are scraped from their listing pages: VCCircle,
Financial Express, Outlook Business.

Tier 4 (the syndication network) ships `enabled: false`. 8 of the 17 have working
WordPress feeds; the other 9 do not resolve in DNS or return hard 500s.

## Deduplication

**Company name is never a key.** Outlets variously use the brand ("Zepto") or the
legal entity ("Kiranakart Technologies Private Limited"). Name is only used after
normalisation, and only as a *guard* that can block a merge — never cause one.

### Stage 1 — deterministic fingerprints, branched by deal type

- **Funding** — `(amount rounded to 0.1, round_stage)` as the key, plus a date
  window of ±7d, plus **investor overlap**.
- **M&A** — `(normalised acquirer, normalised target)` ±7d. Amount is
  deliberately *not* in the key, because it is so often undisclosed.
- **IPO** — `(normalised company, ipo_milestone)` ±14d. An IPO is a sequence, not
  a point event: DRHP, SEBI approval, price band, anchor book and listing are
  five legitimate rows for one company.

Investor and acquirer names are normalised by stripping `Capital`, `Ventures`,
`Partners`, `LLP`, `Fund` and similar, so `Peak XV` and `Peak XV Partners` match.

**One deliberate deviation from the spec.** The brief specifies the top 3
normalised investor names as part of the funding key. Taken literally that makes
stage 1 brittle in the common case: Entrackr names one investor, Inc42 names
three, the sorted top-3 sets differ, the key differs, and the same round becomes
two rows — which breaks "one row per deal". So investors are compared by
**non-empty intersection** instead. It is still fully deterministic, and it only
ever merges rounds that share a named investor. When *neither* side names an
investor, amount+stage+date alone would happily merge two unrelated $5M seed
rounds in the same week, so the company name must also agree.

### Stage 2 — embedding fallback

For records that cannot be fingerprinted (undisclosed amount, no investors), the
record is embedded and compared by cosine against the last 14 days.

**Read this before trusting the threshold.** The default embedding backend
(`hash-tf-v1`) is local, free and byte-stable — deliberately, because vectors are
persisted in SQLite and compared against vectors written by earlier runs, and a
hosted model that silently reversions would corrupt 14 days of history. But it is
*lexical*, not semantic: it scores the **same** deal from two outlets around
**0.65**. The brief's 0.93 is the right number for a semantic model, where two
different fintech Series A rounds in the same week land at 0.80–0.90. At 0.93
with the lexical backend, stage 2 never fires.

That fails in the safe direction — a missed merge is a visible duplicate row, a
wrong merge is an invisible collapse of two real deals — so it ships that way, with
`embedding_merge_enabled: false`. Stage 2 currently *reports* matches instead of
acting on them. To turn it on, either:

1. run calibration for a week and set a threshold that fits `hash-tf-v1`, or
2. set `embedding_model: "voyage:voyage-3"` (needs `voyageai` + `VOYAGE_API_KEY`)
   and keep 0.93.

Then set `embedding_merge_enabled: true`.

### Stage 3 — merge

Base record is the one with the most non-null fields, ties broken on confidence
then tier. Gaps in the base are filled from the other record, so a merge never
loses a fact. Sources are appended, `first_reported_by` is the outlet with the
earliest publish timestamp, `source_count` counts distinct outlets. Material
disagreements — amount more than 10% apart, a different lead investor, a
different round stage — are written into `conflicts` rather than silently
resolved.

## Calibration mode

```bash
python -m dealtracker run --no-dedup
```

Writes every extracted record as its own row, with the fingerprint it would have
matched on and its top-3 nearest neighbours by cosine — to a separate
`calibration` worksheet, so the main sheet stays one-row-per-deal. The console
prints the score distribution. Pick a threshold above the highest score you see
between two records you judge to be *different* deals.

## Things that will bite you

### Roundups are the main false-positive source
An article covering ten deals must be rejected, not extracted as one. This is
handled in three places: a title-pattern gate that skips the model call
entirely, the first and longest rule in the extraction prompt, and — for the
syndication tier — the near-duplicate gate below. It is still the most likely
source of a garbage row, because a roundup whose title looks like a single deal
("Carrum Mobility, Circolife, ARC, QNu Labs raise early-stage funding") is only
detectable from the body. Grep the sheet for rows whose `sector` reads oddly
generic; that is usually a roundup that slipped through.

### Undisclosed-amount seed rounds are the hardest dedup case
No amount means no funding fingerprint, so they fall to stage 2 — which is off by
default. Two outlets covering the same unpriced seed round will produce two rows
until you calibrate stage 2. That is the intended failure: a visible duplicate is
cheap to spot, a silent merge of two different seed rounds is not.

### Paywalled outlets return partial text
VCCircle, Moneycontrol, ET, Business Standard, Livemint and Mint serve a teaser
and a subscribe prompt. Boilerplate lines are stripped *before* the length check
so a subscribe prompt cannot pad a teaser over the threshold, and bodies ending
in a truncation marker are treated as teasers. Anything under
`min_body_chars` (200) is logged and skipped — never sent to the model. In a
typical run VCCircle contributes 5 of 8 articles as thin.

### Thresholds are empirical
Every number in `config.yaml` — the stage-2 threshold, the near-duplicate
threshold, the ±7d/±14d windows, the 10% conflict tolerance, `min_body_chars` —
was set from a small sample or from the brief, not from a season of your data.
They need retuning as the source mix changes, and especially when you enable
tier 4. Re-run calibration after any material change to the source list.

### Syndication costs nothing, once
When tier 4 is enabled, an article whose body is >90% similar (normalised
shingle overlap, containment rather than Jaccard — a reprint is usually the wire
copy *plus* a boilerplate footer, so it is a superset) to anything ingested in
the last 48h is dropped before extraction. Verbatim syndication never costs an
LLM call.

## State and scheduling

SQLite (`data/state.sqlite`) holds the last 14 days of records plus their
vectors, and the hashes of every article already extracted. The Sheet is output
only and is never read back. The Action commits state after each run.

Articles already seen in an earlier run are skipped before extraction, so the
30-minute cadence does not re-pay for the same articles.

`data/runs.log` gets one line per run: fetched, thin, already-seen,
near-duplicates, passed filter, extracted, merged, written, tokens, and errors
by source.

## Google Sheet

Service-account auth via `GOOGLE_SERVICE_ACCOUNT_JSON` (or
`sheets.service_account_file`). Share the sheet with the service account's email
as an Editor.

Append-only, except that `sources`, `source_count` and `conflicts` are refreshed
on an existing row when a late article joins that deal. **`read_flag` is yours** —
appends stop one column short of it and updates address columns by name, so the
pipeline can never write to it.

## Tests

```bash
make test
```

`tests/test_dedupe.py` covers all three fingerprint branches, including the cases
the brief calls out by name: the IPO timeline must not collapse, M&A must match
with an undisclosed amount, `Peak XV` must equal `Peak XV Partners`, and two
different $5M seed rounds in the same week must not merge.

`tests/test_pipeline.py` runs the whole pipeline over 20 real cached articles
with a scripted model, and asserts that Carrum Mobility (Entrackr + Inc42)
collapses to one row carrying both URLs, that the VCCircle four-deal roundup
produces no row, that two Rentomojo IPO milestones stay as two rows, and that
re-running the same articles costs zero LLM calls.
