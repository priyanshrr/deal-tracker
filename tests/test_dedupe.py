"""Dedup behaviour, including the failures the brief calls out by name."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dealtracker.config import load_config
from dealtracker.dedupe import deduplicate, fingerprint, fingerprint_match
from dealtracker.records import DealRecord

CFG = load_config()
PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print("%-4s %s" % ("ok" if cond else "FAIL", name))


def rec(**kw):
    base = dict(deal_type="funding", company_name="Acme", sector="widgets",
                deal_date="2026-09-09", confidence="high", source_outlet="Entrackr",
                source_url="https://a/%s" % kw.get("deal_id", "x"), source_tier=1,
                published="2026-09-09T10:00:00+00:00")
    base.update(kw)
    r = DealRecord(**{k: v for k, v in base.items() if k != "deal_id"})
    r.deal_id = kw.get("deal_id", "d%d" % (len(PASS) + len(FAIL)))
    r.sources = [{"outlet": r.source_outlet, "url": r.source_url,
                  "published": r.published, "tier": r.source_tier}]
    r.first_reported_by = r.source_outlet
    return r


print("\n-- funding branch --")
a = rec(deal_id="a", amount_usd_mn=10.909, amount_as_reported="Rs 96 crore",
        round_stage="Series B", investors=["Uber"], lead_investor="Uber",
        source_outlet="Entrackr")
b = rec(deal_id="b", amount_usd_mn=10.909, amount_as_reported="$10 Mn",
        round_stage="Series B", investors=["Uber"], lead_investor="Uber",
        source_outlet="Inc42", source_url="https://b/1",
        published="2026-09-09T06:00:00+00:00")
check("same funding round, two outlets -> fingerprint match", fingerprint_match(a, b, CFG))

c = rec(deal_id="c", amount_usd_mn=10.909, round_stage="Series B",
        investors=["Peak XV Partners"], lead_investor="Peak XV Partners")
d = rec(deal_id="d", amount_usd_mn=10.909, round_stage="Series B",
        investors=["Peak XV"], lead_investor="Peak XV")
check("'Peak XV' == 'Peak XV Partners'", fingerprint_match(c, d, CFG))

e = rec(deal_id="e", amount_usd_mn=12.0, round_stage="Series A", investors=["Accel"])
f = rec(deal_id="f", amount_usd_mn=12.0, round_stage="Series A", investors=["Tiger Global"])
check("same size+stage, different investors -> NO match", not fingerprint_match(e, f, CFG))

g = rec(deal_id="g", amount_usd_mn=10.909, round_stage="Series B", investors=["Uber"],
        deal_date="2026-09-09")
h = rec(deal_id="h", amount_usd_mn=10.909, round_stage="Series B", investors=["Uber"],
        deal_date="2026-09-30")
check("same round 21 days apart -> NO match (7d window)", not fingerprint_match(g, h, CFG))

u = rec(deal_id="u", amount_usd_mn=None, round_stage="Seed", investors=[])
check("undisclosed seed round -> no fingerprint (falls to stage 2)",
      fingerprint(u, CFG) is None)

print("\n-- m&a branch --")
m1 = rec(deal_id="m1", deal_type="ma", company_name="Shadowfax",
         acquirer="Bajaj Finance Ltd", target="TrueFan AI", amount_usd_mn=None)
m2 = rec(deal_id="m2", deal_type="ma", company_name="TrueFan",
         acquirer="Bajaj Finance", target="TrueFan AI Pvt Ltd", amount_usd_mn=5.0,
         source_outlet="Inc42", source_url="https://b/2")
check("M&A matches on acquirer+target with amount undisclosed on one side",
      fingerprint_match(m1, m2, CFG))

m3 = rec(deal_id="m3", deal_type="ma", acquirer="Bajaj Finance", target="Other Co")
check("different target -> NO match", not fingerprint_match(m2, m3, CFG))

print("\n-- ipo branch (the timeline must NOT collapse) --")
milestones = ["drhp_filed", "sebi_approval", "price_band", "anchor_book", "listing"]
ipos = [rec(deal_id="ipo%d" % i, deal_type="ipo", company_name="Rentomojo",
            ipo_milestone=ms, deal_date="2026-09-%02d" % (1 + i * 2))
        for i, ms in enumerate(milestones)]
survivors, _touched, outcome = deduplicate(ipos, CFG)
check("5 IPO milestones for one company -> 5 rows, not 1", len(survivors) == 5)

same = [rec(deal_id="s1", deal_type="ipo", company_name="Rentomojo",
            ipo_milestone="anchor_book", deal_date="2026-09-09"),
        rec(deal_id="s2", deal_type="ipo", company_name="RentoMojo Ltd",
            ipo_milestone="anchor_book", deal_date="2026-09-10",
            source_outlet="Inc42", source_url="https://b/3")]
survivors2, _t, _o = deduplicate(same, CFG)
check("same milestone, two outlets -> 1 row", len(survivors2) == 1)
check("merged row keeps both sources", len(survivors2[0].sources) == 2)

print("\n-- merge semantics --")
rich = rec(deal_id="r1", amount_usd_mn=10.909, amount_as_reported="Rs 96 crore",
           round_stage="Series B", investors=["Uber"], lead_investor="Uber",
           sector="B2B fleet management", company_legal_name="Carrum Mobility Pvt Ltd",
           source_outlet="Entrackr", published="2026-09-09T13:00:00+00:00")
thin = rec(deal_id="r2", amount_usd_mn=10.909, round_stage="Series B",
           investors=["Uber", "Existing Angel"], source_outlet="Inc42",
           source_url="https://b/4", published="2026-09-09T06:00:00+00:00",
           confidence="medium", notes="second Uber cheque")
surv, _t, out = deduplicate([rich, thin], CFG)
merged = surv[0]
check("richer record becomes the base", merged.company_legal_name == "Carrum Mobility Pvt Ltd")
check("merge gains the other record's notes", merged.notes == "second Uber cheque")
check("investors are unioned", len(merged.investors) == 2)
check("source_count counts distinct outlets", merged.source_count == 2)
check("first_reported_by = earliest publish", merged.first_reported_by == "Inc42")

conf_a = rec(deal_id="c1", amount_usd_mn=10.0, round_stage="Series B",
             investors=["Uber"], lead_investor="Uber")
conf_b = rec(deal_id="c2", amount_usd_mn=10.0, round_stage="Series B",
             investors=["Uber"], lead_investor="Accel", source_outlet="Livemint",
             source_url="https://b/5")
surv3, _t, _o = deduplicate([conf_a, conf_b], CFG)
check("conflicting lead investor is written to conflicts, not silently picked",
      any("lead_investor" in c for c in surv3[0].conflicts))

amt_a = rec(deal_id="x1", amount_usd_mn=10.0, round_stage="Series B", investors=["Uber"])
amt_b = rec(deal_id="x2", amount_usd_mn=10.0, round_stage="Series B", investors=["Uber"],
            source_outlet="ET", source_url="https://b/6")
amt_b.amount_usd_mn = 10.0
surv4, _t, _o = deduplicate([amt_a, amt_b], CFG)
check("identical amounts produce no conflict noise", surv4[0].conflicts == [])

print("\n-- cross-type safety --")
mix = [rec(deal_id="z1", deal_type="funding", amount_usd_mn=10.0, round_stage="Series B",
           investors=["Uber"]),
       rec(deal_id="z2", deal_type="ma", acquirer="Uber", target="Acme")]
surv5, _t, _o = deduplicate(mix, CFG)
check("a funding row and an M&A row never merge", len(surv5) == 2)

print("\n-- the guard: no investors named on either side --")
s1 = rec(deal_id="q1", company_name="Alpha Foods", amount_usd_mn=5.0,
         round_stage="Seed", investors=[])
s2 = rec(deal_id="q2", company_name="Beta Logistics", amount_usd_mn=5.0,
         round_stage="Seed", investors=[], source_outlet="Inc42",
         source_url="https://b/7")
check("two different $5M seed rounds, same week, no investors -> NO merge",
      not fingerprint_match(s1, s2, CFG))

s3 = rec(deal_id="q3", company_name="Zepto", amount_usd_mn=5.0, round_stage="Seed",
         investors=[])
s4 = rec(deal_id="q4", company_name="Zepto (Kiranakart Technologies)",
         amount_usd_mn=5.0, round_stage="Seed", investors=[],
         source_outlet="Inc42", source_url="https://b/8")
check("same company, brand vs legal name, no investors -> merge",
      fingerprint_match(s3, s4, CFG))

p1 = rec(deal_id="p1", company_name="Acme", amount_usd_mn=20.0, round_stage="Series A",
         investors=["Accel", "Blume Ventures"])
p2 = rec(deal_id="p2", company_name="Acme Technologies", amount_usd_mn=20.0,
         round_stage="Series A", investors=["Accel India", "Elevation Capital"],
         source_outlet="ET", source_url="https://b/9")
check("partial investor overlap (Accel) -> merge", fingerprint_match(p1, p2, CFG))

n1 = rec(deal_id="n1", amount_usd_mn=20.0, round_stage="Series A", investors=["Accel"])
n2 = rec(deal_id="n2", amount_usd_mn=20.0, round_stage="Series A",
         investors=["Tiger Global"], source_outlet="ET", source_url="https://b/10")
check("no investor overlap -> NO merge", not fingerprint_match(n1, n2, CFG))

print("\n-- regressions from the first live run (2026-09-19) --")
d1 = rec(deal_id="dh1", company_name="DheyaTech", amount_usd_mn=4.886, round_stage="Pre-Series A",
         investors=["Avaana Capital", "Unimech Aerospace"], deal_date="2026-09-17")
d2 = rec(deal_id="dh2", company_name="DheyaTech", amount_usd_mn=4.886, round_stage=None,
         investors=["Avaana Capital"], deal_date="2026-09-17", source_outlet="Inc42",
         source_url="https://b/dh2")
d3 = rec(deal_id="dh3", company_name="DheyaTech", amount_usd_mn=4.89, round_stage=None,
         investors=["Avaana"], deal_date="2026-09-17", source_outlet="VCCircle",
         source_url="https://b/dh3")
surv, _t, _o = deduplicate([d1, d2, d3], CFG)
check("DheyaTech: stage given once, missing twice -> 1 row", len(surv) == 1)
check("...keeping all 3 outlets", len(surv) == 1 and surv[0].source_count == 3)

s_a = rec(deal_id="sa", amount_usd_mn=10.0, round_stage="Seed", investors=["Accel"])
s_b = rec(deal_id="sb", amount_usd_mn=10.0, round_stage="Series A", investors=["Accel"],
          source_outlet="ET", source_url="https://b/sb")
check("stated stages that DISAGREE still block a merge", not fingerprint_match(s_a, s_b, CFG))

p1 = rec(deal_id="pp1", company_name="Physioplus Healthcare", amount_usd_mn=None,
         round_stage="Seed", investors=["HBF"], deal_date="2026-09-18")
p2 = rec(deal_id="pp2", company_name="Physioplus Healthcare", amount_usd_mn=None,
         round_stage="Seed", investors=["HBF"], deal_date="2026-09-18",
         source_outlet="VCCircle", source_url="https://b/pp2")
check("Physioplus: undisclosed, same investor + company -> merge", fingerprint_match(p1, p2, CFG))
p3 = rec(deal_id="pp3", company_name="Some Other Clinic", amount_usd_mn=None,
         round_stage="Seed", investors=["HBF"], deal_date="2026-09-18",
         source_outlet="VCCircle", source_url="https://b/pp3")
check("undisclosed, same investor, DIFFERENT company -> no merge", not fingerprint_match(p1, p3, CFG))
p4 = rec(deal_id="pp4", company_name="Physioplus Healthcare", amount_usd_mn=2.0,
         round_stage="Seed", investors=["HBF"], deal_date="2026-09-18",
         source_outlet="Inc42", source_url="https://b/pp4")
check("one outlet discloses the amount, one doesn't -> merge", fingerprint_match(p1, p4, CFG))

t1 = rec(deal_id="ts1", deal_type="ma", company_name="Shapoorji Pallonji",
         acquirer="Tata Sons", target="Shapoorji Pallonji Group stake", deal_date="2026-09-18")
t2 = rec(deal_id="ts2", deal_type="ma", company_name="Shapoorji Pallonji",
         acquirer="Tata Sons", target="Shapoorji Pallonji Group", deal_date="2026-09-17",
         source_outlet="ET Now", source_url="https://b/ts2")
check("Tata Sons: 'Group stake' vs 'Group' -> 1 row", fingerprint_match(t1, t2, CFG))
t3 = rec(deal_id="ts3", deal_type="ma", acquirer="Tata Sons", target="Bisleri",
         deal_date="2026-09-17", source_outlet="ET Now", source_url="https://b/ts3")
check("same acquirer, different target -> no merge", not fingerprint_match(t1, t3, CFG))

thin = rec(deal_id="first", company_name="Acme", amount_usd_mn=5.0, investors=["Accel"],
           source_outlet="Inc42", published="2026-09-17T05:00:00+00:00")
rich = rec(deal_id="second", company_name="Acme", amount_usd_mn=5.0, round_stage="Seed",
           investors=["Accel"], lead_investor="Accel", sector="widgets for x",
           company_legal_name="Acme Pvt Ltd", notes="n", source_outlet="Entrackr",
           source_url="https://b/rich", published="2026-09-17T09:00:00+00:00")
surv, _t, _o = deduplicate([thin, rich], CFG)
check("richer newcomer in the same run: still 1 row", len(surv) == 1)
check("...and it is the RICHER record that survives", surv and surv[0].company_legal_name == "Acme Pvt Ltd")
check("...carrying both sources", surv and surv[0].source_count == 2)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
