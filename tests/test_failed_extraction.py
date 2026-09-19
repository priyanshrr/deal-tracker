"""A run where every model call fails must not mark those articles as seen.

Regression test for the first live run: a bad API key made all 90 calls fail,
and the articles were still recorded as seen, so later runs would have skipped
them and the deals would never have reached the sheet.
"""
import os, sys, tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dealtracker.config import load_config
from dealtracker.pipeline import run
from dealtracker.store import Store


class RejectingMessages:
    def create(self, **kwargs):
        raise RuntimeError("AuthenticationError: invalid x-api-key")


class RejectingClient:
    messages = RejectingMessages()


cfg = load_config()
tmp = tempfile.mkdtemp()
cfg.raw["state"]["sqlite_path"] = os.path.join(tmp, "s.sqlite")
cfg.raw["logging"]["run_log"] = os.path.join(tmp, "r.log")
cfg.raw["logging"]["dropped_log"] = os.path.join(tmp, "d.jsonl")
cfg.raw["extraction"]["retries"] = 0

args = SimpleNamespace(source=None, tier=None, limit=50, no_dedup=False, no_sheet=True,
                       dry_run=False, from_cache="data/articles_cache.json",
                       _client=RejectingClient())
summary, artifacts = run(cfg, args)

store = Store(cfg.path("state.sqlite_path"))
seen = store.seen_article_ids()
store.close()

failed = summary["extraction_failed"]
attempted_ids = {r.article_id for r in artifacts.get("records", [])}
ok = []
ok.append(("every call failed", failed > 0 and summary["extracted"] == 0))
ok.append(("error text recorded", "invalid x-api-key" in summary["first_extraction_error"]))
ok.append(("no key text leaks", "sk-ant-" not in open(cfg.path("logging.run_log")).read()))
log_line = open(cfg.path("logging.run_log")).read()
ok.append(("error appears in runs.log", "invalid x-api-key" in log_line))
ok.append(("failed articles are NOT marked seen", len(seen) + failed <= summary["fetched"]
           and failed > 0))

# The decisive check: re-running must retry them, not skip them.
args2 = SimpleNamespace(**{**args.__dict__, "_client": RejectingClient()})
summary2, _ = run(cfg, args2)
ok.append(("second run retries the same articles", summary2["extraction_failed"] == failed))

for name, cond in ok:
    print("%-4s %s" % ("ok" if cond else "FAIL", name))
print("\n%d passed, %d failed" % (sum(c for _, c in ok), sum(not c for _, c in ok)))
sys.exit(0 if all(c for _, c in ok) else 1)
