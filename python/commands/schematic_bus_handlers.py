"""Bus command handlers for KiCADInterface.

Mixed into KiCADInterface like SchematicHandlersMixin. Covers bus segments,
bus entries, bus aliases, the per-sheet bus report and the pin-to-bus fan-out.
The bus rules these follow are described in docs/SCHEMATIC_BUSES.md.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from commands.bus_connectivity import (
    bus_definitions,
    describe_sheet_buses,
    try_parse_bus_label,
    on_segment,
    parse_sheet_geometry,
    segment_networks,
    to_iu,
)
from commands.bus_fanout import PinRequest, mm, plan_fanout
from commands.bus_names import parse_bus_label
from commands.pin_locator import PinLocator
from commands.schematic import SchematicLoadError
from commands.wire_manager import _SYM_WIRE, WireManager
from utils.bus_aliases import (
    design_bus_aliases,
    find_project_file,
    project_bus_aliases,
    remove_bus_alias,
    write_bus_alias,
)
from utils.interactive_schematic import reload_kicad_schematic
from utils.sexpr_format import dumps as kicad_dumps

logger = logging.getLogger("kicad_interface")

# Bus entry direction -> (dx, dy) from the bus end to the wire end, in mm.
# Schematic y grows downward, so "down" is +y.
ENTRY_DIRECTIONS: Dict[str, Tuple[float, float]] = {
    "up_right": (2.54, -2.54),
    "down_right": (2.54, 2.54),
    "up_left": (-2.54, -2.54),
    "down_left": (-2.54, 2.54),
}

_ALIAS_NAME_RE = re.compile(r"^[^\s{}\[\]]+$")
_ALIAS_MEMBER_RE = re.compile(r"^[^\s{},]+$")


def _point(value: Any, what: str) -> List[float]:
    """Accept {x, y} or [x, y]; raise ValueError otherwise."""
    if isinstance(value, dict) and "x" in value and "y" in value:
        return [float(value["x"]), float(value["y"])]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [float(value[0]), float(value[1])]
    raise ValueError(f"{what} must be {{x, y}} or [x, y]")


def _load(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return sexpdata.loads(f.read())


def _sheet_aliases(schematic_path: Path, sexp: list) -> Tuple[Dict[str, List[str]], List[str]]:
    """The aliases a sheet can use, and a warning when the project file cannot be read."""
    try:
        return design_bus_aliases(find_project_file(schematic_path), [sexp]), []
    except ValueError as exc:
        return design_bus_aliases(None, [sexp]), [f"bus aliases not read: {exc}"]


def _bus_segments_at(geo: Any, point: Tuple[int, int]) -> Optional[List[Tuple[Any, Any]]]:
    """The segments of the bus network through *point*, or None."""
    for network in segment_networks(geo.buses, geo.junctions):
        segments = [geo.buses[i] for i in network]
        if any(on_segment(point, seg) for seg in segments):
            return segments
    return None


def _bus_labels_on(geo: Any, segments: List[Any], aliases: Dict[str, List[str]]) -> List[str]:
    """Names of the bus labels, or else bus sheet pins, that give the bus its members."""
    return [point.name for point, _ in bus_definitions(geo, segments, aliases)]


def _check_alias(name: str, members: List[str]) -> Optional[str]:
    if not _ALIAS_NAME_RE.match(name):
        return "Alias name must be one word with no braces or brackets"
    if not members:
        return "An alias needs at least one member"
    bad = [m for m in members if not isinstance(m, str) or not _ALIAS_MEMBER_RE.match(m)]
    if bad:
        return (
            f"Alias members must be single names or vectors such as D[0..7], with no "
            f"spaces, braces or commas: {bad}"
        )
    return None


def _clashes_with_existing(geo: Any, legs: List[Any]) -> List[str]:
    """Refuse legs that repeat an existing entry or touch an existing wire.

    A repeated call would otherwise draw a second entry, wire and label on
    top of the first, and a new wire end on an existing wire joins that
    wire's net to the bus member.
    """
    errors: List[str] = []
    existing_entries = {frozenset((e.bus_end, e.wire_end)) for e in geo.entries}
    for leg in legs:
        name = f"{leg.request.reference}/{leg.request.pin}"
        wire_end = leg.label_at
        if frozenset((leg.entry_at, wire_end)) in existing_entries:
            errors.append(
                f"{name} already has a bus entry at ({mm(leg.entry_at[0]):g}, {mm(leg.entry_at[1]):g})"
            )
            continue
        if any(on_segment(wire_end, seg) for seg in geo.wires):
            errors.append(
                f"{name}: the new wire would start on an existing wire at "
                f"({mm(wire_end[0]):g}, {mm(wire_end[1]):g})"
            )
        elif leg.wire is not None and any(on_segment(leg.request.at, seg) for seg in geo.wires):
            errors.append(f"{name} already has a wire; delete it first or connect it by label")
    return errors


class SchematicBusHandlersMixin:
    """Bus handlers mixed into KiCADInterface."""

    def _handle_add_schematic_bus(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Draw a bus through two or more waypoints."""
        try:
            schematic_path = params.get("schematicPath")
            waypoints = params.get("waypoints") or []
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            if len(waypoints) < 2:
                return {"success": False, "message": "At least 2 waypoints are required"}
            points = [_point(p, "Each waypoint") for p in waypoints]
            path = Path(schematic_path)
            aliases, warnings = _sheet_aliases(path, _load(path))
            count = WireManager.add_bus(path, points)
            reload_kicad_schematic()

            geo = parse_sheet_geometry(_load(path))
            segments = _bus_segments_at(geo, to_iu(*points[0])) or []
            if not _bus_labels_on(geo, segments, aliases):
                warnings.append(
                    "This bus has no bus label or bus sheet pin yet. Add a label with "
                    "add_schematic_net_label (for example 'D[0..7]' or 'I2C{SDA SCL}') at a "
                    "point on the bus, or end it on a bus sheet pin, or it carries no members."
                )
            result: Dict[str, Any] = {
                "success": True,
                "message": f"Added bus with {count} segment(s)",
                "segments": count,
            }
            if warnings:
                result["warnings"] = warnings
            return result
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error adding bus")
            return {"success": False, "message": str(e)}

    def _handle_delete_schematic_bus(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete one bus segment by its end points."""
        try:
            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            start = _point(params.get("start"), "start")
            end = _point(params.get("end"), "end")
            if not WireManager.delete_bus(Path(schematic_path), start, end):
                return {"success": False, "message": "No bus segment with those end points"}
            reload_kicad_schematic()
            return {"success": True, "message": f"Deleted bus segment {start} -> {end}"}
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error deleting bus")
            return {"success": False, "message": str(e)}

    def _handle_add_schematic_bus_entry(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a bus entry whose bus end sits on an existing bus."""
        try:
            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            position = _point(params.get("position"), "position")
            direction = params.get("direction", "down_right")
            if direction not in ENTRY_DIRECTIONS:
                return {
                    "success": False,
                    "message": f"direction must be one of {sorted(ENTRY_DIRECTIONS)}",
                }
            path = Path(schematic_path)
            geo = parse_sheet_geometry(_load(path))
            at = to_iu(*position)
            if not any(on_segment(at, seg) for seg in geo.buses):
                return {
                    "success": False,
                    "message": f"No bus passes through {position}; a bus entry must start on a bus",
                }
            dx, dy = ENTRY_DIRECTIONS[direction]
            WireManager.add_bus_entries(path, [(position, [dx, dy])])
            reload_kicad_schematic()
            wire_end = [round(position[0] + dx, 4), round(position[1] + dy, 4)]
            return {
                "success": True,
                "message": f"Added bus entry at {position}; connect a labelled wire at {wire_end}",
                "position": {"x": position[0], "y": position[1]},
                "wireEnd": {"x": wire_end[0], "y": wire_end[1]},
            }
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error adding bus entry")
            return {"success": False, "message": str(e)}

    def _handle_delete_schematic_bus_entry(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete the bus entry whose bus end is at a position."""
        try:
            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            position = _point(params.get("position"), "position")
            if not WireManager.delete_bus_entry(Path(schematic_path), position):
                return {"success": False, "message": f"No bus entry within 0.5 mm of {position}"}
            reload_kicad_schematic()
            return {"success": True, "message": f"Deleted bus entry at {position}"}
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error deleting bus entry")
            return {"success": False, "message": str(e)}

    def _handle_list_schematic_buses(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Report the buses, bus entries and bus problems on one sheet."""
        try:
            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            path = Path(schematic_path)
            sexp = _load(path)
            aliases, warnings = _sheet_aliases(path, sexp)
            report = describe_sheet_buses(sexp, aliases)
            result = {"success": True, **report, "aliases": aliases}
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            logger.exception("Error listing buses")
            return {"success": False, "message": str(e)}

    def _alias_project(self, params: Dict[str, Any]) -> Path:
        project = params.get("projectPath")
        if project:
            path = Path(project)
            if path.suffix != ".kicad_pro":
                raise ValueError("projectPath must name a .kicad_pro file")
            if not path.is_file():
                raise ValueError(f"Project file not found: {path}")
            return path
        schematic_path = params.get("schematicPath")
        if not schematic_path:
            raise ValueError("schematicPath or projectPath is required")
        found = find_project_file(Path(schematic_path))
        if found is None:
            raise ValueError(
                f"No .kicad_pro found next to or above {schematic_path}; pass projectPath"
            )
        return found

    def _handle_list_bus_aliases(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List the bus aliases in the project file."""
        try:
            project = self._alias_project(params)
            aliases = project_bus_aliases(project)
            return {"success": True, "projectPath": str(project), "aliases": aliases}
        except ValueError as e:
            return {"success": False, "message": str(e)}

    def _handle_set_bus_alias(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create or replace a bus alias in the project file."""
        try:
            name = str(params.get("name") or "")
            members = params.get("members") or []
            problem = _check_alias(name, members)
            if problem:
                return {"success": False, "message": problem}
            project = self._alias_project(params)
            proposed = {**project_bus_aliases(project), name: list(members)}
            try:
                parse_bus_label(f"{{{name}}}", proposed)
            except ValueError as e:
                return {"success": False, "message": f"Alias '{name}' rejected: {e}"}
            replaced = write_bus_alias(project, name, list(members))
            verb = "Replaced" if replaced else "Added"
            return {
                "success": True,
                "message": (
                    f"{verb} bus alias '{name}' in {project.name}. Use it in a bus label as "
                    f"'{{{name}}}' or 'PREFIX{{{name}}}'. KiCad reads the project file when it "
                    "opens the project, so reopen it if it is open."
                ),
                "projectPath": str(project),
                "replaced": replaced,
            }
        except ValueError as e:
            return {"success": False, "message": str(e)}

    def _handle_delete_bus_alias(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delete a bus alias from the project file."""
        try:
            name = str(params.get("name") or "")
            if not name:
                return {"success": False, "message": "name is required"}
            project = self._alias_project(params)
            if not remove_bus_alias(project, name):
                return {"success": False, "message": f"No bus alias '{name}' in {project.name}"}
            return {"success": True, "message": f"Deleted bus alias '{name}' from {project.name}"}
        except ValueError as e:
            return {"success": False, "message": str(e)}

    def _handle_connect_pins_to_bus(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Connect symbol pins to a bus with one entry, wire and member label each."""
        try:
            schematic_path = params.get("schematicPath")
            connections = params.get("connections") or []
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            if not connections:
                return {"success": False, "message": "connections must list at least one pin"}
            bus_point = to_iu(*_point(params.get("busPoint"), "busPoint"))
            path = Path(schematic_path)

            sexp = _load(path)
            aliases, alias_warnings = _sheet_aliases(path, sexp)
            geo = parse_sheet_geometry(sexp)
            segments = _bus_segments_at(geo, bus_point)
            if segments is None:
                return {"success": False, "message": "busPoint is not on a bus"}

            bus_names = _bus_labels_on(geo, segments, aliases)
            members: Optional[List[str]] = None
            if bus_names:
                members = []
                for bus_name in bus_names:
                    bus = try_parse_bus_label(bus_name, aliases)
                    if bus is not None:
                        members.extend(m for m in bus.members if m not in members)

            locator = PinLocator()
            requests: List[PinRequest] = []
            errors: List[str] = []
            for conn in connections:
                ref = str(conn.get("reference") or "")
                pin = str(conn.get("pin") or "")
                net = str(conn.get("net") or "")
                if not (ref and pin and net):
                    errors.append(f"connection {conn} needs reference, pin and net")
                    continue
                if try_parse_bus_label(net, aliases) is not None:
                    errors.append(f"{ref}/{pin}: net '{net}' is a bus, not a single member")
                    continue
                if members is not None and net not in members:
                    errors.append(
                        f"{ref}/{pin}: '{net}' is not a member of bus "
                        f"{' / '.join(bus_names)} (members: {', '.join(members)})"
                    )
                    continue
                location = locator.get_pin_location(path, ref, pin)
                if location is None:
                    errors.append(f"pin {ref}/{pin} not found")
                    continue
                angle = locator.get_pin_angle(path, ref, pin)
                requests.append(PinRequest(ref, pin, net, to_iu(*location), angle))
            if errors:
                return {"success": False, "message": "; ".join(errors), "errors": errors}

            legs, plan_errors = plan_fanout(segments, requests)
            plan_errors += _clashes_with_existing(geo, legs)
            if plan_errors:
                return {"success": False, "message": "; ".join(plan_errors), "errors": plan_errors}

            self._write_fanout(path, legs)
            reload_kicad_schematic()

            result: Dict[str, Any] = {
                "success": True,
                "message": f"Connected {len(legs)} pin(s) to the bus",
                "legs": [
                    {
                        "reference": leg.request.reference,
                        "pin": leg.request.pin,
                        "net": leg.request.net,
                        "entry": {"x": mm(leg.entry_at[0]), "y": mm(leg.entry_at[1])},
                        "label": {"x": mm(leg.label_at[0]), "y": mm(leg.label_at[1])},
                    }
                    for leg in legs
                ],
            }
            warnings = alias_warnings + [leg.warning for leg in legs if leg.warning]
            if members is None:
                warnings.append(
                    "The bus has no bus label or bus sheet pin, so the member names were not "
                    "checked and the "
                    "bus carries no members until you label it"
                )
            if warnings:
                result["warnings"] = warnings
            return result
        except SchematicLoadError as e:
            return e.to_response()
        except ValueError as e:
            return {"success": False, "message": str(e)}
        except Exception as e:
            logger.exception("Error connecting pins to bus")
            return {"success": False, "message": str(e)}

    @staticmethod
    def _write_fanout(path: Path, legs: List[Any]) -> None:
        sch_data = _load(path)
        items: List[list] = []
        for leg in legs:
            entry_at = [mm(leg.entry_at[0]), mm(leg.entry_at[1])]
            items.append(
                WireManager._make_bus_entry_sexp(
                    entry_at, [mm(leg.entry_size[0]), mm(leg.entry_size[1])]
                )
            )
            if leg.wire is not None:
                (ax, ay), (bx, by) = leg.wire
                for end in ([mm(ax), mm(ay)], [mm(bx), mm(by)]):
                    WireManager._break_wires_at_point(sch_data, end, _SYM_WIRE)
                items.append(WireManager._make_wire_sexp([mm(ax), mm(ay)], [mm(bx), mm(by)]))
            items.append(
                WireManager._make_label_sexp(
                    leg.request.net,
                    [mm(leg.label_at[0]), mm(leg.label_at[1])],
                    "label",
                    leg.label_angle,
                )
            )
        at = WireManager._insertion_index(sch_data)
        sch_data[at:at] = items
        WireManager.sync_junctions(sch_data)
        text = kicad_dumps(sch_data)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"Wrote {len(legs)} bus fan-out leg(s) to {path.name}")
