"""
Junctions in a parsed schematic.

The functions here take the list that ``sexpdata.loads`` returns for a
.kicad_sch file and return a new list; the handlers read and write the file.
A junction's connections are counted the way WireManager.sync_junctions
counts them.
"""

import math
from typing import Any, Dict, List, Optional, Tuple, Union

from sexpdata import Symbol

from commands.wire_manager import PointConnections, WireManager, _iu

_SYM_AT = Symbol("at")
_SYM_UUID = Symbol("uuid")
_SYM_JUNCTION = Symbol("junction")

# A junction needs this many connections at its point to join anything.
JUNCTION_MIN_CONNECTIONS = 3


def _child(item: list, tag: Symbol) -> Optional[list]:
    return next((p for p in item if isinstance(p, list) and p and p[0] == tag), None)


def list_junctions(sch_data: list) -> List[Dict[str, Any]]:
    """Every junction with its position, uuid and the connections at its point."""
    connections = WireManager.point_connections(sch_data)
    found = []
    for item in sch_data:
        if not (isinstance(item, list) and item and item[0] == _SYM_JUNCTION):
            continue
        at = _child(item, _SYM_AT)
        if at is None:
            continue
        point = (float(at[1]), float(at[2]))
        uid = _child(item, _SYM_UUID)
        found.append(
            {
                "position": {"x": point[0], "y": point[1]},
                "uuid": str(uid[1]) if uid else "",
                **_connection_fields(connections(point)),
            }
        )
    return found


def _connection_fields(counts: PointConnections) -> Dict[str, Any]:
    return {
        "wireConnections": counts.wires,
        "busConnections": counts.buses,
        "joinsAnything": max(counts.wires, counts.buses) >= JUNCTION_MIN_CONNECTIONS,
    }


def add_junction(
    sch_data: list, point: Tuple[float, float]
) -> Union[Tuple[list, Dict[str, Any]], str]:
    """Add a junction at *point*: (new data, report), or a reason it was not added.

    A junction joins the wires (or buses) that pass through or end at its
    point. Where fewer than three connections meet it joins nothing, so it
    is refused; sync_junctions would also remove it on the next wire edit.
    """
    if _iu(point) in WireManager._get_existing_junctions(sch_data):
        return f"A junction already sits at ({point[0]}, {point[1]})"
    counts = WireManager.point_connections(sch_data)(point)
    if max(counts.wires, counts.buses) < JUNCTION_MIN_CONNECTIONS:
        return (
            f"({point[0]}, {point[1]}) has {counts.wires} wire and {counts.buses} bus "
            f"connection(s). A junction joins something only where {JUNCTION_MIN_CONNECTIONS} "
            "or more meet: a T, a crossing, or a pin on the middle of a wire. Each segment "
            "end and pin counts one; a segment passing through counts two."
        )
    junction = WireManager._make_junction_sexp(point[0], point[1])
    at = WireManager._insertion_index(sch_data)
    data = sch_data[:at] + [junction] + sch_data[at:]
    return data, {
        "position": {"x": point[0], "y": point[1]},
        "uuid": str(_child(junction, _SYM_UUID)[1]),
        **_connection_fields(counts),
    }


def delete_junction(
    sch_data: list, point: Tuple[float, float], tolerance: float = 0.5
) -> Union[Tuple[list, Dict[str, Any]], str]:
    """Remove the junction nearest *point*: (new data, report), or a reason none was removed."""
    best: Optional[Tuple[float, int, Tuple[float, float]]] = None
    for i, item in enumerate(sch_data):
        if not (isinstance(item, list) and item and item[0] == _SYM_JUNCTION):
            continue
        at = _child(item, _SYM_AT)
        if at is None:
            continue
        where = (float(at[1]), float(at[2]))
        dist = math.hypot(where[0] - point[0], where[1] - point[1])
        if dist <= tolerance and (best is None or dist < best[0]):
            best = (dist, i, where)
    if best is None:
        return f"No junction within {tolerance} mm of ({point[0]}, {point[1]})"
    _, index, where = best
    data = sch_data[:index] + sch_data[index + 1 :]
    counts = WireManager.point_connections(data)(where)
    report: Dict[str, Any] = {
        "position": {"x": where[0], "y": where[1]},
        **_connection_fields(counts),
    }
    if max(counts.wires, counts.buses) >= JUNCTION_MIN_CONNECTIONS:
        if counts.segment_ends:
            report["warning"] = (
                "A wire or bus ends here and meets 3 or more connections. Without the junction "
                "it is not joined, but KiCad and this server's wire tools put a junction back "
                "at such a point on the next wire edit. To break the connection, move or "
                "delete the wire instead."
            )
        else:
            report["note"] = "The segments that cross here are no longer joined."
    return data, report
