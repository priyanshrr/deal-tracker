# Build status

Build order from the brief, section 9.

| # | Step | State |
|---|------|-------|
| 1 | `sources.yaml` + feed discovery | done |
| 2 | Fetch Entrackr + VCCircle | done (all tier 1 working) |
| 3 | Relevance filter | done, tuned once against real drops |
| 4 | Extraction on ~20 real articles | written; **the 20 outputs are unreviewed — needs `ANTHROPIC_API_KEY`** |
| 5 | Fingerprint dedup, 3 branches | done, 22 tests |
| 6 | Embedding fallback + calibration | done; stage-2 merging ships OFF, see below |
| 7 | Google Sheet write | written; **never run against a real sheet (no credentials here)** |
| 8 | All tier 1-3 sources | done: 14 of 19 work, 5 blocked and flagged |
| 9 | GitHub Actions | done |

## The one thing still unreviewed

Step 4 is your gate: "prompt quality is decided at this step". There is no
`ANTHROPIC_API_KEY` in this environment, so the 20 real extraction outputs have
never been produced. Everything downstream was built and tested against those
same 20 real articles with a *scripted* model, which proves the wiring but says
nothing about prompt quality.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m dealtracker extract --from-cache data/articles_cache.json --limit 20
```

The 20 cached articles are a good gate set by luck: they include a VCCircle
roundup covering four deals at once, Carrum Mobility and QNu Labs each reported
by two outlets, and two different Rentomojo IPO milestones.

## Calibration result (scripted records, real articles)

Running `--no-dedup` over the cached set gives, with the default `hash-tf-v1`
backend:

- same deal, two outlets: **0.577** and **0.659**
- different deals: **0.276 - 0.368**

So the real threshold for this backend is around **0.45-0.50**, not 0.93. This is
a 5-record sample and should not be trusted as a setting — it is an illustration
of why calibration mode exists. Stage-2 merging ships disabled
(`embedding_merge_enabled: false`) until you run it over a real week.

## Step 1 result: feed discovery

19 enabled sources probed. 12 resolved to a feed, 7 need HTML scraping.

**Resolved to RSS/Atom (12)**

| Source | Feed |
|---|---|
| Entrackr | `https://entrackr.com/rss` |
| Inc42 | `https://inc42.com/feed/` |
| YourStory | `https://yourstory.com/feed` |
| Economic Times | `https://economictimes.indiatimes.com/rssfeedstopstories.cms` |
| Livemint | `https://www.livemint.com/rss/companies` |
| NDTV Profit | `https://feeds.feedburner.com/ndtvprofit-latest` |
| The Hindu | `https://www.thehindu.com/business/feeder/default.rss` |
| Hindustan Times | `https://www.hindustantimes.com/feeds/rss/business/rssfeed.xml` |
| Times of India | `https://timesofindia.indiatimes.com/rssfeeds/1898055.cms` |
| ET Now | `https://www.etnownews.com/feeds/gns-etn-companies.xml` |
| CNBC | `https://www.cnbc.com/id/19832390/device/rss/rss.html` |
| PR Newswire India | `https://www.prnewswire.com/rss/india-latest-news/india-latest-news-list.rss` |

**Needs an HTML scraper (3, working)**

- **VCCircle** — `/rss/news` serves the Next.js app shell, not a feed. Scraped.
- **Financial Express** — `/feed/` is gone (HTTP 410), `/business/feed/` serves HTML. Scraped.
- **Outlook Business** — no feed at any probed path. Scraped.

**Blocked by bot management (4, flagged not removed)**

`Moneycontrol`, `Business Standard`, `Businesswire India`, `ANI News` return HTTP
403 to every non-browser user agent, on feeds and article pages alike.
Businesswire additionally disallows its RSS endpoint in `robots.txt`. They are
left `enabled: true` with `flags: [blocked]` so each run keeps reporting them
separately from real errors; if the block lifts they start working with no config
change. Getting them would mean either a paid API or evading bot detection.

**Tier 4 (disabled, probed anyway)** — 8 of 17 have working WordPress feeds
(`startupchronicle.in`, `businesssaga.in`, `storynetwork.in`, `startuptimes.in`,
`startupmagazine.in`, `startupnewswire.in`, `businessmax.in`, `economicedge.in`).
The other 9 do not resolve in DNS or return hard 500s — they appear to be dead.

## Step 3 result: relevance filter

First live run on tier 1 kept 16/27. Inspecting the drops found three real deals
being thrown away, all discontinuous phrasings that no fixed keyword catches:

- "Navam Capital **leads** Rs 22 Cr **round** in DigitalPaani"
- "NewQuest **offloads** Rs 200 Cr Shadowfax **stake**"
- "Kotak Alts **sells** HKR Roadways **to** Cube Highways"

So `config.yaml` keywords now also accept raw regex when prefixed with `re:`.
All three are caught, and the previously-correct drops still drop. Current tier-1
rate is 30/67 kept (45%).

Judgment call worth reviewing: **VC/PE fund closes** ("Aum Ventures marks first
close of second deeptech fund") are treated as not-a-deal. They are a fund
raising its own capital, not a company's funding round.
