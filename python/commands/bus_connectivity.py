"""
Bus connectivity for KiCad schematics, one sheet at a time.

``describe_sheet_buses`` reads one sheet and reports its bus networks: the
segments, the bus labels and the members they define, the bus entries and
the net on the wire each entry feeds, and the sheet pins the bus reaches.

The geometry follows KiCad: segments of one kind join where they share an
end point, and a segment that ends on the middle of another only joins it
through a junction. A label, sheet pin or bus entry joins a segment anywhere
along it. A wire never joins a bus directly; only a bus entry links them, and
the wire's own label decides which member it carries.

How nets join across sheets is left to KiCad: see commands/design_netlist.py.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import sexpdata
from commands.bus_names import BusAliases, BusLabel, parse_bus_label
from commands.wire_dragger import WireDragger
from commands.wire_manager import WireManager
from sexpdata import Symbol
from utils.bus_aliases import design_bus_aliases, find_project_file
from utils.sheet_tree import sheet_tree

logger = logging.getLogger("kicad_interface")

_IU_PER_MM = 10000

IU = Tuple[int, int]
Segment = Tuple[IU, IU]

LABEL_KINDS = ("global_label", "label", "hierarchical_label")
_LABEL_TAGS = {Symbol(k) for k in LABEL_KINDS}
# KiCad names a net after its strongest driver: a global label beats a power
# port, which beats a local label, which beats a hierarchical label.
_DRIVER_RANK = {"global_label": 0, "power": 1, "label": 2, "hierarchical_label": 3, "sheet_pin": 4}


def to_iu(x_mm: float, y_mm: float) -> IU:
    return (round(x_mm * _IU_PER_MM), round(y_mm * _IU_PER_MM))


def to_mm(pt: IU) -> Dict[str, float]:
    return {"x": pt[0] / _IU_PER_MM, "y": pt[1] / _IU_PER_MM}


def _xy(pt: IU) -> str:
    return f"({pt[0] / _IU_PER_MM:g}, {pt[1] / _IU_PER_MM:g})"


@dataclass(frozen=True)
class NamedPoint:
    """A label or power port: a name anchored at one point."""

    name: str
    kind: str
    at: IU


@dataclass(frozen=True)
class SheetPin:
    name: str
    at: IU


@dataclass(frozen=True)
class SheetSymbol:
    name: str
    file: str
    pins: Tuple[SheetPin, ...]


@dataclass(frozen=True)
class BusEntry:
    """A bus entry's two ends, after parse_sheet_geometry has found which one touches a bus."""

    bus_end: IU
    wire_end: IU


@dataclass
class SheetGeometry:
    """The connection items of one sheet, in KiCad internal units."""

    wires: List[Segment] = field(default_factory=list)
    buses: List[Segment] = field(default_factory=list)
    junctions: Set[IU] = field(default_factory=set)
    labels: List[NamedPoint] = field(default_factory=list)
    power_ports: List[NamedPoint] = field(default_factory=list)
    entries: List[BusEntry] = field(default_factory=list)
    sheets: List[SheetSymbol] = field(default_factory=list)


def _at(item: list) -> Optional[IU]:
    for part in item:
        if isinstance(part, list) and len(part) >= 3 and part[0] == Symbol("at"):
            return to_iu(float(part[1]), float(part[2]))
    return None


def _property(item: list, name: str) -> Optional[str]:
    for part in item:
        if (
            isinstance(part, list)
            and len(part) >= 3
            and part[0] == Symbol("property")
            and str(part[1]) == name
        ):
            return str(part[2])
    return None


