"""
Design connectivity from KiCad's own netlist.

Which nets join across sheets depends on sheet pins, bus pins, global labels,
power ports and the sheet each local label belongs to. A sheet used more than
once also gives every symbol on it one reference per use. Getting all of that
right by reading label names fails: two sub-sheets that each have a
hierarchical label EN are two nets in KiCad, not one. So the design's nets
come from ``kicad-cli sch export netlist``, which runs KiCad's connectivity
code.

Labels still matter to the caller, who asks about a net by a label it can see
(``EN``, ``D3``) rather than KiCad's full name (``/LED_PWM_UV310``). A label is
mapped to KiCad nets through the pins it reaches on its own sheet, and through
KiCad net names that end in the label's text.
"""

import logging
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

import sexpdata
from commands.bus_connectivity import (
    IU,
    SheetSymbol,
    on_segment,
    parse_sheet_geometry,
    segment_networks,
)
from commands.pin_locator import PinLocator
from commands.wire_connectivity import _build_sheet_pin_index, pins_by_sheet
from utils.kicad_cli import resolve_kicad_cli
from utils.sheet_tree import sheet_tree

logger = logging.getLogger("kicad_interface")

Pin = Tuple[str, str]

# KiCad gives a pin wired to nothing its own net with this prefix.
UNCONNECTED_PREFIX = "unconnected-("


@dataclass(frozen=True)
class DesignNetlist:
    """KiCad's nets for one design: net name to its (reference, pin) nodes."""

    nets: Dict[str, Tuple[Pin, ...]]

    def net_of(self) -> Dict[Pin, str]:
        return {pin: name for name, pins in self.nets.items() for pin in pins}

    def named_nets(self) -> Dict[str, Tuple[Pin, ...]]:
        """Every net except KiCad's single-pin unconnected-(...) nets."""
        return {n: p for n, p in self.nets.items() if not n.startswith(UNCONNECTED_PREFIX)}


