"""Text note and junction handlers for KiCADInterface.

Mixed into KiCADInterface like SchematicHandlersMixin. Each handler reads the
sheet, changes the parsed list through commands.schematic_text or
commands.schematic_junctions, and writes the sheet back.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import sexpdata
from commands.schematic_junctions import add_junction, delete_junction, list_junctions
from commands.schematic_text import (
    HORIZONTAL,
    TextEdit,
    apply_text_edit,
    describe_text,
    find_text,
)
from utils.interactive_schematic import reload_kicad_schematic
from utils.sexpr_format import dumps as kicad_dumps

logger = logging.getLogger("kicad_interface")


def _point(value: Any, what: str) -> Tuple[float, float]:
    """Accept {x, y} or [x, y]; raise ValueError otherwise."""
    if isinstance(value, dict) and "x" in value and "y" in value:
        return (float(value["x"]), float(value["y"]))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (float(value[0]), float(value[1]))
    raise ValueError(f"{what} must be {{x, y}} or [x, y]")


def _sheet(params: Dict[str, Any]) -> Path:
    schematic_path = params.get("schematicPath")
    if not schematic_path:
        raise ValueError("schematicPath is required")
    path = Path(schematic_path)
    if not path.is_file():
        raise ValueError(f"Schematic not found: {schematic_path}")
    return path


def _load(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return sexpdata.loads(f.read())


def _save(path: Path, sch_data: list) -> None:
    text = kicad_dumps(sch_data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    reload_kicad_schematic()


def _optional_bool(params: Dict[str, Any], key: str) -> Optional[bool]:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def parse_text_edit(params: Dict[str, Any]) -> TextEdit:
    """The changes an edit_schematic_text call asks for; raise ValueError on bad input."""
    text = params.get("text")
    if text is not None and (not isinstance(text, str) or not text):
        raise ValueError("text must be a non-empty string; use delete_schematic_text to remove")
    font_size = params.get("fontSize")
    if font_size is not None and float(font_size) <= 0:
        raise ValueError("fontSize must be positive")
    justify = params.get("justify")
    if justify is not None and justify not in HORIZONTAL:
        raise ValueError(f"justify must be one of {', '.join(HORIZONTAL)}")
    new_position = params.get("newPosition")
    angle = params.get("angle")
    return TextEdit(
        text=text,
        position=_point(new_position, "newPosition") if new_position is not None else None,
        angle=float(angle) if angle is not None else None,
        font_size=float(font_size) if font_size is not None else None,
        bold=_optional_bool(params, "bold"),
        italic=_optional_bool(params, "italic"),
        justify=justify,
    )


def _pick_text(sch_data: list, params: Dict[str, Any]) -> Union[int, str]:
    position = params.get("position")
    return find_text(
        sch_data,
        uuid=params.get("uuid"),
        position=_point(position, "position") if position is not None else None,
    )


class SchematicMarkupHandlersMixin:
    """Text note and junction handlers mixed into KiCADInterface."""

    def _handle_edit_schematic_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Change the text, position, angle or style of one text note."""
        try:
            path = _sheet(params)
            edit = parse_text_edit(params)
            if edit.is_empty():
                return {
                    "success": False,
                    "message": (
                        "Nothing to change: pass text, newPosition, angle, fontSize, bold, "
                        "italic or justify"
                    ),
                }
            sch_data = _load(path)
            index = _pick_text(sch_data, params)
            if isinstance(index, str):
                return {"success": False, "message": index}
            before = describe_text(sch_data[index])
            sch_data[index] = apply_text_edit(sch_data[index], edit)
            _save(path, sch_data)
            return {
                "success": True,
                "message": f"Edited {before['kind']} {before['uuid']}",
                "before": before,
                "after": describe_text(sch_data[index]),
            }
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error editing schematic text")
            return {"success": False, "message": str(e)}

    def _handle_delete_schematic_text(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete one text note."""
        try:
            path = _sheet(params)
            sch_data = _load(path)
            index = _pick_text(sch_data, params)
            if isinstance(index, str):
                return {"success": False, "message": index}
            deleted = describe_text(sch_data[index])
            _save(path, sch_data[:index] + sch_data[index + 1 :])
            return {
                "success": True,
                "message": f"Deleted {deleted['kind']} {deleted['uuid']}",
                "deleted": deleted,
            }
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error deleting schematic text")
            return {"success": False, "message": str(e)}

    def _handle_list_schematic_junctions(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List the junctions on one sheet and what each joins."""
        try:
            junctions = list_junctions(_load(_sheet(params)))
            return {"success": True, "junctions": junctions, "count": len(junctions)}
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error listing junctions")
            return {"success": False, "message": str(e)}

    def _handle_add_schematic_junction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a junction where three or more wire or bus connections meet."""
        try:
            path = _sheet(params)
            point = _point(params.get("position"), "position")
            result = add_junction(_load(path), point)
            if isinstance(result, str):
                return {"success": False, "message": result}
            sch_data, report = result
            _save(path, sch_data)
            return {
                "success": True,
                "message": f"Added junction at ({point[0]}, {point[1]})",
                **report,
            }
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error adding junction")
            return {"success": False, "message": str(e)}

    def _handle_delete_schematic_junction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete the junction within 0.5 mm of a position."""
        try:
            path = _sheet(params)
            point = _point(params.get("position"), "position")
            result = delete_junction(_load(path), point)
            if isinstance(result, str):
                return {"success": False, "message": result}
            sch_data, report = result
            _save(path, sch_data)
            where = report["position"]
            return {
                "success": True,
                "message": f"Deleted junction at ({where['x']}, {where['y']})",
                **report,
            }
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error deleting junction")
            return {"success": False, "message": str(e)}
