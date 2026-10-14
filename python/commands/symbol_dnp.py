"""
The Do Not Populate (DNP) flag of placed symbols, edited in the file text.

A placed symbol carries ``(dnp yes)`` or ``(dnp no)`` as a direct child, next
to ``(in_bom ...)`` and ``(on_board ...)``. KiCad 10 design variants can hold
their own ``(dnp ...)`` deeper inside ``(instances ...)``; those are left
alone, so only direct children are read or written here. A part with several
units has one placed symbol per unit, all with the same reference, and each
carries the flag.
"""

import re
from typing import List, Optional, Tuple

_TAG = re.compile(r"\(\s*([^\s()\"]+)")


def direct_children(text: str, start: int = 0) -> List[Tuple[str, int, int]]:
    """(tag, start, end) of each list directly inside the list that opens at text[start].

    *end* is the index of the child's closing paren. Parens inside quoted
    strings are skipped; KiCad writes them unescaped in pin names.
    """
    children: List[Tuple[str, int, int]] = []
    depth = 0
    child_start = -1
    i = start
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
            if depth == 2:
                child_start = i
        elif ch == ")":
            if depth == 2:
                m = _TAG.match(text, child_start)
                children.append((m.group(1) if m else "", child_start, i))
            depth -= 1
            if depth == 0:
                break
        i += 1
    return children


def placed_symbols(content: str, reference: str) -> List[Tuple[int, int]]:
    """(start, end) of each placed symbol with this reference in a .kicad_sch file."""
    ref = re.compile(r'\(property\s+"Reference"\s+"' + re.escape(reference) + r'"')
    root = content.find("(kicad_sch")
    if root < 0:
        return []
    return [
        (start, end)
        for tag, start, end in direct_children(content, root)
        if tag == "symbol" and ref.search(content, start, end)
    ]


def symbol_dnp(block: str) -> Optional[bool]:
    """The DNP flag of one placed symbol's text, or None when it has none."""
    for tag, start, end in direct_children(block):
        if tag == "dnp":
            return block[start:end].split()[-1] == "yes"
    return None


def set_symbol_dnp(block: str, dnp: bool) -> str:
    """One placed symbol's text with its DNP flag set."""
    flag = f"(dnp {'yes' if dnp else 'no'})"
    children = direct_children(block)
    for tag, start, end in children:
        if tag == "dnp":
            return block[:start] + flag + block[end + 1 :]
    # KiCad writes (dnp ...) after (on_board ...), which follows (in_bom ...).
    after = [c for c in children if c[0] in ("on_board", "in_bom", "unit", "at")]
    order = {"on_board": 0, "in_bom": 1, "unit": 2, "at": 3}
    if after:
        _, _, end = min(after, key=lambda c: order[c[0]])
        return block[: end + 1] + " " + flag + block[end + 1 :]
    return block[:-1] + " " + flag + block[-1:]


def set_dnp(content: str, reference: str, dnp: bool) -> Tuple[str, int]:
    """*content* with every unit of *reference* set to *dnp*, and how many units it set."""
    blocks = placed_symbols(content, reference)
    for start, end in reversed(blocks):
        content = (
            content[:start] + set_symbol_dnp(content[start : end + 1], dnp) + content[end + 1 :]
        )
    return content, len(blocks)
