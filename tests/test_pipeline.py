"""End-to-end run over the real cached articles, with a scripted model.

Proves the wiring of steps 5-7 without needing an API key: the same deal from
two outlets collapses to one row, the roundup is rejected, and the IPO timeline
stays intact.
"""
import json, os, sys, tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dealtracker.config import load_config
from dealtracker.commands import _load_articles
from dealtracker.pipeline import run
from stub_client import StubClient

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print("%-4s %s" % ("ok" if cond else "FAIL", name))

CFG = load_config()
tmp = tempfile.mkdtemp()
CFG.raw["state"]["sqlite_path"] = os.path.join(tmp, "state.sqlite")
CFG.raw["logging"]["run_log"] = os.path.join(tmp, "runs.log")
CFG.raw["logging"]["dropped_log"] = os.path.join(tmp, "dropped.jsonl")

ARTICLES = _load_articles("data/articles_cache.json")

# A scripted "model": keyed on what the real cached articles actually say.
SCRIPT = {
    "carrum": dict(deal_type="funding", company_name="Carrum Mobility",
                   sector="B2B fleet management for CNG and EV fleets",
                   amount_as_reported="$10 million", amount_usd_mn=10.0,
                   round_stage="Series B", investors=["Uber"], lead_investor="Uber",
                   deal_date="2026-09-09", confidence="high"),
    "qnu": dict(deal_type="funding", company_name="QNu Labs",
                sector="quantum-safe cybersecurity", amount_as_reported="Rs 200 Cr",
                amount_usd_mn=22.727, round_stage="Series A1",
                investors=["National Quantum Mission", "Speciale Invest"],
                lead_investor=None, deal_date="2026-09-08", confidence="high"),
    "truefan": dict(deal_type="ma", company_name="TrueFan AI", sector="AI celebrity video",
                    acquirer="Bajaj Finance", target="TrueFan AI",
                    amount_as_reported=None, amount_usd_mn=None,
                    investors=[], deal_date="2026-09-08", confidence="high"),
    "rentomojo_anchor": dict(deal_type="ipo", company_name="Rentomojo",
                             sector="furniture and appliance rental",
                             ipo_milestone="anchor_book", amount_as_reported="Rs 376 Cr",
                             amount_usd_mn=42.7, investors=[], deal_date="2026-09-08",
                             confidence="high"),
    "rentomojo_sub": dict(deal_type="ipo", company_name="RentoMojo",
                          sector="furniture and appliance rental",
                          ipo_milestone="listing", amount_as_reported=None,
                          amount_usd_mn=None, investors=[], deal_date="2026-09-09",
                          confidence="medium"),
    "none": dict(deal_type="none", company_name="", sector="", investors=[],
                 confidence="high"),
}

def _title_of(content):
    for line in content.splitlines():
        if line.startswith("Title: "):
            return line[len("Title: "):].strip().lower()
    return ""


def responder(kw):
    """Scripted per TITLE, so the stub is deterministic rather than fuzzy.

    The VCCircle entry is the interesting one: it names four companies, so a
    body-substring stub would wrongly feed it to the Carrum record. A roundup
    must return `none`, which is exactly what the prompt instructs.
    """
    title = _title_of(kw["messages"][0]["content"])
    if "circolife" in title and "arc" in title:          # the 4-deal roundup
        return dict(SCRIPT["none"])
    if "carrum mobility" in title:
        return dict(SCRIPT["carrum"])
    if "qnu labs" in title:
        return dict(SCRIPT["qnu"])
    if "truefan" in title:
        return dict(SCRIPT["truefan"])
    if "rentomojo" in title and "anchor" in title:
        return dict(SCRIPT["rentomojo_anchor"])
    if "rentomojo" in title and "subscribed" in title:
        return dict(SCRIPT["rentomojo_sub"])
    return dict(SCRIPT["none"])

args = SimpleNamespace(source=None, tier=None, limit=50, no_dedup=False,
                       no_sheet=True, dry_run=False,
                       from_cache="data/articles_cache.json",
                       _client=StubClient(responder))

summary, artifacts = run(CFG, args)
survivors = artifacts["survivors"]
print("\nsurviving rows:")
for r in survivors:
    print("   %-8s %-22s %-14s sources=%d (%s)" % (
        r.deal_type, r.company_name, r.amount_as_reported or "undisclosed",
        r.source_count, ", ".join(s["outlet"] for s in r.sources)))

by_company = {r.company_name.lower(): r for r in survivors}
check("Carrum Mobility reported by 2 outlets -> 1 row",
      sum(1 for r in survivors if "carrum" in r.company_name.lower()) == 1)
check("...carrying both source URLs",
      bool(by_company.get("carrum mobility")) and by_company["carrum mobility"].source_count == 2)
check("QNu Labs reported by 2 outlets -> 1 row",
      sum(1 for r in survivors if "qnu" in r.company_name.lower()) == 1)
check("the VCCircle 4-deal roundup produced no row",
      not any("circolife" in (r.company_name or "").lower() for r in survivors))
check("two different Rentomojo IPO milestones stay as 2 rows",
      sum(1 for r in survivors if "rentomojo" in r.company_name.lower()) == 2)
check("M&A row survives with an undisclosed amount",
      any(r.deal_type == "ma" and r.amount_usd_mn is None for r in survivors))
check("merge counter agrees", summary["merged"] >= 2)
check("nothing written to a sheet in --no-sheet", summary["written"] == 0)

# Second run over the same articles must be a no-op: they are already seen.
args2 = SimpleNamespace(**{**args.__dict__, "_client": StubClient(responder)})
summary2, _ = run(CFG, args2)
check("re-running the same articles costs zero LLM calls",
      summary2["already_seen"] == summary["fetched"] and summary2["extracted"] == 0)

print("\nrun log line:\n  " + summary.line())
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
