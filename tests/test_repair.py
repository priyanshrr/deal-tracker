"""Rehearse the one-time repair against a copy of the real state and a fake sheet."""
import os, shutil, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)

from dealtracker.config import load_config
from dealtracker.repair import maybe_repair, plan_repair
from dealtracker.sheets import COLUMNS, SheetWriter, record_to_row
from dealtracker.store import Store


class FakeSpreadsheet:
    def __init__(self):
        self.tabs = []
        self.rich_requests = 0
    def worksheets(self):
        return self.tabs
    def duplicate_sheet(self, source_sheet_id, new_sheet_name=None, **kw):
        src = [t for t in self.tabs if t.id == source_sheet_id][0]
        copy = FakeWorksheet(self, new_sheet_name, [list(r) for r in src.rows], 99)
        self.tabs.append(copy)
        return copy
    def batch_update(self, body):
        self.rich_requests += len(body.get("requests", []))


class FakeWorksheet:
    def __init__(self, book, title, rows, wid=0):
        self.spreadsheet, self.title, self.rows, self.id = book, title, rows, wid
    def row_values(self, n):
        return list(self.rows[n - 1]) if n <= len(self.rows) else []
    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]
    def delete_rows(self, start_index, end_index=None):
        del self.rows[start_index - 1]
    def batch_update(self, updates, value_input_option=None):
        for u in updates:
            col = ord(u["range"][0]) - 65
            row = int(u["range"][1:])
            self.rows[row - 1][col] = u["values"][0][0]


PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name); print("%-4s %s" % ("ok" if cond else "FAIL", name))

cfg = load_config()
tmp = tempfile.mkdtemp()
shutil.copy("data/state.sqlite", os.path.join(tmp, "s.sqlite"))
cfg.raw["state"]["sqlite_path"] = os.path.join(tmp, "s.sqlite")
store = Store(cfg.path("state.sqlite_path"))

# Build the "sheet" exactly as the pipeline wrote it, and tick read_flag on every row.
records, _ = store.load_recent(14)
book = FakeSpreadsheet()
rows = [list(COLUMNS)] + [record_to_row(r) + ["x"] for r in records]
ws = FakeWorksheet(book, "deals", rows); book.tabs.append(ws)
before_ids = [r[0] for r in rows[1:]]
plan = plan_repair(cfg, [r for r in store.load_recent(14)[0]])
expect_removed = set(plan["remove_ids"])
expect_kept = {r.deal_id for r in plan["survivors"]}

orig_init = SheetWriter.__init__
def fake_init(self, cfg):
    orig_init(self, cfg); self._ws = ws
SheetWriter.__init__ = fake_init

result = maybe_repair(cfg, store)
after_ids = [r[0] for r in ws.rows[1:]]

check("backup tab created before changes", any(t.title.startswith("deals_backup_") for t in book.tabs))
check("backup tab holds all %d original rows" % len(before_ids),
      len([t for t in book.tabs if t.title.startswith("deals_backup_")][0].rows) - 1 == len(before_ids))
check("exactly the planned %d rows removed" % len(expect_removed),
      set(before_ids) - set(after_ids) == expect_removed)
check("no other row touched or reordered",
      after_ids == [i for i in before_ids if i not in expect_removed])
check("read_flag survives on every remaining row", all(r[COLUMNS.index("read_flag")] == "x" for r in ws.rows[1:]))
dh = [r for r in ws.rows[1:] if r[COLUMNS.index("company")] == "DheyaTech"]
check("DheyaTech: 1 row left, source_count 3", len(dh) == 1 and str(dh[0][COLUMNS.index("source_count")]) == "3")
check("the rows kept are exactly the planned survivors", set(after_ids) == expect_kept)
check("nothing planned for removal is also planned to be kept", not (expect_removed & expect_kept))
check("state agrees with the sheet", {r.deal_id for r in store.load_recent(14)[0]} == set(after_ids))
check("second run is a no-op", maybe_repair(cfg, store) == {})
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
