"""CSV / XLSX extraction - one segment per row ('Column: value; ...')."""
from __future__ import annotations

import csv
import io

from openpyxl import load_workbook

from ria.config import get_settings
from ria.extractors.base import Extractor, corrupt, decode_text
from ria.models.enums import SegmentKind, WarningCode, WarningSeverity
from ria.models.segments import ExtractedBlock, ExtractionWarning, ExtractorOutput


def _rows_to_blocks(rows: list[list[str]], heading: str | None, limit: int,
                    out: list[ExtractedBlock]) -> bool:
    """Append row blocks; return True if rows were truncated."""
    rows = [[(c or "").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return False
    header = [h or f"Column {i + 1}" for i, h in enumerate(rows[0])]
    truncated = False
    for r in rows[1:]:
        if len(out) >= limit:
            truncated = True
            break
        pairs = [f"{h}: {c}" for h, c in zip(header, r) if c]
        if pairs:
            out.append(ExtractedBlock(text=" | ".join(pairs), kind=SegmentKind.TABLE_ROW, heading=heading))
    return truncated


class TabularExtractor(Extractor):
    extensions = (".csv", ".xlsx")

    def extract(self, data: bytes, file_name: str) -> ExtractorOutput:
        limit = get_settings().max_table_rows
        blocks: list[ExtractedBlock] = []
        truncated = False
        try:
            if file_name.lower().endswith(".csv"):
                text = decode_text(data)
                try:
                    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                rows = list(csv.reader(io.StringIO(text), dialect))
                truncated = _rows_to_blocks(rows, None, limit, blocks)
            else:
                wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
                for ws in wb.worksheets:
                    rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
                    truncated = _rows_to_blocks(rows, ws.title, limit, blocks) or truncated
                wb.close()
        except Exception as exc:  # noqa: BLE001
            return corrupt(f"The spreadsheet could not be read ({type(exc).__name__}).")

        warnings = []
        if truncated:
            warnings.append(ExtractionWarning(
                code=WarningCode.ROWS_TRUNCATED, severity=WarningSeverity.WARNING,
                message=f"Only the first {limit} rows were extracted.",
                action="Split the file or raise RIA_MAX_TABLE_ROWS."))
        return ExtractorOutput(blocks=blocks, warnings=warnings)
