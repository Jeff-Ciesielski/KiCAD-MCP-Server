"""
Bus label syntax for KiCad schematics.

KiCad treats a label as a bus when its text is a vector or a group:

* vector: ``PREFIX[A..B]``. The members are ``PREFIX<n>`` for every n between
  A and B. KiCad lists them in ascending order whichever way the range is
  written, so ``D[3..0]`` and ``D[0..3]`` have the same members.
* group: ``PREFIX{M1 M2 ...}``. Members are separated by spaces or commas.
  A member can be a plain name, a vector, or the name of a bus alias. With a
  prefix each member net is ``PREFIX.<member>``; without one it is the bare
  member name.

A bus alias is a named member list stored in the project file. It is only
expanded inside braces: ``{MEM}`` and ``U{MEM}`` are buses, a bare ``MEM`` is a
plain net label.

How members join across a sheet pin is left to KiCad (see
commands/design_netlist.py). The syntax rules come from kicad-cli 10.0.6 runs
on test schematics; docs/SCHEMATIC_BUSES.md has the cases and their results.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

BusAliases = Mapping[str, Sequence[str]]

VECTOR = "vector"
GROUP = "group"

# KiCad allows very wide vectors; this cap stops a typo such as D[0..99999999]
# from building a huge member list.
MAX_VECTOR_WIDTH = 4096

# The prefix may hold text formatting such as ~{CS}; KiCad keeps it in the
# member names (~{CS}[0..1] carries ~{CS}0 and ~{CS}1).
_VECTOR_RE = re.compile(
    r"^(?P<prefix>(?:[^\s\[\]{}]|[~^_]\{[^\s{}]*\})*)\[(?P<a>\d+)\.\.(?P<b>\d+)\]$"
)
_MEMBER_SPLIT_RE = re.compile(r"[\s,]+")
# ~{..}, ^{..} and _{..} are KiCad text formatting (overbar, superscript,
# subscript), not group braces.
_FORMATTING_MARKERS = "~^_"


@dataclass(frozen=True)
class BusLabel:
    """A parsed bus label.

    ``local_members`` are the member names before the group prefix is applied
    (for a vector they equal ``members``). Group members pair across a sheet pin
    by these names. ``members`` are the net names the bus carries.
    """

    text: str
    kind: str
    prefix: str
    local_members: Tuple[str, ...]
    members: Tuple[str, ...]


def _expand_vector(text: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    match = _VECTOR_RE.match(text)
    if match is None:
        return None
    lo, hi = sorted((int(match.group("a")), int(match.group("b"))))
    if lo == hi:
        return None  # KiCad does not treat D[2..2] as a bus
    if hi - lo + 1 > MAX_VECTOR_WIDTH:
        raise ValueError(
            f"Bus vector '{text}' has {hi - lo + 1} members; the limit is {MAX_VECTOR_WIDTH}"
        )
    prefix = match.group("prefix")
    return prefix, tuple(f"{prefix}{n}" for n in range(lo, hi + 1))


def _skip_formatting(text: str, i: int) -> Optional[int]:
    """If a formatting span such as ~{X} starts at *i*, return the index after it."""
    if text[i] in _FORMATTING_MARKERS and text[i + 1 : i + 2] == "{":
        close = text.find("}", i + 2)
        return close + 1 if close != -1 else len(text)
    return None


def _split_group(text: str) -> Optional[Tuple[str, str]]:
    """Split a group label into (prefix, body), or return None.

    Formatting spans (~{..}, ^{..}, _{..}) are not group braces. KiCad
    ignores text after the closing brace ({A B}x carries A and B), rejects a
    group inside a group, and rejects a prefix that holds a vector.
    """
    i = 0
    while i < len(text):
        skipped = _skip_formatting(text, i)
        if skipped is not None:
            i = skipped
            continue
        if text[i] == "{":
            j = i + 1
            while j < len(text):
                skipped = _skip_formatting(text, j)
                if skipped is not None:
                    j = skipped
                    continue
                if text[j] == "{":
                    return None
                if text[j] == "}":
                    prefix, suffix = text[:i], text[j + 1 :]
                    if any(ch.isspace() or ch in "[]" for ch in prefix + suffix):
                        return None
                    return prefix, text[i + 1 : j]
                j += 1
            return None
        i += 1
    return None


def _expand_group_member(
    member: str, aliases: BusAliases, seen_aliases: Tuple[str, ...]
) -> List[str]:
    vector = _expand_vector(member)
    if vector is not None:
        return list(vector[1])
    if member in aliases:
        if member in seen_aliases:
            raise ValueError(f"Bus alias '{member}' refers to itself")
        expanded: List[str] = []
        for alias_member in aliases[member]:
            expanded.extend(_expand_group_member(alias_member, aliases, seen_aliases + (member,)))
        return expanded
    return [member]


def parse_bus_label(text: str, aliases: Optional[BusAliases] = None) -> Optional[BusLabel]:
    """Parse *text* as a bus label, or return None when it names a single net.

    Raises ValueError for a vector wider than MAX_VECTOR_WIDTH or an alias
    that refers to itself.
    """
    aliases = aliases or {}

    vector = _expand_vector(text)
    if vector is not None:
        prefix, members = vector
        return BusLabel(text, VECTOR, prefix, members, members)

    group = _split_group(text)
    if group is None:
        return None
    prefix, body = group
    raw_members = [m for m in _MEMBER_SPLIT_RE.split(body) if m]
    if not raw_members:
        return None
    local: List[str] = []
    for raw in raw_members:
        for name in _expand_group_member(raw, aliases, ()):
            if name not in local:
                local.append(name)
    members = tuple(f"{prefix}.{m}" for m in local) if prefix else tuple(local)
    return BusLabel(text, GROUP, prefix, tuple(local), members)


def is_bus_label(text: str, aliases: Optional[BusAliases] = None) -> bool:
    return parse_bus_label(text, aliases) is not None
