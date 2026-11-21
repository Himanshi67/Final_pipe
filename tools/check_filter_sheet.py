"""Check a client's filter sheets (a config check, not a pipeline stage).

The attributes master (client.toml `attributes_master`) holds the
'Sub-Category Attributes' sheet and the filter sheet ('Filter Group | Filter
Values'); category_map.xlsx holds copies of both. app.py builds the products'
filter attributes from them (app.filter_attributes). This tool prints:

  1. every disagreement between the sub-category sheet and the filter sheet,
     per axis -> filter group: values in one and not the other, and values
     that are the same number but spelled differently ('1.0 Ton' / '1 Ton' -
     the filter-sheet spelling is the one written);
  2. cells that cannot be parsed into a value list (free-text hints with no
     list and no 'e.g.' format, mojibake, empty filter groups);
  3. filter groups no sub-category axis and no app.DIRECT_FILTER_GROUPS
     entry feeds (Brand, Price, ... - computed by the store);
  4. every difference between the master and the category_map.xlsx copies;
  5. the owner's category list (the master sheet with the header 'Parent
     Category | Sub-Categories', 'Sheet1' today; owner decision 5 of
     2026-11-19) against (a) the rows of 'Sub-Category Attributes' and (b) the
     first sheet of category_map.xlsx (same shape): a sub-category in one and
     not the other, and the same name spelled differently ('Trolly Bag' /
     'Trolley Bag' - fixed in the shreejee sheets on 2026-11-20 -, 'Top load' /
     'Top Load'). Nothing is edited.

Read-only. Usage:
    .venv\\Scripts\\python.exe tools\\check_filter_sheet.py [--client SLUG]
Exit code 0 always (it reports; it does not gate).
"""
import argparse
import difflib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import client_config                      # noqa: E402  (after sys.path)

# filter groups WooCommerce computes itself; the pipeline never writes them
STORE_COMPUTED_GROUPS = frozenset(('brand', 'price', 'availability', 'discount',
                                   'fridge price range'))


def check(client):
    """{'disagreements', 'spelling', 'unparseable', 'unfed_groups',
    'copy_differences', 'sources'} for one client (lists of strings)."""
    import app
    records, source, problems = app._subcategory_sheet(client)
    groups = app.load_filter_groups(client)
    table = app.load_subcategory_filter_axes(client)
    report = {'disagreements': [], 'spelling': [], 'unparseable': [],
              'unfed_groups': [], 'copy_differences': [], 'category_list': [],
              'sources': {'sub-category sheet': source,
                          'filter sheet': next(
                              (p for p in app.attribute_workbooks(client)
                               if _has_filter_sheet(app, p)), '')}}
    report['unparseable'].extend(problems)
    every_value = [v for values in groups.values() for v in values]
    # groups app.DIRECT_FILTER_GROUPS feeds from the spec table / title: fed,
    # but their values cannot be checked against a sub-category row
    lookup = {name.lower(): name for name in groups}
    direct = {lookup[e.group.lower()] for e in app.DIRECT_FILTER_GROUPS
              if e.group.lower() in lookup}

    fed = {}                      # filter group -> values the rows list
    for row in records:
        sub, parent = row.get('Sub-Category', ''), row.get('Parent Category', '')
        if not sub:
            continue
        for axis in app.SUBCAT_AXES + (app.SUB_TYPE_AXIS,):
            cell = row.get(axis, '')
            if not cell or cell.upper() == 'N/A':
                continue
            spec = app.parse_axis_cell(cell)
            where = f'{sub} / {axis}'
            if spec is None:
                report['unparseable'].append(f'{where}: mojibake cell {cell!r}')
            elif spec.hint and not spec.values and not spec.free:
                report['unparseable'].append(
                    f'{where}: free-text hint with no list and no e.g. '
                    f'format, nothing is filled: {cell!r}')
            elif (not spec.hint and len(spec.values) == 1
                  and not any(ch.isdigit() for ch in spec.values[0])
                  and len(spec.values[0].split()) > 2
                  and not app.snap_filter_value(spec.values[0], every_value)):
                # one long value is a hint unless the filter sheet lists it
                # ('Fully Automatic Front Load')
                report['unparseable'].append(
                    f'{where}: one value that reads like a hint: {cell!r}')
        for axis, spec, mapped, _derived in app._filter_plan(sub, parent,
                                                              table, groups):
            if not mapped:
                continue
            for group in mapped:
                fed.setdefault(group, [])
            options = [o for g in mapped for o in groups.get(g, ())]
            for value in spec.values:
                hit = app.snap_filter_value(value, options, spec.unit)
                for group in mapped:
                    if app.snap_filter_value(value, groups.get(group, ()),
                                             spec.unit):
                        fed[group].append(value)
                if hit is None:
                    report['disagreements'].append(
                        f'{sub} / {axis} -> {" | ".join(mapped)}: '
                        f'{value!r} is in the sub-category sheet only')
                elif hit != value and app._allowed_entry(hit)[0] == 'exact':
                    report['spelling'].append(
                        f'{sub} / {axis}: {value!r} (sub-category sheet) = '
                        f'{hit!r} (filter sheet, written)')

    for group, values in groups.items():
        if not values:
            report['unparseable'].append(
                f'filter group {group!r}: no usable values (empty or mojibake)')
        if group in direct and group not in fed:
            continue                       # fed from the spec table / title
        if group not in fed:
            report['unfed_groups'].append(group)
            continue
        for option in values:
            kind = app._allowed_entry(option)[0]
            if kind == 'band':
                continue                   # a range is fed by any value in it
            if not any(app.snap_filter_value(v, [option]) for v in fed[group]):
                report['disagreements'].append(
                    f'filter group {group!r}: {option!r} is in the filter '
                    f'sheet only')
    report['copy_differences'] = app.attribute_sheet_differences(client)
    report['category_list'] = category_list_differences(client, records)
    return report