def export_design_netlist(schematic_path: str, timeout: int = 120) -> Union[DesignNetlist, str]:
    """Run kicad-cli on *schematic_path* and return its nets, or a reason it could not."""
    kicad_cli = resolve_kicad_cli()
    if not kicad_cli:
        return "kicad-cli was not found"
    if not Path(schematic_path).is_file():
        return f"{schematic_path} is not a file"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "netlist.xml"
        cmd = [kicad_cli, "sch", "export", "netlist", "--format", "kicadxml"]
        cmd += ["--output", str(out), str(schematic_path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"kicad-cli failed: {exc}"
        if result.returncode != 0 or not out.is_file():
            return f"kicad-cli failed (exit {result.returncode}): {result.stderr.strip()}"
        try:
            root = ET.parse(out).getroot()
        except ET.ParseError as exc:
            return f"kicad-cli netlist could not be parsed: {exc}"
    nets: Dict[str, Tuple[Pin, ...]] = {}
    for net in root.findall("./nets/net"):
        name = net.get("name", "")
        nets[name] = tuple((n.get("ref", ""), n.get("pin", "")) for n in net.findall("node"))
    return DesignNetlist(nets)


def nets_named_like(netlist: DesignNetlist, name: str) -> List[str]:
    """KiCad nets whose name is *name*, or ends in /*name* (a sheet-local net)."""
    if name in netlist.nets:
        return [name]
    suffix = "/" + name
    return sorted(n for n in netlist.nets if n.endswith(suffix))


def label_nets(
    schematic_path: str,
    netlist: DesignNetlist,
    label_pins: Dict[str, Iterable[Pin]],
) -> Dict[str, List[str]]:
    """Map each label name to the KiCad nets it is on.

    *label_pins* gives, for each label name, the pins it reaches on its own
    sheets. A label is on every net that holds one of those pins, and on every
    net named after it. Pins in a sheet used more than once carry the
    reference stored in the file, which KiCad gives to one of its uses, so a
    label there maps to that use's net.
    """
    net_of = netlist.net_of()
    result: Dict[str, List[str]] = {}
    for name, pins in label_pins.items():
        found: Set[str] = set(nets_named_like(netlist, name))
        found.update(net_of[pin] for pin in pins if pin in net_of)
        result[name] = sorted(found)
    return result


def _key(path: Path) -> str:
    return str(Path(path).resolve())


def pins_through_sheet_pins(schematic_path: str, names: Iterable[str]) -> Dict[str, List[Pin]]:
    """For labels that reach no pin on their own sheet, find pins by following the hierarchy.

    From each label with one of *names*, walk its wire to the hierarchical
    labels and sheet pins on it, through the matching sheet pin or
    hierarchical label on the next sheet, and on until a wire reaches symbol
    pins. Each step follows one concrete sheet symbol, so nets that only share
    a label name are never mixed. Bus pins are not followed; KiCad's net
    names cover bus members (see nets_named_like).

    Returns {name: [(reference, pin), ...]} for every name in *names*.
    """
    wanted = set(names)
    sheets = sheet_tree(Path(schematic_path))
    sexps: Dict[str, list] = {}
    for sheet in sheets:
        with open(sheet, "r", encoding="utf-8") as f:
            sexps[_key(sheet)] = sexpdata.loads(f.read())
    geos = {key: parse_sheet_geometry(sexp) for key, sexp in sexps.items()}
    networks = {key: segment_networks(geo.wires, geo.junctions) for key, geo in geos.items()}
    locator = PinLocator(memoize_pins=True)
    pin_index: Dict[str, List[Tuple[str, str, IU]]] = {}

    # child file -> [(parent file, sheet symbol)]
    parents: Dict[str, List[Tuple[str, SheetSymbol]]] = {}
    for sheet in sheets:
        for symbol in geos[_key(sheet)].sheets:
            child = _key(sheet.parent / symbol.file)
            parents.setdefault(child, []).append((_key(sheet), symbol))

    def network_points(key: str, at: IU) -> Tuple[Optional[List[int]], Set[IU]]:
        geo = geos[key]
        for network in networks[key]:
            if any(on_segment(at, geo.wires[i]) for i in network):
                return network, set()
        return None, {at}

    def on_network(key: str, network: Optional[List[int]], points: Set[IU], at: IU) -> bool:
        if at in points:
            return True
        return network is not None and any(on_segment(at, geos[key].wires[i]) for i in network)

    def walk(start_key: str, start_at: IU) -> List[Pin]:
        queue = [(start_key, start_at)]
        seen: Set[Tuple[str, IU]] = set()
        found: List[Pin] = []
        while queue:
            key, at = queue.pop()
            if (key, at) in seen or key not in geos:
                continue
            seen.add((key, at))
            network, points = network_points(key, at)
            if key not in pin_index:
                pin_index[key] = _build_sheet_pin_index(sexps[key], key, locator)
            found.extend(
                (ref, pin)
                for ref, pin, pin_at in pin_index[key]
                if on_network(key, network, points, pin_at)
            )
            if found:
                return found
            geo = geos[key]
            for label in geo.labels:
                if label.kind != "hierarchical_label" or not on_network(
                    key, network, points, label.at
                ):
                    continue
                for parent_key, symbol in parents.get(key, []):
                    queue.extend(
                        (parent_key, pin.at) for pin in symbol.pins if pin.name == label.name
                    )
            for symbol in geo.sheets:
                child_key = _key(Path(key).parent / symbol.file)
                if child_key not in geos:
                    continue
                for pin in symbol.pins:
                    if on_network(key, network, points, pin.at):
                        queue.extend(
                            (child_key, label.at)
                            for label in geos[child_key].labels
                            if label.kind == "hierarchical_label" and label.name == pin.name
                        )
        return found

    result: Dict[str, List[Pin]] = {name: [] for name in wanted}
    for key, geo in geos.items():
        for label in geo.labels:
            if label.name in wanted:
                result[label.name].extend(
                    p for p in walk(key, label.at) if p not in result[label.name]
                )
    return result


def label_pins(
    schematic: object, schematic_path: str, names: List[str], locator: Optional[PinLocator] = None
) -> Dict[str, List[Pin]]:
    """The pins each label name reaches: on its own sheet, or else through sheet pins."""
    by_sheet = pins_by_sheet(schematic, schematic_path, names, locator)
    found: Dict[str, List[Pin]] = {
        name: [
            (pin["component"], pin["pin"])
            for sheet in by_sheet.values()
            for pin in sheet.get(name, [])
        ]
        for name in names
    }
    pinless = [name for name, pins in found.items() if not pins]
    if pinless:
        found.update(pins_through_sheet_pins(schematic_path, pinless))
    return found
