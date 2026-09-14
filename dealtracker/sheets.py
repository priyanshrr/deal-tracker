"""Google Sheet output. Append-only, except for late joiners.

`read_flag` is the operator's column. Nothing here ever writes to it: appends
stop one column short, and updates address `sources`/`source_count` by name.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("dealtracker.sheets")

# `sources` sits third, right after the dates, so the link to the article is
# reachable without scrolling the full width of the row.
COLUMNS = [
    "deal_id", "date_added", "sources", "deal_date", "deal_type",
    "ipo_milestone", "company", "legal_name", "sector", "amount_usd_mn",
    "amount_as_reported", "round_stage", "lead_investor", "all_investors",
    "valuation_usd_mn", "first_reported_by", "source_count", "confidence",
    "conflicts", "read_flag",
]

# Never written by the pipeline.
OPERATOR_COLUMNS = {"read_flag"}

# Refreshed when a late article joins an existing deal.
UPDATABLE_COLUMNS = ["sources", "source_count", "conflicts"]


def _a1_col(index_zero_based: int) -> str:
    n, out = index_zero_based + 1, ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


LINK_BLUE = {"red": 0.06, "green": 0.33, "blue": 0.80}
SEPARATOR = "  ·  "


def sources_rich_cell(rec) -> Dict[str, Any]:
    """One cell, one clickable link per outlet.

    A plain "Outlet: https://…" string is not clickable in Google Sheets until
    you enter edit mode, and HYPERLINK() only carries one link per cell. Rich
    text runs are the only way to get several independently clickable outlet
    names into a single cell, so the cell reads "Entrackr · Inc42" and each
    name opens that outlet's article in one click.
    """
    sources = rec.sources or []
    text, runs = "", []
    for src in sources:
        label = src.get("outlet") or "source"
        url = src.get("url") or ""
        if text:
            runs.append({"startIndex": len(text), "format": {}})
            text += SEPARATOR
        if url:
            runs.append({
                "startIndex": len(text),
                "format": {"link": {"uri": url}, "underline": True,
                           "foregroundColor": LINK_BLUE},
            })
        else:
            runs.append({"startIndex": len(text), "format": {}})
        text += label
    return {
        "userEnteredValue": {"stringValue": text},
        "textFormatRuns": runs,
    }


def record_to_row(rec) -> List[Any]:
    """One row per deal. Stops before read_flag so that column is never touched."""
    # Plain-text fallback, used if the rich-text pass fails for any reason.
    # Keeps the URLs visible so a row is never left unauditable.
    sources = " | ".join(
        "%s: %s" % (s.get("outlet", "?"), s.get("url", "")) for s in (rec.sources or [])
    )
    values = {
        "deal_id": rec.deal_id,
        "date_added": (rec.date_added or "")[:19].replace("T", " "),
        "deal_date": rec.deal_date or "",
        "deal_type": rec.deal_type,
        "ipo_milestone": rec.ipo_milestone or "",
        "company": rec.company_name or "",
        "legal_name": rec.company_legal_name or "",
        "sector": rec.sector or "",
        "amount_usd_mn": "" if rec.amount_usd_mn is None else rec.amount_usd_mn,
        "amount_as_reported": rec.amount_as_reported or "",
        "round_stage": rec.round_stage or "",
        "lead_investor": rec.lead_investor or "",
        "all_investors": ", ".join(rec.investors or []),
        "valuation_usd_mn": "" if rec.valuation_usd_mn is None else rec.valuation_usd_mn,
        "first_reported_by": rec.first_reported_by or "",
        "source_count": rec.source_count,
        "sources": sources,
        "confidence": rec.confidence,
        "conflicts": " ; ".join(rec.conflicts or []),
    }
    last = len(COLUMNS) - len(OPERATOR_COLUMNS)
    return [values.get(col, "") for col in COLUMNS[:last]]


class SheetWriter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.spreadsheet_id = (
            os.environ.get("GSHEET_ID") or cfg.get("sheets.spreadsheet_id") or ""
        )
        self.worksheet_name = cfg.get("sheets.worksheet", "deals")
        self._ws = None

    # -- auth --------------------------------------------------------
    def _credentials(self):
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        env_name = self.cfg.get("sheets.service_account_env", "GOOGLE_SERVICE_ACCOUNT_JSON")
        raw = os.environ.get(env_name)
        if raw:
            return Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or self.cfg.get(
            "sheets.service_account_file"
        )
        if path and os.path.exists(path):
            return Credentials.from_service_account_file(path, scopes=scopes)
        raise RuntimeError(
            "no service-account credentials: set %s or sheets.service_account_file" % env_name
        )

    @property
    def worksheet(self):
        if self._ws is not None:
            return self._ws
        import gspread

        if not self.spreadsheet_id:
            raise RuntimeError("no spreadsheet id: set GSHEET_ID or sheets.spreadsheet_id")
        client = gspread.authorize(self._credentials())
        book = client.open_by_key(self.spreadsheet_id)
        try:
            ws = book.worksheet(self.worksheet_name)
        except Exception:  # noqa: BLE001 - gspread raises WorksheetNotFound
            ws = book.add_worksheet(title=self.worksheet_name, rows=1000, cols=len(COLUMNS))
        self._ws = ws
        return ws

    # -- writes ------------------------------------------------------
    def ensure_header(self) -> List[str]:
        ws = self.worksheet
        existing = ws.row_values(1)
        if not existing:
            ws.update(range_name="A1", values=[COLUMNS])
            return COLUMNS
        if existing[: len(COLUMNS)] == COLUMNS:
            return existing

        # Column order changed. Rewriting is only safe while no deals have been
        # written -- otherwise every existing row would silently misalign.
        data_rows = max(0, len(ws.col_values(1)) - 1)
        if not data_rows:
            ws.update(range_name="A1", values=[COLUMNS])
            log.info("rewrote the header row to the current column order")
            return COLUMNS

        raise RuntimeError(
            "the sheet's column order differs from the expected one and it already "
            "holds %d data rows, so rewriting the header would misalign them. "
            "Either move the columns by hand to match, or start a new worksheet.\n"
            "expected: %s\nfound:    %s" % (data_rows, COLUMNS, existing)
        )

    def append(self, records: Iterable[Any]) -> Dict[str, int]:
        """Append new deals. Returns deal_id -> sheet row number."""
        records = list(records)
        if not records:
            return {}
        ws = self.worksheet
        self.ensure_header()
        first_new_row = len(ws.col_values(1)) + 1
        ws.append_rows(
            [record_to_row(r) for r in records],
            value_input_option="USER_ENTERED",
            insert_data_option="INSERT_ROWS",
            table_range="A1",
        )
        row_for = {rec.deal_id: first_new_row + i for i, rec in enumerate(records)}
        self.write_rich_sources(records, row_for)
        return row_for

    def write_rich_sources(self, records: Iterable[Any], row_for: Dict[str, int]) -> int:
        """Replace the plain sources text with individually clickable outlets.

        Best-effort: if it fails the cell keeps the plain-text version written
        by the append, which is still complete, just not clickable.
        """
        header = self.worksheet.row_values(1) or COLUMNS
        if "sources" not in header:
            return 0
        col = header.index("sources")
        requests = []
        for rec in records:
            row = row_for.get(rec.deal_id)
            if not row or not rec.sources:
                continue
            requests.append({
                "updateCells": {
                    "range": {
                        "sheetId": self.worksheet.id,
                        "startRowIndex": row - 1, "endRowIndex": row,
                        "startColumnIndex": col, "endColumnIndex": col + 1,
                    },
                    "rows": [{"values": [sources_rich_cell(rec)]}],
                    "fields": "userEnteredValue,textFormatRuns",
                }
            })
        if not requests:
            return 0
        try:
            self.worksheet.spreadsheet.batch_update({"requests": requests})
        except Exception as exc:  # noqa: BLE001 - links are a nicety, rows are not
            log.warning("could not write clickable source links (%s); "
                        "the plain-text URLs are still in the cell", exc)
            return 0
        return len(requests)

    def update_sources(self, records: Iterable[Any], row_for: Dict[str, int]) -> int:
        """Refresh sources/source_count/conflicts on rows that gained an outlet."""
        updates = []
        header = self.ensure_header()
        for rec in records:
            row = row_for.get(rec.deal_id)
            if not row:
                log.warning("no sheet row known for %s; skipping update", rec.deal_id)
                continue
            values = record_to_row(rec)
            for col in UPDATABLE_COLUMNS:
                if col not in header:
                    continue
                idx = header.index(col)
                updates.append({
                    "range": "%s%d" % (_a1_col(idx), row),
                    "values": [[values[COLUMNS.index(col)]]],
                })
        if updates:
            self.worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        self.write_rich_sources(list(records), row_for)
        return len(updates)


CALIBRATION_EXTRA = [
    "fingerprint", "fingerprint_twin",
    "nn1_company", "nn1_score", "nn2_company", "nn2_score", "nn3_company", "nn3_score",
]
CALIBRATION_COLUMNS = [c for c in COLUMNS if c not in OPERATOR_COLUMNS] + CALIBRATION_EXTRA


def calibration_row(entry) -> List[Any]:
    row = record_to_row(entry["record"])
    row = row + [entry["fingerprint"], entry["fingerprint_twin"]]
    for i in range(3):
        if i < len(entry["neighbours"]):
            n = entry["neighbours"][i]
            row.extend([n.company, n.score])
        else:
            row.extend(["", ""])
    return row


class CalibrationWriter(SheetWriter):
    """Writes to its own worksheet so the main sheet stays one-row-per-deal."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.worksheet_name = cfg.get("sheets.calibration_worksheet", "calibration")

    def ensure_header(self) -> List[str]:
        ws = self.worksheet
        existing = ws.row_values(1)
        if not existing:
            ws.update(range_name="A1", values=[CALIBRATION_COLUMNS])
            return CALIBRATION_COLUMNS
        return existing

    def append_entries(self, entries) -> int:
        entries = list(entries)
        if not entries:
            return 0
        self.ensure_header()
        self.worksheet.append_rows(
            [calibration_row(e) for e in entries],
            value_input_option="USER_ENTERED",
            insert_data_option="INSERT_ROWS",
            table_range="A1",
        )
        return len(entries)
