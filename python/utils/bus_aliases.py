"""Read and write schematic bus aliases.

KiCad 9 and 10 keep bus aliases in the project file, under
``schematic.bus_aliases`` as ``{alias_name: [member, ...]}``. KiCad 8 and
earlier kept them in each schematic as ``(bus_alias "NAME" (members ...))``;
KiCad moves those into the project file when it upgrades the schematic. Reads
merge both places so an un-upgraded design still resolves; writes go to the
project file only.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from sexpdata import Symbol
from utils.project_settings_guard import _write_json_atomic
from utils.sheet_tree import _PROJECT_SEARCH_DIRS as PROJECT_SEARCH_DIRS
from utils.sheet_tree import sheet_tree

logger = logging.getLogger("kicad_interface")

_SYM_BUS_ALIAS = Symbol("bus_alias")
_SYM_MEMBERS = Symbol("members")


def find_project_file(schematic_path: Path) -> Optional[Path]:
    """Return the .kicad_pro that owns *schematic_path*, or None.

    Sub-sheets can live in sub-directories, so the search walks up from the
    schematic's directory, as far as sheet_tree.root_candidates looks, and
    stops at the first directory with a project file. Of several project files
    there, the one named like the schematic wins, then the one whose root
    sheet reaches the schematic. A directory of unrelated projects gives None.
    """
    schematic_path = Path(schematic_path).resolve()
    directory = schematic_path.parent
    for _ in range(PROJECT_SEARCH_DIRS):
        candidates = sorted(directory.glob("*.kicad_pro"))
        if candidates:
            for candidate in candidates:
                if candidate.stem == schematic_path.stem:
                    return candidate
            if len(candidates) == 1:
                return candidates[0]
            for candidate in candidates:
                root = candidate.with_suffix(".kicad_sch")
                if root.is_file() and schematic_path in {p.resolve() for p in sheet_tree(root)}:
                    return candidate
            return None
        if directory.parent == directory:
            break
        directory = directory.parent
    return None


def _read_project(pro_path: Path) -> dict:
    """Read a project file; raise ValueError when it is not a JSON object."""
    try:
        with open(pro_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{pro_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{pro_path} does not hold a JSON object")
    return data


def project_bus_aliases(pro_path: Optional[Path]) -> Dict[str, List[str]]:
    """Return the aliases stored in a project file (empty when it has none)."""
    if pro_path is None or not Path(pro_path).is_file():
        return {}
    schematic = _read_project(Path(pro_path)).get("schematic")
    raw = (schematic.get("bus_aliases") if isinstance(schematic, dict) else None) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"schematic.bus_aliases in {pro_path} is not an object")
    return {str(name): [str(m) for m in members] for name, members in raw.items()}


def schematic_bus_aliases(sexp: list) -> Dict[str, List[str]]:
    """Return the KiCad 8 style ``(bus_alias ...)`` entries of one parsed sheet."""
    aliases: Dict[str, List[str]] = {}
    for item in sexp:
        if not (isinstance(item, list) and len(item) >= 2 and item[0] == _SYM_BUS_ALIAS):
            continue
        members: List[str] = []
        for part in item[2:]:
            if isinstance(part, list) and part and part[0] == _SYM_MEMBERS:
                members.extend(str(m) for m in part[1:])
        aliases[str(item[1])] = members
    return aliases


def design_bus_aliases(
    pro_path: Optional[Path], sheet_sexps: Iterable[list]
) -> Dict[str, List[str]]:
    """Merge legacy per-sheet aliases with the project file's; the project file wins."""
    merged: Dict[str, List[str]] = {}
    for sexp in sheet_sexps:
        merged.update(schematic_bus_aliases(sexp))
    merged.update(project_bus_aliases(pro_path))
    return merged


def write_bus_alias(pro_path: Path, name: str, members: List[str]) -> bool:
    """Create or replace one alias. Returns True when it replaced an existing one."""
    data = _read_project(pro_path)
    schematic = data.get("schematic")
    if not isinstance(schematic, dict):
        schematic = {}
        data["schematic"] = schematic
    aliases = schematic.get("bus_aliases")
    if not isinstance(aliases, dict):
        aliases = {}
    replaced = name in aliases
    aliases[name] = list(members)
    schematic["bus_aliases"] = aliases
    _write_json_atomic(str(pro_path), data)
    return replaced


def remove_bus_alias(pro_path: Path, name: str) -> bool:
    """Delete one alias. Returns False when the project file has no such alias."""
    data = _read_project(pro_path)
    schematic = data.get("schematic")
    aliases = schematic.get("bus_aliases") if isinstance(schematic, dict) else None
    if not isinstance(aliases, dict) or name not in aliases:
        return False
    del aliases[name]
    _write_json_atomic(str(pro_path), data)
    return True
