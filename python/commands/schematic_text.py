"""
Text notes in a parsed schematic.

The functions here take the list that ``sexpdata.loads`` returns for a
.kicad_sch file; apply_text_edit returns a new item rather than changing it.

A text note is a ``(text ...)`` or a ``(text_box ...)`` item. Its horizontal
justification is written as a ``left`` or ``right`` token in ``(justify ...)``.
KiCad has no ``center`` token: a centred text simply leaves the horizontal
token out, and KiCad 10 refuses to load a file with ``(justify center ...)``.
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from sexpdata import Symbol

_SYM_TEXT = Symbol("text")
_SYM_TEXT_BOX = Symbol("text_box")
_SYM_AT = Symbol("at")
_SYM_SIZE = Symbol("size")
_SYM_UUID = Symbol("uuid")
_SYM_EFFECTS = Symbol("effects")
_SYM_FONT = Symbol("font")
_SYM_JUSTIFY = Symbol("justify")
_SYM_BOLD = Symbol("bold")
_SYM_ITALIC = Symbol("italic")
_SYM_YES = Symbol("yes")

TEXT_KINDS = (_SYM_TEXT, _SYM_TEXT_BOX)
HORIZONTAL = ("left", "center", "right")


@dataclass(frozen=True)
class TextEdit:
    """Changes to one text note. A field left as None is not changed."""

    text: Optional[str] = None
    position: Optional[Tuple[float, float]] = None
    angle: Optional[float] = None
    font_size: Optional[float] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    justify: Optional[str] = None

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in self.__dataclass_fields__)


def justify_tokens(horizontal: str, vertical: Optional[str] = "bottom") -> List[Symbol]:
    """The (justify ...) tokens for a horizontal justification and a vertical one."""
    tokens = [] if horizontal == "center" else [Symbol(horizontal)]
    if vertical:
        tokens.append(Symbol(vertical))
    return tokens


def _child(item: list, tag: Symbol) -> Optional[list]:
    return next((p for p in item if isinstance(p, list) and p and p[0] == tag), None)


def _is_text(item: Any) -> bool:
    return isinstance(item, list) and len(item) >= 2 and item[0] in TEXT_KINDS


def _flag(font: Optional[list], tag: Symbol) -> bool:
    """A font flag: KiCad 7+ writes (bold yes); older files write a bare bold."""
    if font is None:
        return False
    for part in font[1:]:
        if part == tag:
            return True
        if isinstance(part, list) and part and part[0] == tag:
            return len(part) < 2 or str(part[1]) == "yes"
    return False


def describe_text(item: list) -> Dict[str, Any]:
    """The fields of one (text ...) or (text_box ...) item."""
    at = _child(item, _SYM_AT) or [_SYM_AT, 0, 0, 0]
    effects = _child(item, _SYM_EFFECTS) or [_SYM_EFFECTS]
    font = _child(effects, _SYM_FONT)
    size = _child(font, _SYM_SIZE) if font else None
    justify = _child(effects, _SYM_JUSTIFY)
    tokens = [str(t) for t in justify[1:]] if justify else []
    uid = _child(item, _SYM_UUID)
    described: Dict[str, Any] = {
        "kind": str(item[0]),
        "text": str(item[1]),
        "position": {"x": float(at[1]), "y": float(at[2])},
        "angle": float(at[3]) if len(at) >= 4 else 0.0,
        "font_size": float(size[1]) if size else 1.27,
        "bold": _flag(font, _SYM_BOLD),
        "italic": _flag(font, _SYM_ITALIC),
        "justify": next((t for t in tokens if t in ("left", "right")), "center"),
        "uuid": str(uid[1]) if uid else "",
    }
    box = _child(item, _SYM_SIZE)
    if item[0] == _SYM_TEXT_BOX and box is not None:
        described["size"] = {"width": float(box[1]), "height": float(box[2])}
    return described


def list_texts(sch_data: list) -> List[Dict[str, Any]]:
    return [describe_text(item) for item in sch_data if _is_text(item)]


def find_text(
    sch_data: list,
    uuid: Optional[str] = None,
    position: Optional[Tuple[float, float]] = None,
    tolerance: float = 0.5,
) -> Union[int, str]:
    """Index of the text note with this uuid, or nearest *position*; or a reason none matched."""
    if uuid:
        for i, item in enumerate(sch_data):
            uid = _child(item, _SYM_UUID) if _is_text(item) else None
            if uid is not None and str(uid[1]) == uuid:
                return i
        return f"No text note has uuid {uuid}"
    if position is None:
        return "Pass uuid or position to pick the text note"
    near: List[Tuple[float, int]] = []
    for i, item in enumerate(sch_data):
        if not _is_text(item):
            continue
        at = _child(item, _SYM_AT)
        if at is None:
            continue
        dist = math.hypot(float(at[1]) - position[0], float(at[2]) - position[1])
        if dist <= tolerance:
            near.append((dist, i))
    if not near:
        return f"No text note within {tolerance} mm of ({position[0]}, {position[1]})"
    near.sort()
    if len(near) > 1 and near[1][0] == near[0][0]:
        return (
            f"{len(near)} text notes sit at ({position[0]}, {position[1]}); "
            "pass the uuid from list_schematic_texts"
        )
    return near[0][1]


def _set_flag(font: list, tag: Symbol, on: bool) -> list:
    kept = [p for p in font if p != tag and not (isinstance(p, list) and p and p[0] == tag)]
    return kept + [[tag, _SYM_YES]] if on else kept


def _edit_font(font: Optional[list], edit: TextEdit) -> list:
    font = list(font) if font is not None else [_SYM_FONT, [_SYM_SIZE, 1.27, 1.27]]
    if edit.font_size is not None:
        sized = [_SYM_SIZE, edit.font_size, edit.font_size]
        if _child(font, _SYM_SIZE) is None:
            font.insert(1, sized)
        else:
            font = [sized if isinstance(p, list) and p and p[0] == _SYM_SIZE else p for p in font]
    if edit.bold is not None:
        font = _set_flag(font, _SYM_BOLD, edit.bold)
    if edit.italic is not None:
        font = _set_flag(font, _SYM_ITALIC, edit.italic)
    return font


def _edit_justify(justify: Optional[list], horizontal: str) -> list:
    """Replace the horizontal token, keeping top/bottom and mirror."""
    old = [str(t) for t in justify[1:]] if justify else []
    kept = [Symbol(t) for t in old if t not in ("left", "right", "center")]
    return [_SYM_JUSTIFY] + ([] if horizontal == "center" else [Symbol(horizontal)]) + kept


def _edit_effects(effects: Optional[list], edit: TextEdit) -> list:
    effects = list(effects) if effects is not None else [_SYM_EFFECTS]
    if edit.font_size is not None or edit.bold is not None or edit.italic is not None:
        font = _edit_font(_child(effects, _SYM_FONT), edit)
        if _child(effects, _SYM_FONT) is None:
            effects.insert(1, font)
        else:
            effects = [
                font if isinstance(p, list) and p and p[0] == _SYM_FONT else p for p in effects
            ]
    if edit.justify is not None:
        old = _child(effects, _SYM_JUSTIFY)
        new = _edit_justify(old, edit.justify)
        rest = [p for p in effects if p is not old]
        effects = rest + [new] if len(new) > 1 else rest
    return effects


def apply_text_edit(item: list, edit: TextEdit) -> list:
    """A copy of a text note item with *edit* applied."""
    out = list(item)
    if edit.text is not None:
        out[1] = edit.text
    if edit.position is not None or edit.angle is not None:
        old_at = _child(out, _SYM_AT) or [_SYM_AT, 0, 0, 0]
        x, y = edit.position if edit.position is not None else (old_at[1], old_at[2])
        angle = edit.angle if edit.angle is not None else (old_at[3] if len(old_at) >= 4 else 0)
        new_at = [_SYM_AT, x, y, angle]
        if _child(out, _SYM_AT) is None:
            out.insert(2, new_at)
        else:
            out = [new_at if isinstance(p, list) and p and p[0] == _SYM_AT else p for p in out]
    if (
        edit.font_size is not None
        or edit.bold is not None
        or edit.italic is not None
        or (edit.justify is not None)
    ):
        old = _child(out, _SYM_EFFECTS)
        new = _edit_effects(old, edit)
        if old is None:
            uid_index = next(
                (i for i, p in enumerate(out) if isinstance(p, list) and p and p[0] == _SYM_UUID),
                len(out),
            )
            out.insert(uid_index, new)
        else:
            out = [new if p is old else p for p in out]
    return out
