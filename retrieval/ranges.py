"""Searchable spans derived from materialized body blocks."""


def searchable_ranges(sections, section_ids=None):
    """Return body-only absolute ranges, excluding epigraphs and footnotes."""
    by_id = {row['id']: row for row in sections}
    if section_ids:
        allowed = set(section_ids)
        changed = True
        while changed:
            changed = False
            for row in sections:
                if row.get('parent_id') in allowed and row['id'] not in allowed:
                    allowed.add(row['id'])
                    changed = True
    else:
        allowed = set(by_id)

    ranges = []
    for row in sections:
        if row['id'] not in allowed:
            continue
        start, end = row.get('body_start_char'), row.get('body_end_char')
        if type(start) is not int or type(end) is not int or start >= end:
            continue
        cursor = start
        excluded = sorted(
            (max(start, block['start']), min(end, block['end']))
            for block in row.get('excluded_ranges', [])
            if type(block.get('start')) is int and type(block.get('end')) is int
            and block['start'] < end and block['end'] > start
        )
        for left, right in excluded:
            if cursor < left:
                ranges.append({'start': cursor, 'end': left})
            cursor = max(cursor, right)
        if cursor < end:
            ranges.append({'start': cursor, 'end': end})
    return sorted(ranges, key=lambda row: (row['start'], row['end']))