CATEGORY_LIST_HEADER = ('parent category', 'sub-categories')


def _compact(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def _is_list_sheet(rows):
    head = next((r for r in rows if any(_compact(c) for c in r)), ())
    return tuple(_compact(c).lower() for c in head[:2]) == CATEGORY_LIST_HEADER


def read_category_list(path, first_sheet_fallback=False):
    """{parent: [sub-categories]} from the sheet whose header is 'Parent
    Category | Sub-Categories' (one row per parent, a comma list), split the
    way app.load_category_map splits it. None when no such sheet. With
    `first_sheet_fallback` the workbook's first sheet is used when no sheet
    carries the header."""
    import app
    sheets = list(app._workbook_rows(path).values())    # workbook order
    rows = next((r for r in sheets if _is_list_sheet(r)), None)
    if rows is None and first_sheet_fallback and sheets:
        rows = sheets[0]
    if rows is None:
        return None
    out = {}
    for row in rows:
        parent = _compact(row[0] if row else '')
        if not parent or parent.lower() == CATEGORY_LIST_HEADER[0]:
            continue
        subs = app._split_subcategories(row[1] if len(row) > 1 else '')
        out.setdefault(parent, []).extend(_compact(s) for s in subs)
    return out


def _subcat_rows_as_list(records):
    """The 'Sub-Category Attributes' rows as {parent: [sub-categories]}; a
    comma-joined row name ('QLED,OLED,Qned , Mini LED') is split like the
    category list, as app.filter_axes does."""
    import app
    out = {}
    for row in records:
        parent = _compact(row.get('Parent Category'))
        sub = row.get('Sub-Category', '')
        if parent and _compact(sub):
            out.setdefault(parent, []).extend(
                _compact(s) for s in app._split_subcategories(sub))
    return out


def compare_category_lists(left, right, left_name, right_name):
    """Human-readable differences between two {parent: [subs]} lists: a
    parent or sub-category in one only, and the same name spelled
    differently (case, or a near spelling under the same parent)."""
    out = []
    lp = {p.lower(): p for p in left}
    rp = {p.lower(): p for p in right}
    for key in sorted(set(lp) | set(rp)):
        if key not in rp:
            out.append(f'parent {lp[key]!r}: in {left_name} only')
            continue
        if key not in lp:
            out.append(f'parent {rp[key]!r}: in {right_name} only')
            continue
        if lp[key] != rp[key]:
            out.append(f'parent spelling: {lp[key]!r} ({left_name}) / '
                       f'{rp[key]!r} ({right_name})')
        a, b = left[lp[key]], right[rp[key]]
        a_only = [s for s in a if s not in b]
        b_only = [s for s in b if s not in a]
        for s in list(a_only):
            twin = next((t for t in b_only if t.lower() == s.lower()), None)
            if twin is None:
                near = difflib.get_close_matches(s.lower(),
                                                 [t.lower() for t in b_only],
                                                 n=1, cutoff=0.85)
                twin = next((t for t in b_only if near and
                             t.lower() == near[0]), None)
            if twin is not None:
                out.append(f'{rp[key]} / spelling: {s!r} ({left_name}) / '
                           f'{twin!r} ({right_name})')
                a_only.remove(s)
                b_only.remove(twin)
        out.extend(f'{rp[key]} / {s!r}: in {left_name} only' for s in a_only)
        out.extend(f'{rp[key]} / {s!r}: in {right_name} only' for s in b_only)
    return out


def category_list_differences(client, records):
    """Owner decision 5 (2026-11-19): the master's category list ('Sheet1')
    against the sub-category rows and the category_map.xlsx list."""
    import app
    master = app.attributes_master_path(client)
    if not master:
        return []
    try:
        owner = read_category_list(master)
    except Exception as exc:
        return [f'cannot read the category list in {os.path.basename(master)}: '
                f'{exc}']
    if owner is None:
        return [f'{os.path.basename(master)} has no sheet with the header '
                f"'Parent Category | Sub-Categories'"]
    name = 'Sheet1'
    out = [f'vs Sub-Category Attributes: {d}' for d in compare_category_lists(
        owner, _subcat_rows_as_list(records), name, 'Sub-Category Attributes')]
    catmap = app.category_map_path(client)
    if catmap and catmap.lower().endswith('.xlsx'):
        try:
            mapped = read_category_list(catmap, first_sheet_fallback=True)
        except Exception as exc:
            mapped, exc_text = None, str(exc)
            out.append(f'cannot read {os.path.basename(catmap)}: {exc_text}')
        if mapped is not None:
            out.extend(f'vs category_map.xlsx: {d}' for d in
                       compare_category_lists(owner, mapped, name,
                                              'category_map.xlsx'))
    return out


def _has_filter_sheet(app, path):
    try:
        return app.read_filter_groups(path) is not None
    except Exception:
        return False


def summary(report):
    keys = ('disagreements', 'spelling', 'unparseable', 'unfed_groups',
            'copy_differences', 'category_list')
    return {k: len(report.get(k, ())) for k in keys}


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    client_config.cli_client(ap)
    args = ap.parse_args(argv)
    client = client_config.load_client(args.client)
    report = check(client)
    print(f'Client: {client.name} ({client.slug})')
    for label, path in report['sources'].items():
        print(f'  {label}: {path or "(none found)"}')
    sections = (
        ('disagreements', 'Sub-category sheet vs filter sheet'),
        ('spelling', 'Same value, different spelling (filter sheet wins)'),
        ('unparseable', 'Cells that give no value list'),
        ('unfed_groups', 'Filter groups nothing in the pipeline feeds'),
        ('copy_differences', 'Master vs category_map.xlsx copies'),
        ('category_list', "Category list ('Sheet1') vs the other lists"),
    )
    for key, title in sections:
        items = report[key]
        print(f'\n{title}: {len(items)}')
        for item in items:
            note = (' (computed by the store)' if key == 'unfed_groups'
                    and item.lower() in STORE_COMPUTED_GROUPS else '')
            print(f'  - {item}{note}')
    print('\nSummary: ' + ', '.join(f'{k}={v}' for k, v in summary(report).items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
