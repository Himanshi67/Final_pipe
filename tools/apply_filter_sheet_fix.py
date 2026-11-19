"""Correct a client's filter sheets (2026-11-19 fix for shreejee).

Edits clients\\<slug>\\SubCategory_Attributes_Master.xlsx and
clients\\<slug>\\category_map.xlsx IDENTICALLY:
  - filter-group values (the sheet whose header is 'Filter Group': master
    'Sheet2', category_map 'Filters'), found by GROUP NAME, never by row
  - inserts the 'AC Mode' group (after 'AC Type') when it is missing
  - deletes the groups in DROP_GROUPS ('Discount': the owner deleted it from
    the master on 2026-11-19, so the copy loses it too)
  - sub-category rows ('Sub-Category Attributes'), found by parent and
    sub-category name, so each lists what the filter sheet lists; AC Ton
    values '1.0 Ton' -> '1 Ton' etc.

Owner decision 2026-11-19: the sheet lists follow the scraped data. No value
is added that no product and no owner sheet has (no 0.8 / 1.8 Ton, no 1 Star,
no 24" / 40" TV, no 50 / 100 / 144 Hz).

Idempotent: a second run changes nothing and says so. Refuses (exit 2) and
changes NEITHER workbook when either one is locked (an Excel `~$` lock file
exists, or the file cannot be opened for writing). Before it writes, it
copies both workbooks AS THEY ARE to clients\\<slug>\\backups\\
(<name>_<YYYYmmdd_HHMMSS>_before-fix.xlsx).

    .venv\\Scripts\\python.exe tools\\apply_filter_sheet_fix.py [--client shreejee]
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent

TV_SIZES = '32", 43", 50", 55", 65", 75"+'          # the owner's original list
AC_TONS = '1 Ton, 1.2 Ton, 1.5 Ton, 2 Ton, 2.5 Ton, 3 Ton'
AC_TONS_OTHER = '1 Ton, 1.5 Ton, 2 Ton'
AC_STARS = '2 Star, 3 Star, 4 Star, 5 Star'
FILTER_VALUES = {
    'AC Capacity': AC_TONS,
    'AC Star Rating': AC_STARS,
    'TV Screen Size': TV_SIZES,
    'TV Display Tech': 'LED, QLED, OLED, Mini LED, QNED',
    'TV Refresh Rate': '60Hz, 120Hz',               # '(for gaming)' removed
    'WM Features': 'Built-in Heater, Inverter Tech, Steam Wash, Smart Wi-Fi, Direct Drive',
    'WP Technology': 'RO + UV, RO + UV + UF, UV + UF, Copper/Alkaline filter, '
                     'Non-Electrical (Gravity)',
}
NEW_GROUP_AFTER = ('AC Type', 'AC Mode', 'Hot & Cold')
DROP_GROUPS = ('Discount',)
FILTER_HEADER = 'filter group'
SUBCAT_SHEET = 'Sub-Category Attributes'

SUBCAT = {      # (parent, sub-category, column header) -> new cell
    ('Washing Machines', 'semi auto', 'Type'): 'Semi-Automatic',
    ('Washing Machines', 'Front Load', 'Type'): 'Fully Automatic Front Load',
    ('Washing Machines', 'Top Load', 'Type'): 'Fully Automatic Top Load',
    ('Washing Machines', 'semi auto', 'Star Rating'): '3 Star, 4 Star, 5 Star',
    ('Washing Machines', 'Front Load', 'Star Rating'): '3 Star, 4 Star, 5 Star',
    ('Washing Machines', 'Top Load', 'Star Rating'): '3 Star, 4 Star, 5 Star',
    ('LED', 'QLED,OLED,Qned , Mini LED', 'Type'): 'Google TV, Android TV, WebOS, Tizen',
    ('LED', 'QLED,OLED,Qned , Mini LED', 'Size'): TV_SIZES,
    ('LED', 'QLED,OLED,Qned , Mini LED', 'Capacity'): 'N/A',
    ('LED', 'QLED,OLED,Qned , Mini LED', 'Star Rating'): 'N/A',
    ('Home Appliances', 'Refrigerators', 'Type'):
        'Single Door, Double Door, Side-by-Side, French Door, Multi-Door',
    ('Home Appliances', 'Water Purifiers', 'Type'):
        'RO + UV, RO + UV + UF, UV + UF, Copper/Alkaline filter, '
        'Non-Electrical (Gravity)',
    # the sub-category rows list what the filter sheet lists
    ('Air Conditioner', 'Split AC', 'Ton'): AC_TONS,
    ('Air Conditioner', 'Window AC', 'Ton'): AC_TONS_OTHER,
    ('Air Conditioner', 'Inverter AC', 'Ton'): AC_TONS_OTHER,
    ('Air Conditioner', 'Non-Inverter AC', 'Ton'): AC_TONS_OTHER,
    ('Air Conditioner', 'Split AC', 'Star Rating'): AC_STARS,
}
TON_FIX = {'1.0 Ton': '1 Ton', '2.0 Ton': '2 Ton', '3.0 Ton': '3 Ton'}


def _set(cell, new, label, changes):
    if cell.value != new:
        changes.append(f'{cell.coordinate} {label}: {cell.value!r} -> {new!r}')
        cell.value = new


def fix_subcat(ws, changes):
    # the FIRST header cell of each name ('Type' before the second 'type')
    head = {}
    for c in ws[1]:
        if c.value and str(c.value).strip() not in head:
            head[str(c.value).strip()] = c.column
    done = set()
    for row in ws.iter_rows(min_row=2):
        parent = str(row[0].value or '').strip()
        sub = str(row[1].value or '').strip()
        for (p, s, col), new in SUBCAT.items():
            if p == parent and s == sub:
                _set(ws.cell(row[0].row, head[col]), new, f'{sub}/{col}', changes)
                done.add((p, s, col))
        if parent == 'Air Conditioner' and (
                parent, sub, 'Ton') not in SUBCAT:
            cell = ws.cell(row[0].row, head['Ton'])
            parts = [p.strip() for p in str(cell.value or '').split(',') if p.strip()]
            new = ', '.join(TON_FIX.get(p, p) for p in parts)
            if parts:
                _set(cell, new, f'{sub}/Ton', changes)
    missing = set(SUBCAT) - done
    if missing:
        raise SystemExit(f'sub-category rows not found: {sorted(missing)}')


def _group_rows(ws):
    """{group name: row number}, read fresh (rows move on insert/delete)."""
    return {str(ws.cell(r, 1).value).strip(): r
            for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value}


def find_filter_sheet(wb):
    """The worksheet whose first cell is the 'Filter Group' header."""
    for ws in wb.worksheets:
        if str(ws.cell(1, 1).value or '').strip().lower() == FILTER_HEADER:
            return ws
    raise SystemExit(f'no filter sheet (header {FILTER_HEADER!r}) in the workbook')


def fix_filters(ws, changes):
    for group in DROP_GROUPS:
        names = _group_rows(ws)
        if group in names:
            row = names[group]
            old = ws.cell(row, 2).value
            ws.delete_rows(row)
            changes.append(f'filter row {row}: deleted {group!r} = {old!r}')
    names = _group_rows(ws)
    for group, new in FILTER_VALUES.items():
        if group not in names:
            raise SystemExit(f'filter group not found: {group!r}')
        _set(ws.cell(names[group], 2), new, f'filter {group}', changes)
    after, group, value = NEW_GROUP_AFTER
    names = _group_rows(ws)
    if group not in names:
        at = names[after] + 1
        ws.insert_rows(at)
        ws.cell(at, 1).value = group
        ws.cell(at, 2).value = value
        changes.append(f'filter row {at}: inserted {group!r} = {value!r}')
    elif ws.cell(names[group], 2).value != value:
        _set(ws.cell(names[group], 2), value, f'filter {group}', changes)


def lock_problem(path):
    """Why `path` cannot be written safely now, or '' when it can."""
    if not path.is_file():
        return f'{path} does not exist'
    lock = path.with_name('~$' + path.name)
    if lock.exists():
        return f'{path.name} is open in Excel (lock file {lock.name} exists)'
    try:
        with open(path, 'r+b'):
            pass
    except OSError as exc:
        return f'{path.name} cannot be opened for writing ({exc})'
    return ''


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--client', default='shreejee')
    args = ap.parse_args(argv)
    folder = ROOT / 'clients' / args.client
    master = folder / 'SubCategory_Attributes_Master.xlsx'
    catmap = folder / 'category_map.xlsx'

    problems = [p for p in (lock_problem(master), lock_problem(catmap)) if p]
    if problems:
        print('REFUSED - neither workbook was changed:')
        for p in problems:
            print(f'  {p}')
        print('Close the workbook in Excel and run this again.')
        return 2

    plan = []
    for path in (master, catmap):
        wb = openpyxl.load_workbook(path)
        changes = []
        fix_subcat(wb[SUBCAT_SHEET], changes)
        fix_filters(find_filter_sheet(wb), changes)
        plan.append((path, wb, changes))

    if not any(changes for _, _, changes in plan):
        print('Nothing to change - both workbooks already carry the fix.')
        return 0

    # a copy of each workbook AS IT IS NOW (incl. any edit the owner made by
    # hand) before anything is written
    backups = folder / 'backups'
    backups.mkdir(exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    for path, _, _ in plan:
        copy = backups / f'{path.stem}_{stamp}_before-fix{path.suffix}'
        shutil.copy2(path, copy)
        print(f'backup: {copy.relative_to(ROOT)}')

    # write both to temp files first, then swap them in, so a failure while
    # saving cannot leave one workbook edited and the other not
    temps = []
    try:
        for path, wb, changes in plan:
            fd, tmp = tempfile.mkstemp(suffix='.xlsx', dir=path.parent)
            os.close(fd)
            wb.save(tmp)
            temps.append((tmp, path))
        for tmp, path in temps:
            os.replace(tmp, path)
    finally:
        for tmp, _ in temps:
            if os.path.exists(tmp):
                os.remove(tmp)

    for path, _, changes in plan:
        print(f'{path.relative_to(ROOT)}: {len(changes)} change(s)')
        for c in changes:
            print(f'  {c}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
