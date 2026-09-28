"""Reads DEMO-SEATING-PLAN.xlsx and converts it into floor-plan records.

Parsing rules (verified against the source file):

* Columns A-I, rows 1-41.
* Six cells are fixed areas; they become non-assignable Desk rows.
* Every other occupied cell is a desk. The first occurrence of a name in
  reading order (top-down, then left-to-right) keeps its seat; later duplicates
  are freed to 'Spare'. The source file contains 16 names repeated across 43
  cells, which would otherwise place one person at up to 8 desks.
* 'SPARE' denotes an empty desk, not a person.
"""

import logging
import re
from dataclasses import dataclass, field

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

from . import grid as grid_const

logger = logging.getLogger(__name__)


@dataclass
class ParsedDesk:
    grid_col: int
    grid_row: int
    kind: str
    label: str = ''
    is_assignable: bool = True


@dataclass
class ParsedPerson:
    name: str
    grid_col: int
    grid_row: int
    is_placeholder: bool = False


@dataclass
class ParseResult:
    desks: list = field(default_factory=list)
    people: list = field(default_factory=list)
    fixed_areas: list = field(default_factory=list)
    freed_duplicates: list = field(default_factory=list)
    empty_desks: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _cell_text(value):
    if value is None:
        return ''
    return str(value).strip()


def parse_workbook(path):
    """Parse the spreadsheet into desks and people. Does not touch the database."""
    result = ParseResult()
    workbook = load_workbook(path, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]

    fixed_refs = dict(grid_const.SPREADSHEET_FIXED_AREAS)
    # Track which grid cells hold a desk occupant, in reading order.
    claimed = {}

    for row in sheet.iter_rows(
            min_row=1, max_row=grid_const.GRID_ROWS,
            min_col=1, max_col=grid_const.GRID_COLUMNS):
        for cell in row:
            text = _cell_text(cell.value)
            if not text:
                continue

            ref = f'{cell.column_letter}{cell.row}'
            col_idx = column_index_from_string(cell.column_letter)

            if ref in fixed_refs:
                result.fixed_areas.append(
                    ParsedDesk(col_idx, cell.row, 'fixed',
                               fixed_refs[ref], False))
                continue

            # A fixed-area label appearing outside its known cell is a layout
            # drift; treat it as fixed and warn rather than seating a person.
            if text.upper() in fixed_refs.values():
                result.fixed_areas.append(
                    ParsedDesk(col_idx, cell.row, 'fixed', text.upper(), False))
                result.warnings.append(
                    f'Cell {ref}: unrecognised fixed area "{text}" placed as fixed')
                continue

            if text.upper() in grid_const.DESK_STATUS_LABELS:
                # Empty desk, explicitly marked spare in the source.
                result.empty_desks.append(
                    ParsedDesk(col_idx, cell.row, 'desk', '', True))
                continue

            if text in claimed:
                holder = claimed[text]
                result.empty_desks.append(
                    ParsedDesk(col_idx, cell.row, 'desk', '', True))
                result.freed_duplicates.append(
                    {'name': text, 'kept_at': holder, 'freed_at': ref})
                continue

            claimed[text] = ref
            result.people.append(
                ParsedPerson(text, col_idx, cell.row,
                             grid_const.is_placeholder_name(text)))

    # Desks are only the cells the spreadsheet actually defines. Cells with a
    # person, an explicit SPARE marker, or a freed duplicate all become desk
    # rows; genuinely blank cells stay absent so the rendered plan keeps the
    # real footprint (aisles and corridors included) rather than a dense grid.
    for item in result.people:
        result.empty_desks.append(
            ParsedDesk(item.grid_col, item.grid_row, 'desk', '', True))

    result.desks = result.fixed_areas + result.empty_desks

    if result.freed_duplicates:
        logger.warning(
            'Resolved %d duplicate occupant cells across %d names; '
            'the first occurrence in reading order kept its seat.',
            len(result.freed_duplicates),
            len({d['name'] for d in result.freed_duplicates}))

    return result