def _power_ports(sexp: list) -> List[NamedPoint]:
    """Power symbols (#PWR references) as named points at their pin."""
    lib_defs: Dict[str, list] = {}
    for item in sexp:
        if isinstance(item, list) and item and item[0] == Symbol("lib_symbols"):
            for sym_def in item[1:]:
                if isinstance(sym_def, list) and len(sym_def) > 1:
                    lib_defs[str(sym_def[1])] = sym_def
    ports: List[NamedPoint] = []
    for item in sexp:
        if not (isinstance(item, list) and item and item[0] == Symbol("symbol")):
            continue
        reference = _property(item, "Reference")
        value = _property(item, "Value")
        if not reference or not reference.startswith("#PWR") or not value:
            continue
        lib_id = next(
            (str(p[1]) for p in item if isinstance(p, list) and p and p[0] == Symbol("lib_id")),
            None,
        )
        at_part = next(
            (p for p in item if isinstance(p, list) and len(p) >= 3 and p[0] == Symbol("at")),
            None,
        )
        if lib_id not in lib_defs or at_part is None:
            continue
        mirror = {
            str(p[1])
            for p in item
            if isinstance(p, list) and len(p) >= 2 and p[0] == Symbol("mirror")
        }
        rotation = float(at_part[3]) if len(at_part) > 3 else 0.0
        for lx, ly in WireManager._parse_lib_pins(lib_defs[lib_id]):
            x, y = WireDragger.pin_world_xy(
                lx, ly, float(at_part[1]), float(at_part[2]), rotation, "x" in mirror, "y" in mirror
            )
            ports.append(NamedPoint(value, "power", to_iu(x, y)))
    return ports


def parse_sheet_geometry(sexp: list) -> SheetGeometry:
    geo = SheetGeometry()
    for item in sexp:
        if not (isinstance(item, list) and item):
            continue
        tag = item[0]
        if tag in (Symbol("wire"), Symbol("bus")):
            parsed = WireManager._parse_wire(item, tag)
            if parsed is not None:
                segment = (to_iu(*parsed[0]), to_iu(*parsed[1]))
                (geo.wires if tag == Symbol("wire") else geo.buses).append(segment)
        elif tag == Symbol("junction"):
            at = _at(item)
            if at is not None:
                geo.junctions.add(at)
        elif tag in _LABEL_TAGS and len(item) > 1:
            at = _at(item)
            if at is not None:
                geo.labels.append(NamedPoint(str(item[1]), str(tag), at))
        elif tag == Symbol("bus_entry"):
            parsed_entry = WireManager._parse_bus_entry(item)
            if parsed_entry is not None:
                (x, y), (dx, dy) = parsed_entry
                geo.entries.append(BusEntry(to_iu(x, y), to_iu(x + dx, y + dy)))
        elif tag == Symbol("sheet"):
            pins = tuple(
                SheetPin(str(p[1]), at)
                for p in item
                if isinstance(p, list) and len(p) > 2 and p[0] == Symbol("pin")
                for at in [_at(p)]
                if at is not None
            )
            sheet_file = _property(item, "Sheetfile") or _property(item, "Sheet file") or ""
            sheet_name = _property(item, "Sheetname") or _property(item, "Sheet name") or ""
            geo.sheets.append(SheetSymbol(sheet_name, sheet_file, pins))
    geo.power_ports = _power_ports(sexp)
    # KiCad joins a bus entry to a bus at either end, and KiCad-drawn entries
    # often have (at ...) on the wire side. Name the end that touches a bus
    # the bus end; an entry touching no bus keeps the file's order.
    geo.entries = [
        (
            BusEntry(e.wire_end, e.bus_end)
            if not any(on_segment(e.bus_end, s) for s in geo.buses)
            and any(on_segment(e.wire_end, s) for s in geo.buses)
            else e
        )
        for e in geo.entries
    ]
    return geo


def on_segment(pt: IU, seg: Segment) -> bool:
    """True when *pt* lies on *seg*, end points included."""
    (px, py), ((ax, ay), (bx, by)) = pt, seg
    if (bx - ax) * (py - ay) != (by - ay) * (px - ax):
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


