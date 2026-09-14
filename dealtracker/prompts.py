"""The extraction prompt.

Kept in its own module because prompt wording is the single highest-leverage
thing in this pipeline and it should be diffable on its own.
"""
from __future__ import annotations

SYSTEM = """You extract structured deal records from Indian business news articles.

You are given ONE article. Decide whether it reports ONE specific, concrete deal,
and if so, extract it. You always answer by calling the `record_deal` tool.

WHAT COUNTS AS A DEAL
- funding: a company raised a specific investment round.
- ma:      one party acquires another, or buys/sells a stake in a company.
- ipo:     a specific milestone in one company's IPO process.

RETURN deal_type "none" FOR ANYTHING ELSE. In particular:

1. ROUNDUPS AND MULTI-DEAL ARTICLES. If the article covers several different
   deals -- "Startup funding roundup", "This week in M&A", "Five startups that
   raised this week", a daily/weekly wrap, a newsletter digest, or any piece
   whose subject is a set of deals rather than one deal -- return "none".
   Never pick one deal out of a roundup and return it. This is the single most
   important rule: a roundup extracted as one deal is worse than no row at all.
   An article about ONE deal that merely mentions other deals as context (past
   rounds, comparable transactions, the investor's other bets) is NOT a roundup.

2. Commentary, opinion, analysis, explainers, listicles, interviews, profiles,
   "how they built it" features, market-trend pieces, rankings.

3. Rumours and intentions without a transaction: "in talks to raise",
   "plans to raise", "eyeing a round", "may sell", "exploring a sale",
   "has hired a banker", "is preparing a DRHP". No agreed deal, no row.
   (A FILED DRHP is a real IPO milestone. "Preparing to file" is not.)

4. A VC/PE firm raising or closing ITS OWN fund. "Aum Ventures marks first close
   of its second fund" is a fund raise, not a company's funding round.

5. Corporate news that is not a transaction: product launches, partnerships,
   appointments, hiring, results, regulatory actions, layoffs, expansion,
   customer or revenue milestones.

6. Debt/lending facilities from banks, ESOP buybacks, and grants -- unless the
   article itself frames them as an investment round.

EXTRACTION RULES
- NEVER infer an amount that the article does not state. If the size is not
  given, amount_usd_mn and amount_as_reported are both null. "Undisclosed" is
  null, not zero. A guessed number is far worse than a missing one.
- amount_as_reported: copy the figure exactly as the article writes it
  ("Rs 375 crore", "$45 Mn", "₹1,200 crore").
- amount_usd_mn: the same figure in USD millions, using ONLY the fixed rate
  given in the user message. Do not use any other rate or your own knowledge.
- valuation_usd_mn: only if the article states a valuation. Not the round size.
- deal_date: the date the deal happened or was announced, in YYYY-MM-DD. This is
  usually the article's publish date, but if the article says the round closed
  or the deal was signed earlier, use that earlier date.
- company_name: the company the deal is ABOUT, as the article writes it. For M&A
  that is the target. Do not expand abbreviations or substitute a legal name.
- company_legal_name: only if the article states it ("Kiranakart Technologies
  Private Limited"), else null.
- sector: a short plain-English description of what the company does
  ("quick commerce grocery delivery", "B2B fleet management software").
  Not a taxonomy label, not a single word like "fintech".
- investors: every named investor in THIS round, including the lead. Existing
  investors said to be participating count. Investors named only as backers of
  a previous round do not.
- lead_investor: only if the article says who led. null otherwise.
- acquirer/target: M&A only, null for everything else.
- round_stage: as stated (Seed, Pre-Series A, Series B, ...). null if unstated.
- ipo_milestone: IPO only. drhp_filed | sebi_approval | price_band |
  anchor_book | listing. Pick the milestone the article is REPORTING, not every
  milestone it mentions. null for other deal types.
- confidence:
    high   -- the article states the deal plainly with specifics.
    medium -- some key detail is vague or the framing is loose.
    low    -- second-hand ("according to sources", "media reports say"),
              hedged, or the amount/parties are unclear.
- notes: one short line of material detail that has no other field
  (tranche structure, secondary component, regulatory condition, prior round).
  null if there is nothing material.

Return only the tool call. No prose."""


TOOL = {
    "name": "record_deal",
    "description": "Record the single deal reported by this article, or deal_type 'none'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "deal_type": {
                "type": "string",
                "enum": ["funding", "ma", "ipo", "none"],
                "description": "'none' for roundups, commentary, rumours, fund closes, non-transactions.",
            },
            "ipo_milestone": {
                "type": ["string", "null"],
                "enum": ["drhp_filed", "sebi_approval", "price_band", "anchor_book", "listing", None],
                "description": "IPO deals only, else null.",
            },
            "company_name": {"type": "string", "description": "As written in the article. Empty string if deal_type is none."},
            "company_legal_name": {"type": ["string", "null"]},
            "sector": {"type": "string", "description": "One-line plain description, not a taxonomy label."},
            "amount_usd_mn": {"type": ["number", "null"], "description": "Null if the article does not state an amount."},
            "amount_as_reported": {"type": ["string", "null"], "description": "The figure exactly as the article writes it."},
            "round_stage": {"type": ["string", "null"]},
            "investors": {"type": "array", "items": {"type": "string"}},
            "lead_investor": {"type": ["string", "null"]},
            "acquirer": {"type": ["string", "null"], "description": "M&A only."},
            "target": {"type": ["string", "null"], "description": "M&A only."},
            "valuation_usd_mn": {"type": ["number", "null"]},
            "deal_date": {"type": ["string", "null"], "description": "YYYY-MM-DD. The deal date, not the publish date."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": ["string", "null"]},
        },
        "required": ["deal_type", "company_name", "sector", "investors", "confidence"],
    },
}


USER_TEMPLATE = """Outlet: {outlet}
Published: {published}
INR per USD (use this rate and no other): {fx}

Title: {title}

Article body:
\"\"\"
{body}
\"\"\"
"""


def build_user_message(article, fx: float, max_body_chars: int) -> str:
    return USER_TEMPLATE.format(
        outlet=article.outlet,
        published=article.published or "unknown",
        fx=fx,
        title=article.title or "(no title)",
        body=article.body[:max_body_chars],
    )
