"""SEBI DRHP list -> IPO rows, from a saved copy of the real page."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
from types import SimpleNamespace
from dealtracker.config import load_config
from dealtracker.http import FetchResult
from dealtracker.structured import ingest_sebi_drhp, parse_sebi_drhp

html = open(os.path.join(os.path.dirname(__file__), "fixtures", "sebi_drhp.html")).read()
PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name); print("%-4s %s" % ("ok" if cond else "FAIL", name))

rows = parse_sebi_drhp(html, "SEBI", 3, 0)
names = [c for c, _d, _u in rows]
check("finds the new DRHP filings", len(rows) >= 5)
check("skips addenda (Rayzon Solar's addendum is not a new IPO)", not any("Rayzon" in n for n in names))
check("company name is clean, no '- DRHP' suffix", all("DRHP" not in n for n in names))
check("every row has a date and a sebi.gov.in link", all(d and u.startswith("https://www.sebi.gov.in/") for _c, d, u in rows))

cfg = load_config(); cfg.raw["fetch"]["max_article_age_hours"] = 0
src = [s for s in cfg.sources if s.name == "SEBI"][0]
fetcher = SimpleNamespace(get=lambda url: FetchResult(url=url, ok=True, status=200, text=html))
arts, recs, err = ingest_sebi_drhp(cfg, fetcher, src, skip_ids=set())
check("builds one IPO row per filing", len(recs) == len(rows) and not err)
check("rows are ipo / drhp_filed", all(r.deal_type == "ipo" and r.ipo_milestone == "drhp_filed" for r in recs))
check("rows pass the India rule", all(r.indian_party for r in recs))
check("outlet is SEBI, url is the filing", all(r.source_outlet == "SEBI" and "sebi.gov.in" in r.source_url for r in recs))
arts2, recs2, _ = ingest_sebi_drhp(cfg, fetcher, src, skip_ids={a.article_id for a in arts})
check("already-seen filings are not added again", recs2 == [])
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
