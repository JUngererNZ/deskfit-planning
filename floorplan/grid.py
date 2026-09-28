"""Floor-plan geometry constants.

All values here were derived from DEMO-SEATING-PLAN.xlsx and cross-checked
against the reference PNG. Keeping them in one place makes the layout easy to
adjust without touching the importer or the renderer.
"""

# The spreadsheet grid: columns A-I, rows 1-41.
GRID_COLUMNS = 9
GRID_ROWS = 41

COLUMN_LETTERS = 'ABCDEFGHI'


def col_to_index(letter):
    """'A' -> 1, 'I' -> 9."""
    return COLUMN_LETTERS.index(letter) + 1


def index_to_col(index):
    """1 -> 'A'."""
    return COLUMN_LETTERS[index - 1]


# Fixed areas present in the source spreadsheet, keyed by their cell reference.
# 'MEETING  ROOM' appears in the file with a double space; it is normalised to
# 'MEETING ROOM' for display.
SPREADSHEET_FIXED_AREAS = {
    'A1': 'BACK WALL',
    'C2': 'DOOR',
    'I2': 'WINDOW',
    'A3': 'MEETING ROOM',
    'A16': 'HUDDLE',
    'A19': 'DOOR',
}

# The merged BACK WALL spans A1:H1 in the spreadsheet. Kept for rendering.
BACK_WALL_SPAN = (1, 8, 1, 1)  # start_col, end_col, start_row, end_row

# Fixed areas required by the specification but absent from the spreadsheet.
# They are placed in genuinely unoccupied floor space: rows 6, 9, 12, 15, 18,
# 21 and 31 are entirely empty, and columns C, F and I are open corridors.
# Stored as (start_col, end_col, start_row, end_row, label).
SUPPLEMENTARY_FIXED_AREAS = [
    (3, 6, 9, 12, 'KITCHENETTE'),
    (3, 6, 18, 21, 'BREAKOUT ROOM'),
    (9, 9, 6, 6, 'PHOTOCOPIER'),
    (3, 6, 31, 31, 'SALES STANDUP AREA'),
]

# 'SPARE' is a desk status rather than a person, so the cell is left empty.
DESK_STATUS_LABELS = {'SPARE'}

# Names matching this pattern are placeholders awaiting real staff details.
PLACEHOLDER_PATTERN = r'^HUMAN\d{2}$'


def is_placeholder_name(name):
    """True for 'HUMAN01'-style names and the bare 'PERSON' label."""
    import re

    name = (name or '').strip()
    return bool(re.match(PLACEHOLDER_PATTERN, name)) or name == 'PERSON'