def segment_networks(segments: Sequence[Segment], junctions: Iterable[IU]) -> List[List[int]]:
    """Group segment indices into connected networks."""
    parent = list(range(len(segments)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    by_end: Dict[IU, List[int]] = {}
    for i, (a, b) in enumerate(segments):
        by_end.setdefault(a, []).append(i)
        by_end.setdefault(b, []).append(i)
    for members in by_end.values():
        for other in members[1:]:
            union(members[0], other)
    for junction in junctions:
        touching = [i for i, seg in enumerate(segments) if on_segment(junction, seg)]
        for other in touching[1:]:
            union(touching[0], other)

    groups: Dict[int, List[int]] = {}
    for i in range(len(segments)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _network_at(
    pt: IU, segments: Sequence[Segment], networks: List[List[int]]
) -> Optional[List[int]]:
    for network in networks:
        if any(on_segment(pt, segments[i]) for i in network):
            return network
    return None


def _points_on(
    points: Iterable[NamedPoint], segments: Sequence[Segment], network: List[int]
) -> List[NamedPoint]:
    return [p for p in points if any(on_segment(p.at, segments[i]) for i in network)]


def driver(points: Iterable[NamedPoint]) -> Optional[NamedPoint]:
    """The point whose name KiCad gives the net, or None when there is none."""
    ranked = sorted(points, key=lambda p: (_DRIVER_RANK.get(p.kind, 9), p.name))
    return ranked[0] if ranked else None


def wire_net_at(
    geo: SheetGeometry, pt: IU, networks: Optional[List[List[int]]] = None
) -> List[NamedPoint]:
    """Labels and power ports on the wire network at *pt*, strongest driver first.

    A label or power port sitting exactly on *pt* counts even with no wire.
    """
    if networks is None:
        networks = segment_networks(geo.wires, geo.junctions)
    named = geo.labels + geo.power_ports
    network = _network_at(pt, geo.wires, networks)
    found = _points_on(named, geo.wires, network) if network is not None else []
    found += [p for p in named if p.at == pt and p not in found]
    return sorted(found, key=lambda p: (_DRIVER_RANK.get(p.kind, 9), p.name))


def bus_definitions(
    geo: SheetGeometry, segments: Sequence[Segment], aliases: BusAliases
) -> List[Tuple[NamedPoint, BusLabel]]:
    """What gives a bus network its members: its bus labels, or else its bus sheet pins.

    A bus drawn between two sheet pins needs no label of its own; KiCad takes
    its members from the pin names (USB{DP DN} carries USB.DP and USB.DN).
    """

    def parsed(points: Iterable[NamedPoint]) -> List[Tuple[NamedPoint, BusLabel]]:
        found = []
        for point in points:
            if any(on_segment(point.at, seg) for seg in segments):
                bus = try_parse_bus_label(point.name, aliases)
                if bus is not None:
                    found.append((point, bus))
        return found

    labels = parsed(geo.labels)
    if labels:
        return labels
    pins = [NamedPoint(pin.name, "sheet_pin", pin.at) for sheet in geo.sheets for pin in sheet.pins]
    return parsed(pins)


def bus_label_at(geo: SheetGeometry, pt: IU, aliases: BusAliases) -> Optional[NamedPoint]:
    """The bus label or bus sheet pin that names the bus network at *pt*, or None."""
    networks = segment_networks(geo.buses, geo.junctions)
    network = _network_at(pt, geo.buses, networks)
    if network is None:
        return None
    segments = [geo.buses[i] for i in network]
    return driver(point for point, _ in bus_definitions(geo, segments, aliases))


# --------------------------------------------------------------------------- #
# One sheet: bus report
# --------------------------------------------------------------------------- #


def _bus_dict(label: NamedPoint, bus: BusLabel) -> Dict:
    return {
        "text": label.name,
        "labelType": label.kind,
        "position": to_mm(label.at),
        "kind": bus.kind,
        "members": list(bus.members),
    }


def describe_sheet_buses(sexp: list, aliases: BusAliases) -> Dict:
    """Report every bus network, bus entry and bus label problem on one sheet."""
    geo = parse_sheet_geometry(sexp)
    wire_networks = segment_networks(geo.wires, geo.junctions)
    bus_networks = segment_networks(geo.buses, geo.junctions)
    problems: List[str] = []

    bus_labels: List[Tuple[NamedPoint, BusLabel]] = []
    for label in geo.labels:
        try:
            parsed = parse_bus_label(label.name, aliases)
        except ValueError as exc:
            problems.append(f"label '{label.name}' at {_xy(label.at)}: {exc}")
            continue
        if parsed is not None:
            bus_labels.append((label, parsed))

    placed_entries: Set[int] = set()
    networks_out: List[Dict] = []
    for net_index, network in enumerate(bus_networks):
        segs = [geo.buses[i] for i in network]
        labels_here = bus_definitions(geo, segs, aliases)
        plain_here = [
            p
            for p in _points_on(geo.labels, geo.buses, network)
            if all(p is not label for label, _ in bus_labels)
        ]
        for p in plain_here:
            problems.append(
                f"label '{p.name}' at {_xy(p.at)} sits on a bus but is not a bus label; "
                "KiCad reports a bus-to-net conflict"
            )
        drive = driver([label for label, _ in labels_here])
        members: List[str] = []
        for label, bus in labels_here:
            for m in bus.members:
                if m not in members:
                    members.append(m)
        if not labels_here:
            problems.append(
                f"bus network {net_index} has no bus label or bus sheet pin, "
                "so it carries no members"
            )
        elif len({bus.members for _, bus in labels_here}) > 1:
            problems.append(
                f"bus network {net_index} has bus labels or sheet pins with different members ("
                + ", ".join(sorted({label.name for label, _ in labels_here}))
                + f"); KiCad names it '{drive.name if drive else ''}'"
            )

        entries_out = []
        for e_index, entry in enumerate(geo.entries):
            if not any(on_segment(entry.bus_end, s) for s in segs):
                continue
            placed_entries.add(e_index)
            names = wire_net_at(geo, entry.wire_end, wire_networks)
            net = names[0].name if names else None
            if net is None:
                problems.append(
                    f"bus entry at {_xy(entry.bus_end)} feeds a wire with no label; "
                    "the wire is not connected to any bus member"
                )
            elif labels_here and net not in members:
                problems.append(
                    f"bus entry at {_xy(entry.bus_end)} feeds net '{net}', which is not a "
                    f"member of bus '{drive.name if drive else ''}'"
                )
            entries_out.append(
                {
                    "position": to_mm(entry.bus_end),
                    "wireEnd": to_mm(entry.wire_end),
                    "net": net,
                    "isMember": net in members if net is not None else False,
                }
            )

        sheet_pins = [
            {"sheet": sheet.name, "pin": pin.name, "position": to_mm(pin.at)}
            for sheet in geo.sheets
            for pin in sheet.pins
            if any(on_segment(pin.at, s) for s in segs)
        ]
        networks_out.append(
            {
                "name": drive.name if drive else None,
                "segments": [{"start": to_mm(a), "end": to_mm(b)} for a, b in segs],
                "labels": [_bus_dict(label, bus) for label, bus in labels_here],
                "members": members,
                "entries": entries_out,
                "sheetPins": sheet_pins,
            }
        )

    for e_index, entry in enumerate(geo.entries):
        if e_index not in placed_entries:
            problems.append(f"bus entry at {_xy(entry.bus_end)} does not touch a bus")
    for label, _ in bus_labels:
        if not any(on_segment(label.at, s) for s in geo.buses):
            on_wire = any(on_segment(label.at, s) for s in geo.wires)
            problems.append(
                f"bus label '{label.name}' at {_xy(label.at)} "
                + ("sits on a wire, not a bus" if on_wire else "does not touch a bus")
            )

    return {
        "buses": networks_out,
        "busLabels": [_bus_dict(label, bus) for label, bus in bus_labels],
        "problems": problems,
    }


# --------------------------------------------------------------------------- #
# Whole design: bus aliases
# --------------------------------------------------------------------------- #


def design_aliases(
    schematic_path: str, sexp_cache: Optional[Dict[str, list]] = None
) -> Tuple[Dict[str, List[str]], List[str]]:
    """The bus aliases of the design under *schematic_path*, and any read problems.

    A project file or sheet that cannot be read gives a problem, not an
    exception, so a broken .kicad_pro never stops a net query.
    """
    cache: Dict[str, list] = {} if sexp_cache is None else sexp_cache
    problems: List[str] = []
    sexps = []
    for sheet in sheet_tree(Path(schematic_path)):
        key = str(sheet)
        try:
            if key not in cache:
                with open(sheet, "r", encoding="utf-8") as f:
                    cache[key] = sexpdata.loads(f.read())
            sexps.append(cache[key])
        except (OSError, ValueError) as exc:
            problems.append(f"could not read {sheet}: {exc}")
    try:
        aliases = design_bus_aliases(find_project_file(Path(schematic_path)), sexps)
    except ValueError as exc:
        problems.append(f"bus aliases not read: {exc}")
        aliases = design_bus_aliases(None, sexps)
    return aliases, problems


def try_parse_bus_label(text: str, aliases: BusAliases) -> Optional[BusLabel]:
    """parse_bus_label, with a label KiCad would reject treated as a plain name."""
    try:
        return parse_bus_label(text, aliases)
    except ValueError:
        return None
