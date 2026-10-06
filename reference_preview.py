"""Bounded lexical citations, never inferred agreement or automatic links."""
import re

PATTERN = re.compile(r'(?<![\w#])#([1-9][0-9]{0,15})(?![0-9])')
SCAN_LIMIT = 7000


def mentions(message, before):
    if not isinstance(message, str):
        return []
    # Read one extra character so a number cut at the boundary is not invented.
    return sorted({int(m[1]) for m in PATTERN.finditer(message[:SCAN_LIMIT + 1])
                   if m.end() <= SCAN_LIMIT and int(m[1]) < before})


def citation_preview(db, message, refs, before):
    detected = [n for n in mentions(message, before)
                if db.execute('SELECT 1 FROM history.identities WHERE seq=?', (n,)).fetchone()]
    unlinked = sorted(set(detected) - set(refs))
    return dict(detected_numbers=detected, unlinked_numbers=unlinked,
                unlinked_count=len(unlinked), advisory=True,
                notices=['Some numbered mentions are absent from references; inspect quotations and examples before linking.'] if unlinked else [])
