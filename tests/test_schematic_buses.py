"""Tests for schematic bus support.

The kicad-cli tests build small designs, ask kicad-cli for a netlist, and
check that this server's connectivity agrees with KiCad. They skip when
kicad-cli is not installed.
"""

import json
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import pytest
import sexpdata

_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

from commands.bus_connectivity import (  # noqa: E402
    describe_sheet_buses,
    segment_networks,
    to_iu,
)
from commands.bus_fanout import PinRequest, plan_fanout  # noqa: E402
from commands.bus_names import (  # noqa: E402
    GROUP,
    VECTOR,
    parse_bus_label,
)
from commands.schematic import SchematicManager  # noqa: E402
from commands.schematic_bus_handlers import SchematicBusHandlersMixin  # noqa: E402
from commands.schematic_handlers import SchematicHandlersMixin  # noqa: E402
from commands.wire_manager import WireManager  # noqa: E402
from utils.bus_aliases import (  # noqa: E402
    design_bus_aliases,
    find_project_file,
    project_bus_aliases,
    remove_bus_alias,
    write_bus_alias,
)

KICAD_CLI = shutil.which("kicad-cli")
needs_kicad_cli = pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")


class Handlers(SchematicHandlersMixin, SchematicBusHandlersMixin):
    board = None

    def _handle_get_net_connections(self, params):
        from kicad_interface import KiCADInterface

        return KiCADInterface._handle_get_net_connections(self, params)


# --------------------------------------------------------------------------- #
# Design builder
# --------------------------------------------------------------------------- #

ROOT_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
SHEET_UUID = "bbbbbbbb-0000-0000-0000-000000000002"
OTHER_SHEET_UUID = "bbbbbbbb-0000-0000-0000-000000000003"

# A two-pin symbol. Unrotated, pin 1 is 3.81 mm above the origin and pin 2
# 3.81 mm below it.
LIB_SYMBOLS = """(lib_symbols (symbol "T:R" (pin_names (offset 0)) (in_bom yes) (on_board yes)
  (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "R_0_1" (rectangle (start -1 -2.54) (end 1 2.54) (stroke (width 0) (type default)) (fill (type none))))
  (symbol "R_1_1"
    (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
  )))"""


def _uid() -> str:
    return str(uuid.uuid4())


class Sheet:
    """Accumulates schematic items as text."""

    def __init__(self, instance_path: str = "/" + ROOT_UUID):
        self.items: List[str] = []
        self.instance_path = instance_path

    def res(self, ref: str, x: float, y: float, rotation: int = 0) -> None:
        self.items.append(
            f"""(symbol (lib_id "T:R") (at {x} {y} {rotation}) (unit 1) (in_bom yes) (on_board yes) (uuid "{_uid()}")
  (property "Reference" "{ref}" (at {x + 2} {y} 0) (effects (font (size 1.27 1.27))))
  (property "Value" "R" (at {x + 4} {y} 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at {x} {y} 0) (effects (font (size 1.27 1.27)) hide))
  (pin "1" (uuid "{_uid()}")) (pin "2" (uuid "{_uid()}"))
  (instances (project "t" (path "{self.instance_path}" (reference "{ref}") (unit 1)))))"""
        )

    def segment(self, kind: str, a: Tuple[float, float], b: Tuple[float, float]) -> None:
        self.items.append(
            f"({kind} (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) "
            f'(stroke (width 0) (type default)) (uuid "{_uid()}"))'
        )

    def entry(self, at: Tuple[float, float], size=(2.54, 2.54)) -> None:
        self.items.append(
            f"(bus_entry (at {at[0]} {at[1]}) (size {size[0]} {size[1]}) "
            f'(stroke (width 0) (type default)) (uuid "{_uid()}"))'
        )

    def label(self, text: str, at: Tuple[float, float], kind: str = "label") -> None:
        shape = "" if kind == "label" else "(shape bidirectional)"
        self.items.append(
            f'({kind} "{text}" {shape} (at {at[0]} {at[1]} 0) '
            f"(effects (font (size 1.27 1.27)) (justify left bottom)) "
            f'(uuid "{_uid()}"))'
        )

    def junction(self, at: Tuple[float, float]) -> None:
        self.items.append(
            f'(junction (at {at[0]} {at[1]}) (diameter 0) (color 0 0 0 0) (uuid "{_uid()}"))'
        )

    def sheet(
        self,
        file: str,
        pins: List[Tuple[str, Tuple[float, float]]],
        sheet_uuid: str = SHEET_UUID,
        name: str = "child",
        y: float = 40.64,
    ) -> None:
        pin_text = "\n".join(
            f'(pin "{pin}" bidirectional (at {at[0]} {at[1]} 180) (uuid "{_uid()}") '
            f"(effects (font (size 1.27 1.27)) (justify left)))"
            for pin, at in pins
        )
        self.items.append(f"""(sheet (at 101.6 {y}) (size 25.4 20.32) (fields_autoplaced yes)
  (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000)) (uuid "{sheet_uuid}")
  (property "Sheetname" "{name}" (at 101.6 {y - 1} 0) (effects (font (size 1.27 1.27)) (justify left bottom)))
  (property "Sheetfile" "{file}" (at 101.6 {y + 21.32} 0) (effects (font (size 1.27 1.27)) (justify left top)))
  {pin_text}
  (instances (project "t" (path "/{ROOT_UUID}" (page "2")))))""")

    def text(self, root: bool, sheet_uuid: str) -> str:
        tail = '(sheet_instances (path "/" (page "1")))' if root else ""
        body = "\n".join(self.items)
        return (
            f'(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")\n'
            f'(uuid "{sheet_uuid}") (paper "A4")\n{LIB_SYMBOLS}\n{body}\n{tail})\n'
        )


def write_project(directory: Path, aliases: Optional[Dict[str, List[str]]] = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    pro = directory / "t.kicad_pro"
    pro.write_text(
        json.dumps(
            {
                "meta": {"filename": "t.kicad_pro", "version": 3},
                "schematic": {"bus_aliases": aliases or {}},
            }
        )
    )
    return pro


def member_drop(sheet: Sheet, ref: str, x: float, member: Optional[str], bus_y: float = 50.8):
    """A bus entry at (x, bus_y) feeding a wire down to pin 1 of a new resistor."""
    sheet.entry((x, bus_y))
    top = (x + 2.54, bus_y + 2.54)
    pin1 = (x + 2.54, bus_y + 10.16)
    sheet.res(ref, x + 2.54, bus_y + 10.16 + 3.81)
    sheet.segment("wire", top, pin1)
    if member:
        sheet.label(member, (x + 2.54, bus_y + 5.08))


def build_hier(
    directory: Path,
    parent_label: str,
    pin_name: str,
    parent_members: List[str],
    child_members: List[str],
    aliases: Optional[Dict[str, List[str]]] = None,
    parent_label_kind: str = "label",
    child_label_kind: str = "hierarchical_label",
    child_label: Optional[str] = None,
) -> Path:
    """Root sheet with a labelled bus to a bus sheet pin; child with the matching port.

    Parent members feed R1, R2, ...; child members feed R101, R102, ...
    """
    write_project(directory, aliases)
    root = Sheet()
    pin_at = (101.6, 50.8)
    root.sheet("child.kicad_sch", [(pin_name, pin_at)])
    root.segment("bus", (25.4, 50.8), pin_at)
    root.label(parent_label, (30.48, 50.8), parent_label_kind)
    for i, member in enumerate(parent_members):
        member_drop(root, f"R{i + 1}", 38.1 + 12.7 * i, member)
    child = Sheet(f"/{ROOT_UUID}/{SHEET_UUID}")
    child.segment("bus", (25.4, 50.8), (101.6, 50.8))
    child.label(child_label or pin_name, (25.4, 50.8), child_label_kind)
    for i, member in enumerate(child_members):
        member_drop(child, f"R{101 + i}", 38.1 + 12.7 * i, member)
    (directory / "t.kicad_sch").write_text(root.text(True, ROOT_UUID))
    (directory / "child.kicad_sch").write_text(child.text(False, _uid()))
    return directory / "t.kicad_sch"


def kicad_netlist(root: Path) -> Dict[str, FrozenSet[str]]:
    out = root.with_suffix(".xml")
    subprocess.run(
        [KICAD_CLI, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(root)],
        check=True,
        capture_output=True,
    )
    nets = {}
    for net in ET.parse(out).getroot().findall("./nets/net"):
        nodes = frozenset(f'{n.get("ref")}.{n.get("pin")}' for n in net.findall("node"))
        nets[net.get("name")] = nodes
    return nets


def kicad_erc(root: Path) -> List[Dict]:
    out = root.with_suffix(".erc.json")
    subprocess.run(
        [KICAD_CLI, "sch", "erc", "--format", "json", "-o", str(out), str(root)],
        check=True,
        capture_output=True,
    )
    data = json.loads(out.read_text())
    return [v for sheet in data["sheets"] for v in sheet["violations"]]


def net_pins(root: Path, name: str) -> FrozenSet[str]:
    """The pins get_net_connections reports for *name*."""
    result = Handlers()._handle_get_net_connections({"schematicPath": str(root), "netName": name})
    assert result["success"] is True, result
    return frozenset(f'{p["component"]}.{p["pin"]}' for p in result["connections"])


def joined_pin_sets(root: Path, names: List[str]) -> Set[FrozenSet[str]]:
    """The pin sets get_net_connections reports for *names*, keeping nets with 2+ pins."""
    sets = {net_pins(root, name) for name in names}
    return {s for s in sets if len(s) > 1}


def kicad_pin_sets(root: Path) -> Set[FrozenSet[str]]:
    return {nodes for nodes in kicad_netlist(root).values() if len(nodes) > 1}


# --------------------------------------------------------------------------- #
# Bus label syntax
# --------------------------------------------------------------------------- #


class TestParseBusLabel:
    def test_plain_name_is_not_a_bus(self):
        assert parse_bus_label("SDA") is None
        assert parse_bus_label("~{RESET}") is None

    def test_vector_members_ascend_either_way(self):
        up = parse_bus_label("D[0..3]")
        down = parse_bus_label("D[3..0]")
        assert up.kind == VECTOR
        assert up.members == down.members == ("D0", "D1", "D2", "D3")

    def test_vector_with_empty_prefix(self):
        assert parse_bus_label("[0..1]").members == ("0", "1")

    def test_vector_width_cap(self):
        with pytest.raises(ValueError):
            parse_bus_label("D[0..99999]")

    def test_named_group_prefixes_members(self):
        bus = parse_bus_label("I2C{SDA SCL}")
        assert bus.kind == GROUP
        assert bus.local_members == ("SDA", "SCL")
        assert bus.members == ("I2C.SDA", "I2C.SCL")

    def test_anonymous_group_and_commas(self):
        assert parse_bus_label("{SDA SCL}").members == ("SDA", "SCL")
        assert parse_bus_label("{A,B}").members == ("A", "B")
        assert parse_bus_label("{A, B}").members == ("A", "B")

    def test_group_with_vector_member(self):
        assert parse_bus_label("M{D[0..1] CLK}").members == ("M.D0", "M.D1", "M.CLK")

    def test_overbar_member_is_not_a_nested_group(self):
        assert parse_bus_label("{~{RST} CLK}").members == ("~{RST}", "CLK")

    def test_alias_expands_only_inside_braces(self):
        aliases = {"MEM": ["D[0..1]", "CLK"]}
        assert parse_bus_label("MEM", aliases) is None
        assert parse_bus_label("{MEM}", aliases).members == ("D0", "D1", "CLK")
        assert parse_bus_label("U{MEM X}", aliases).members == ("U.D0", "U.D1", "U.CLK", "U.X")

    @pytest.mark.parametrize(
        "text,members",
        [
            # Each result was checked against kicad-cli 10.0.6 ERC.
            ("~{CS}[0..1]", ("~{CS}0", "~{CS}1")),
            ("CS~{0}[0..1]", ("CS~{0}0", "CS~{0}1")),
            ("{A B}x", ("A", "B")),
            ("P{A B}x", ("P.A", "P.B")),
            ("U1.D[0..1]", ("U1.D0", "U1.D1")),
            ("D[2..2]", None),
            ("A{B{C D} E}", None),
            ("D[0..1]{X}", None),
            ("X_{A B}", None),
            ("D[0..3] ", None),
            (" D[0..3]", None),
        ],
    )
    def test_matches_kicad(self, text, members):
        bus = parse_bus_label(text)
        assert (bus.members if bus else None) == members

    def test_self_referencing_alias_is_rejected(self):
        with pytest.raises(ValueError):
            parse_bus_label("{A}", {"A": ["B"], "B": ["A"]})


# --------------------------------------------------------------------------- #
# Bus aliases in the project file
# --------------------------------------------------------------------------- #


class TestBusAliases:
    def test_write_read_remove(self, tmp_path):
        pro = write_project(tmp_path)
        assert write_bus_alias(pro, "MEM", ["D[0..7]", "CLK"]) is False
        assert write_bus_alias(pro, "MEM", ["D[0..3]"]) is True
        assert project_bus_aliases(pro) == {"MEM": ["D[0..3]"]}
        assert remove_bus_alias(pro, "MEM") is True
        assert remove_bus_alias(pro, "MEM") is False
        assert project_bus_aliases(pro) == {}

    def test_write_keeps_other_project_settings(self, tmp_path):
        pro = tmp_path / "t.kicad_pro"
        pro.write_text(json.dumps({"board": {"x": 1}, "schematic": {"legacy_lib_dir": ""}}))
        write_bus_alias(pro, "MEM", ["CLK"])
        data = json.loads(pro.read_text())
        assert data["board"] == {"x": 1}
        assert data["schematic"]["legacy_lib_dir"] == ""

    def test_find_project_file_walks_up(self, tmp_path):
        pro = write_project(tmp_path)
        sub = tmp_path / "sheets"
        sub.mkdir()
        assert find_project_file(sub / "child.kicad_sch") == pro.resolve()

    def test_legacy_schematic_alias_is_read_and_project_wins(self, tmp_path):
        pro = write_project(tmp_path, {"MEM": ["CLK"]})
        sexp = sexpdata.loads(
            '(kicad_sch (bus_alias "MEM" (members "OLD")) (bus_alias "I2C" (members "SDA" "SCL")))'
        )
        assert design_bus_aliases(pro, [sexp]) == {"MEM": ["CLK"], "I2C": ["SDA", "SCL"]}

    def test_null_schematic_section_and_file_mode(self, tmp_path):
        pro = tmp_path / "t.kicad_pro"
        pro.write_text(json.dumps({"schematic": None}))
        pro.chmod(0o644)
        write_bus_alias(pro, "MEM", ["CLK"])
        assert project_bus_aliases(pro) == {"MEM": ["CLK"]}
        assert pro.stat().st_mode & 0o777 == 0o644

    def test_unrelated_projects_are_not_used(self, tmp_path):
        (tmp_path / "a.kicad_pro").write_text("{}")
        (tmp_path / "b.kicad_pro").write_text("{}")
        assert find_project_file(tmp_path / "other.kicad_sch") is None

    def test_alias_cycle_is_rejected(self, tmp_path):
        write_project(tmp_path, {"A": ["B"]})
        result = Handlers()._handle_set_bus_alias(
            {"projectPath": str(tmp_path / "t.kicad_pro"), "name": "B", "members": ["A"]}
        )
        assert result["success"] is False
        assert "refers to itself" in result["message"]

    def test_broken_project_file_is_a_warning(self, tmp_path):
        (tmp_path / "t.kicad_pro").write_text("")
        sch = tmp_path / "t.kicad_sch"
        sch.write_text(Sheet().text(True, ROOT_UUID))
        result = Handlers()._handle_add_schematic_bus(
            {"schematicPath": str(sch), "waypoints": [[0, 0], [10, 0]]}
        )
        assert result["success"] is True
        assert "bus aliases not read" in result["warnings"][0]
        assert len(_items(sch, "bus")) == 1

    def test_handlers(self, tmp_path):
        write_project(tmp_path)
        sch = tmp_path / "t.kicad_sch"
        sch.write_text(Sheet().text(True, ROOT_UUID))
        h = Handlers()
        bad = h._handle_set_bus_alias({"schematicPath": str(sch), "name": "A B", "members": ["X"]})
        assert bad["success"] is False
        ok = h._handle_set_bus_alias(
            {"schematicPath": str(sch), "name": "MEM", "members": ["D[0..1]", "CLK"]}
        )
        assert ok["success"] is True
        listed = h._handle_list_bus_aliases({"schematicPath": str(sch)})
        assert listed["aliases"] == {"MEM": ["D[0..1]", "CLK"]}
        assert h._handle_delete_bus_alias({"schematicPath": str(sch), "name": "MEM"})["success"]


# --------------------------------------------------------------------------- #
# Bus geometry and junctions
# --------------------------------------------------------------------------- #


def _items(path: Path, tag: str) -> List[list]:
    return [
        item
        for item in sexpdata.loads(path.read_text())
        if isinstance(item, list) and item and item[0] == sexpdata.Symbol(tag)
    ]


def _junction_points(path: Path) -> Set[Tuple[float, float]]:
    return {(float(j[1][1]), float(j[1][2])) for j in _items(path, "junction")}


class TestBusGeometry:
    @pytest.fixture
    def sch(self, tmp_path) -> Path:
        write_project(tmp_path)
        path = tmp_path / "t.kicad_sch"
        path.write_text(Sheet().text(True, ROOT_UUID))
        return path

    def test_add_bus_writes_two_point_segments(self, sch):
        assert WireManager.add_bus(sch, [[10, 10], [30, 10], [30, 30]]) == 2
        assert len(_items(sch, "bus")) == 2
        assert _items(sch, "wire") == []

    def test_bus_tee_splits_and_gets_a_junction(self, sch):
        WireManager.add_bus(sch, [[10, 10], [50, 10]])
        WireManager.add_bus(sch, [[30, 10], [30, 30]])
        assert len(_items(sch, "bus")) == 3
        assert _junction_points(sch) == {(30.0, 10.0)}

    def test_wire_edit_keeps_bus_junction(self, sch):
        WireManager.add_bus(sch, [[10, 10], [50, 10]])
        WireManager.add_bus(sch, [[30, 10], [30, 30]])
        WireManager.add_wire(sch, [100, 100], [120, 100])
        assert (30.0, 10.0) in _junction_points(sch)

    def test_unsplit_wire_tee_keeps_its_junction(self, sch):
        """A wire that ends on the middle of another wire needs its junction.

        KiCad does not join that T without one, so removing it on an
        unrelated edit disconnects the branch.
        """
        sheet = Sheet()
        sheet.segment("wire", (10, 10), (50, 10))
        sheet.segment("wire", (30, 10), (30, 30))
        sheet.junction((30, 10))
        sch.write_text(sheet.text(True, ROOT_UUID))
        WireManager.add_wire(sch, [100, 100], [120, 100])
        assert (30.0, 10.0) in _junction_points(sch)

    def test_wire_and_bus_crossing_get_no_junction(self, sch):
        WireManager.add_bus(sch, [[10, 10], [50, 10]])
        WireManager.add_wire(sch, [30, 0], [30, 10])
        assert _junction_points(sch) == set()

    def test_delete_bus(self, sch):
        WireManager.add_bus(sch, [[10, 10], [50, 10]])
        assert WireManager.delete_bus(sch, [50, 10], [10, 10]) is True
        assert _items(sch, "bus") == []
        assert WireManager.delete_bus(sch, [50, 10], [10, 10]) is False

    def test_bus_entry_handlers(self, sch):
        WireManager.add_bus(sch, [[10, 10], [50, 10]])
        h = Handlers()
        off = h._handle_add_schematic_bus_entry(
            {"schematicPath": str(sch), "position": {"x": 10, "y": 20}}
        )
        assert off["success"] is False
        added = h._handle_add_schematic_bus_entry(
            {"schematicPath": str(sch), "position": {"x": 20, "y": 10}, "direction": "up_left"}
        )
        assert added["wireEnd"] == {"x": 17.46, "y": 7.46}
        entry = _items(sch, "bus_entry")[0]
        assert [float(v) for v in entry[2][1:]] == [-2.54, -2.54]
        gone = h._handle_delete_schematic_bus_entry(
            {"schematicPath": str(sch), "position": {"x": 20, "y": 10}}
        )
        assert gone["success"] is True
        assert _items(sch, "bus_entry") == []

    def test_add_bus_warns_without_label(self, sch):
        h = Handlers()
        result = h._handle_add_schematic_bus(
            {"schematicPath": str(sch), "waypoints": [[0, 0], [10, 0]]}
        )
        assert result["success"] is True
        assert "no bus label" in result["warnings"][0]

    def test_segment_tee_needs_junction_to_join(self):
        a = (to_iu(0, 0), to_iu(10, 0))
        b = (to_iu(5, 0), to_iu(5, 5))
        assert len(segment_networks([a, b], [])) == 2
        assert len(segment_networks([a, b], [to_iu(5, 0)])) == 1


# --------------------------------------------------------------------------- #
# Bus report
# --------------------------------------------------------------------------- #


class TestDescribeSheetBuses:
    def test_entries_members_and_problems(self, tmp_path):
        sheet = Sheet()
        sheet.segment("bus", (25.4, 50.8), (101.6, 50.8))
        sheet.label("D[0..3]", (25.4, 50.8))
        member_drop(sheet, "R1", 38.1, "D1")
        member_drop(sheet, "R2", 50.8, None)
        member_drop(sheet, "R3", 63.5, "X9")
        sheet.label("E[0..1]", (10, 10))
        report = describe_sheet_buses(sexpdata.loads(sheet.text(True, ROOT_UUID)), {})

        (bus,) = report["buses"]
        assert bus["name"] == "D[0..3]"
        assert bus["members"] == ["D0", "D1", "D2", "D3"]
        assert [(e["net"], e["isMember"]) for e in bus["entries"]] == [
            ("D1", True),
            (None, False),
            ("X9", False),
        ]
        problems = " | ".join(report["problems"])
        assert "feeds a wire with no label" in problems
        assert "'X9', which is not a member" in problems
        assert "bus label 'E[0..1]' at (10, 10) does not touch a bus" in problems

    @pytest.mark.parametrize("other_pin", ["USB{DP DN}", "USB{DP}"])
    def test_unlabelled_bus_between_sheet_pins(self, other_pin):
        """KiCad takes an unlabelled bus's members from the sheet pins it joins."""
        sheet = Sheet()
        sheet.sheet("a.kicad_sch", [("USB{DP DN}", (101.6, 50.8))], SHEET_UUID, "A", 40.64)
        sheet.sheet("b.kicad_sch", [(other_pin, (101.6, 90.8))], OTHER_SHEET_UUID, "B", 80.64)
        sheet.segment("bus", (101.6, 50.8), (90, 50.8))
        sheet.segment("bus", (90, 50.8), (90, 90.8))
        sheet.segment("bus", (90, 90.8), (101.6, 90.8))
        report = describe_sheet_buses(sexpdata.loads(sheet.text(True, ROOT_UUID)), {})

        (bus,) = report["buses"]
        assert bus["name"] == "USB{DP DN}"
        assert {label["labelType"] for label in bus["labels"]} == {"sheet_pin"}
        if other_pin == "USB{DP DN}":
            assert bus["members"] == ["USB.DP", "USB.DN"]
            assert report["problems"] == []
        else:
            assert "different members" in report["problems"][0]

    def test_entry_drawn_from_the_wire_side(self, tmp_path):
        """KiCad joins either end of an entry to a bus; here (at ...) is the wire end."""
        sheet = Sheet()
        sheet.segment("bus", (50.8, 139.7), (50.8, 165.1))
        sheet.label("RLY[0..1]", (50.8, 139.7))
        sheet.entry((53.34, 149.86), size=(-2.54, 2.54))
        sheet.res("R1", 66.04, 149.86, 90)  # pin 1 at (62.23, 149.86)
        sheet.segment("wire", (53.34, 149.86), (62.23, 149.86))
        sheet.label("RLY0", (55.88, 149.86))
        path = tmp_path / "t.kicad_sch"
        path.write_text(sheet.text(True, ROOT_UUID))

        report = describe_sheet_buses(sexpdata.loads(path.read_text()), {})
        assert report["problems"] == []
        (entry,) = report["buses"][0]["entries"]
        assert entry["position"] == {"x": 50.8, "y": 152.4}
        assert entry["wireEnd"] == {"x": 53.34, "y": 149.86}
        assert (entry["net"], entry["isMember"]) == ("RLY0", True)

        from commands.schematic_analysis import find_orphaned_wires

        assert find_orphaned_wires(path)["count"] == 0
        assert WireManager.delete_bus_entry(path, [50.8, 152.4]) is True


# --------------------------------------------------------------------------- #
# Fan-out planning
# --------------------------------------------------------------------------- #


class TestPlanFanout:
    VERTICAL = [(to_iu(50.8, 40.64), to_iu(50.8, 81.28))]

    def test_worked_example(self):
        req = PinRequest("U1", "3", "D3", to_iu(63.5, 55.88), 180.0)
        (leg,), errors = plan_fanout(self.VERTICAL, [req])
        assert errors == []
        assert leg.entry_at == to_iu(50.8, 53.34)
        assert leg.entry_size == to_iu(2.54, 2.54)
        assert leg.wire == (to_iu(53.34, 55.88), to_iu(63.5, 55.88))
        assert leg.label_at == to_iu(53.34, 55.88)
        assert leg.label_angle == 0
        assert leg.warning is None

    def test_entry_moves_after_the_pin_near_the_bus_start(self):
        req = PinRequest("U1", "1", "D0", to_iu(40.64, 41.91), 0.0)
        (leg,), _ = plan_fanout(self.VERTICAL, [req])
        assert leg.entry_at == to_iu(50.8, 44.45)
        assert leg.label_angle == 180

    def test_horizontal_bus_and_pin_below(self):
        bus = [(to_iu(0, 20), to_iu(50, 20))]
        req = PinRequest("U1", "1", "A0", to_iu(10.16, 30.48), 90.0)
        (leg,), _ = plan_fanout(bus, [req])
        assert leg.label_at == to_iu(10.16, 22.54)
        assert leg.label_angle == 270
        assert leg.warning is None

    def test_pin_facing_away_warns(self):
        req = PinRequest("U1", "3", "D3", to_iu(63.5, 55.88), 0.0)
        (leg,), _ = plan_fanout(self.VERTICAL, [req])
        assert "away from the bus" in leg.warning

    def test_pin_too_close_or_out_of_reach(self):
        near = PinRequest("U1", "1", "D0", to_iu(51.8, 55.88), None)
        beyond = PinRequest("U1", "2", "D1", to_iu(63.5, 120.0), None)
        legs, errors = plan_fanout(self.VERTICAL, [near, beyond])
        assert legs == []
        assert len(errors) == 2

    def test_two_pins_on_one_row_collide(self):
        a = PinRequest("U1", "1", "D0", to_iu(63.5, 55.88), None)
        b = PinRequest("U2", "1", "D1", to_iu(76.2, 55.88), None)
        _, errors = plan_fanout(self.VERTICAL, [a, b])
        assert any("same bus entry" in e for e in errors)


# --------------------------------------------------------------------------- #
# Agreement with kicad-cli
# --------------------------------------------------------------------------- #

HIERARCHY_CASES = [
    # (case, parent label, sheet pin, parent members, child members, aliases)
    ("vector_rename", "ADDR[0..1]", "A[0..1]", ["ADDR0", "ADDR1"], ["A0", "A1"], None),
    ("vector_reversed_pin", "ADDR[0..1]", "A[1..0]", ["ADDR0", "ADDR1"], ["A0", "A1"], None),
    ("vector_offset", "ADDR[4..5]", "A[0..1]", ["ADDR4", "ADDR5"], ["A0", "A1"], None),
    (
        "vector_width_mismatch",
        "ADDR[0..3]",
        "A[0..1]",
        ["ADDR0", "ADDR1", "ADDR2"],
        ["A0", "A1"],
        None,
    ),
    (
        "group_named",
        "I2C{SDA SCL}",
        "BUS{SDA SCL}",
        ["I2C.SDA", "I2C.SCL"],
        ["BUS.SDA", "BUS.SCL"],
        None,
    ),
    ("group_renamed_members", "{SDA SCL}", "{X Y}", ["SDA", "SCL"], ["X", "Y"], None),
    (
        "group_named_to_anonymous",
        "I2C{SDA SCL}",
        "{SDA SCL}",
        ["I2C.SDA", "I2C.SCL"],
        ["SDA", "SCL"],
        None,
    ),
    (
        "group_vector_member",
        "M{D[0..1] CLK}",
        "N{D[0..1] CLK}",
        ["M.D0", "M.CLK"],
        ["N.D0", "N.CLK"],
        None,
    ),
    ("alias", "{MEM}", "{MEM}", ["D0", "CLK"], ["D0", "CLK"], {"MEM": ["D[0..1]", "CLK"]}),
    (
        "alias_prefixed",
        "U{MEM}",
        "V{MEM}",
        ["U.D0", "U.CLK"],
        ["V.D0", "V.CLK"],
        {"MEM": ["D[0..1]", "CLK"]},
    ),
]


@needs_kicad_cli
class TestAgreesWithKicad:
    @pytest.mark.parametrize(
        "case,parent_label,pin_name,parent_members,child_members,aliases", HIERARCHY_CASES
    )
    def test_hierarchical_bus_members(
        self, tmp_path, case, parent_label, pin_name, parent_members, child_members, aliases
    ):
        root = build_hier(
            tmp_path / case, parent_label, pin_name, parent_members, child_members, aliases
        )
        ours = joined_pin_sets(root, parent_members + child_members)
        assert ours == kicad_pin_sets(root)

    def test_global_bus_members_are_global(self, tmp_path):
        root = build_hier(
            tmp_path,
            "G[0..1]",
            "P[0..1]",
            ["G0", "G1"],
            ["G0", "G1"],
            parent_label_kind="global_label",
            child_label_kind="global_label",
            child_label="G[0..1]",
        )
        assert joined_pin_sets(root, ["G0", "G1"]) == kicad_pin_sets(root)

    def test_plain_sheet_pin_with_a_different_parent_name(self, tmp_path):
        write_project(tmp_path)
        root = Sheet()
        pin_at = (101.6, 50.8)
        root.sheet("child.kicad_sch", [("SDA", pin_at)])
        root.res("R1", 50.8, 54.61)  # pin 1 at (50.8, 50.8)
        root.segment("wire", (50.8, 50.8), pin_at)
        root.label("I2C_SDA", (76.2, 50.8))
        child = Sheet(f"/{ROOT_UUID}/{SHEET_UUID}")
        child.res("R101", 50.8, 54.61)
        child.segment("wire", (25.4, 50.8), (50.8, 50.8))
        child.label("SDA", (25.4, 50.8), "hierarchical_label")
        (tmp_path / "t.kicad_sch").write_text(root.text(True, ROOT_UUID))
        (tmp_path / "child.kicad_sch").write_text(child.text(False, _uid()))
        path = tmp_path / "t.kicad_sch"

        assert net_pins(path, "I2C_SDA") == net_pins(path, "SDA") == {"R1.1", "R101.1"}

    def test_same_child_label_on_two_sheets_stays_two_nets(self, tmp_path):
        """Two sub-sheets with hierarchical label EN, fed by EN_A and EN_B, are two nets."""
        write_project(tmp_path)
        root = Sheet()
        for i, (net, uid) in enumerate((("EN_A", SHEET_UUID), ("EN_B", OTHER_SHEET_UUID))):
            y = 54.61 + 40 * i
            root.res(f"R{i + 1}", 30, y - 3.81)  # pin 2 at (30, y)
            root.segment("wire", (30, y), (101.6, y))
            root.label(net, (50, y))
            root.sheet(f"c{i}.kicad_sch", [("EN", (101.6, y))], uid, f"S{i}", y - 13.97)
            child = Sheet(f"/{ROOT_UUID}/{uid}")
            child.label("EN", (25.4, 50.8), "hierarchical_label")
            child.segment("wire", (25.4, 50.8), (40.64, 50.8))
            child.res(f"R{101 + 100 * i}", 40.64, 54.61)  # pin 1 at (40.64, 50.8)
            (tmp_path / f"c{i}.kicad_sch").write_text(child.text(False, _uid()))
        path = tmp_path / "t.kicad_sch"
        path.write_text(root.text(True, ROOT_UUID))

        assert net_pins(path, "EN_A") == {"R1.2", "R101.1"}
        assert net_pins(path, "EN_B") == {"R2.2", "R201.1"}
        result = Handlers()._handle_get_net_connections(
            {"schematicPath": str(path), "netName": "EN"}
        )
        assert result["nets"] == ["/EN_A", "/EN_B"]
        assert "2 separate nets" in result["warnings"][0]

    def test_second_label_on_a_middle_sheet_does_not_split_the_net(self, tmp_path):
        """Root net P passes through sheet M (label MIDX on its wire) into sheet L."""
        write_project(tmp_path)
        mid_uuid, leaf_uuid = SHEET_UUID, OTHER_SHEET_UUID
        root = Sheet()
        root.res("R1", 30, 50.79)  # pin 2 at (30, 54.6)
        root.segment("wire", (30, 54.6), (101.6, 54.6))
        root.label("P", (50, 54.6))
        root.sheet("mid.kicad_sch", [("EN", (101.6, 54.6))], mid_uuid, "M", 40.64)
        mid = Sheet(f"/{ROOT_UUID}/{mid_uuid}")
        mid.label("EN", (25.4, 54.6), "hierarchical_label")
        mid.segment("wire", (25.4, 54.6), (101.6, 54.6))
        mid.label("MIDX", (50, 54.6))
        mid.sheet("leaf.kicad_sch", [("LEAF", (101.6, 54.6))], leaf_uuid, "L", 40.64)
        leaf = Sheet(f"/{ROOT_UUID}/{mid_uuid}/{leaf_uuid}")
        leaf.label("LEAF", (25.4, 50.8), "hierarchical_label")
        leaf.segment("wire", (25.4, 50.8), (40.64, 50.8))
        leaf.res("R101", 40.64, 54.61)
        path = tmp_path / "t.kicad_sch"
        path.write_text(root.text(True, ROOT_UUID))
        (tmp_path / "mid.kicad_sch").write_text(mid.text(False, _uid()))
        (tmp_path / "leaf.kicad_sch").write_text(leaf.text(False, _uid()))

        assert kicad_pin_sets(path) == {frozenset({"R1.2", "R101.1"})}
        for name in ("P", "MIDX", "LEAF"):
            assert net_pins(path, name) == {"R1.2", "R101.1"}, name

    def test_unlabelled_bus_between_two_sheet_pins(self, tmp_path):
        write_project(tmp_path)
        root = Sheet()
        root.sheet("a.kicad_sch", [("A[0..1]", (101.6, 50.8))], SHEET_UUID, "A", 40.64)
        root.sheet("b.kicad_sch", [("B[0..1]", (101.6, 90.8))], OTHER_SHEET_UUID, "B", 80.64)
        root.segment("bus", (101.6, 50.8), (90, 50.8))
        root.segment("bus", (90, 50.8), (90, 90.8))
        root.segment("bus", (90, 90.8), (101.6, 90.8))
        path = tmp_path / "t.kicad_sch"
        path.write_text(root.text(True, ROOT_UUID))
        for file, uid, prefix, first_ref in (
            ("a.kicad_sch", SHEET_UUID, "A", 101),
            ("b.kicad_sch", OTHER_SHEET_UUID, "B", 201),
        ):
            child = Sheet(f"/{ROOT_UUID}/{uid}")
            child.segment("bus", (25.4, 50.8), (101.6, 50.8))
            child.label(f"{prefix}[0..1]", (25.4, 50.8), "hierarchical_label")
            member_drop(child, f"R{first_ref}", 38.1, f"{prefix}0")
            (tmp_path / file).write_text(child.text(False, _uid()))

        assert net_pins(path, "A0") == net_pins(path, "B0") == {"R101.1", "R201.1"}

    def test_list_schematic_nets_reports_members_not_bus_labels(self, tmp_path):
        root = build_hier(tmp_path, "ADDR[0..1]", "A[0..1]", ["ADDR0", "ADDR1"], ["A0", "A1"])
        result = Handlers()._handle_list_schematic_nets({"schematicPath": str(root)})
        assert result["success"] is True
        assert result["connectivitySource"] == "kicad-cli"
        by_name = {n["name"]: n for n in result["nets"]}
        assert not any("[" in name for name in by_name)
        assert by_name["/ADDR0"]["labels"] == ["A0", "ADDR0"]
        pins = {f'{c["component"]}.{c["pin"]}' for c in by_name["/ADDR0"]["connections"]}
        assert pins == {"R1.1", "R101.1"}
        assert by_name["/ADDR0"]["connected_pin_count"] == 2
        assert {b["name"] for b in result["buses"]} == {"ADDR[0..1]", "A[0..1]"}

    def test_without_kicad_cli_nets_fall_back_to_labels(self, tmp_path, monkeypatch):
        import commands.design_netlist as design_netlist

        monkeypatch.setattr(
            design_netlist, "export_design_netlist", lambda path: "kicad-cli was not found"
        )
        root = build_hier(tmp_path, "ADDR[0..1]", "A[0..1]", ["ADDR0", "ADDR1"], ["A0", "A1"])
        result = Handlers()._handle_list_schematic_nets({"schematicPath": str(root)})
        assert result["connectivitySource"] == "labels"
        assert "kicad-cli was not found" in result["warnings"][0]
        names = {n["name"] for n in result["nets"]}
        assert {"ADDR0", "A0"} <= names and "ADDR[0..1]" not in names

    @pytest.mark.parametrize("case", ["crossing", "diagonal_tee", "pin_mid_wire"])
    def test_unrelated_edit_keeps_load_bearing_junctions(self, tmp_path, case):
        """Each sheet has a junction KiCad needs; an unrelated wire edit must keep it."""
        write_project(tmp_path)
        sheet = Sheet()
        sheet.res("R1", 20, 46.19)  # pin 2 at (20, 50)
        if case == "crossing":
            sheet.res("R2", 60, 46.19)  # pin 2 at (60, 50)
            sheet.res("R3", 40, 26.19)  # pin 2 at (40, 30)
            sheet.res("R4", 40, 73.81)  # pin 1 at (40, 70)
            sheet.segment("wire", (20, 50), (60, 50))
            sheet.segment("wire", (40, 30), (40, 70))
            junction = (40, 50)
        elif case == "diagonal_tee":
            sheet.res("R2", 60, 93.81)  # pin 1 at (60, 90)
            sheet.res("R3", 40, 26.19)  # pin 2 at (40, 30)
            sheet.segment("wire", (20, 50), (60, 90))
            sheet.segment("wire", (40, 30), (40, 70))
            junction = (40, 70)
        else:
            sheet.res("R2", 60, 46.19)  # pin 2 at (60, 50)
            sheet.res("R3", 40, 53.81)  # pin 1 at (40, 50), on the wire's middle
            sheet.segment("wire", (20, 50), (60, 50))
            junction = (40, 50)
        sheet.junction(junction)
        path = tmp_path / "t.kicad_sch"
        path.write_text(sheet.text(True, ROOT_UUID))
        before = kicad_pin_sets(path)
        assert any(len(net) >= 3 for net in before)

        WireManager.add_wire(path, [100, 100], [120, 100])

        assert (float(junction[0]), float(junction[1])) in _junction_points(path)
        assert kicad_pin_sets(path) == before

    def test_tools_build_a_design_kicad_accepts(self, tmp_path):
        """Draw buses, labels and fan-outs with the tools, then check KiCad's netlist and ERC."""
        write_project(tmp_path)
        root = Sheet()
        pin_at = (101.6, 50.8)
        root.sheet("child.kicad_sch", [("D[0..3]", pin_at)])
        child = Sheet(f"/{ROOT_UUID}/{SHEET_UUID}")
        # Rotated 90 degrees, pin 1 points left at the bus: pin 1 is at (x - 3.81, y).
        for i in range(4):
            root.res(f"R{i + 1}", 66.04, 60.96 + 7.62 * i, 90)
            child.res(f"R{101 + i}", 66.04, 60.96 + 7.62 * i, 90)
        root_path = tmp_path / "t.kicad_sch"
        child_path = tmp_path / "child.kicad_sch"
        root_path.write_text(root.text(True, ROOT_UUID))
        child_path.write_text(child.text(False, _uid()))

        h = Handlers()
        for path, label_kind in ((root_path, "label"), (child_path, "hierarchical_label")):
            bus = [[101.6, 50.8], [50.8, 50.8], [50.8, 88.9]]
            assert h._handle_add_schematic_bus({"schematicPath": str(path), "waypoints": bus})[
                "success"
            ]
            if label_kind == "label":
                assert h._handle_add_schematic_net_label(
                    {"schematicPath": str(path), "netName": "D[0..3]", "position": [76.2, 50.8]}
                )["success"]
            else:
                assert WireManager.add_hierarchical_label(path, "D[0..3]", [101.6, 50.8])
            refs = (
                ["R1", "R2", "R3", "R4"] if path == root_path else ["R101", "R102", "R103", "R104"]
            )
            result = h._handle_connect_pins_to_bus(
                {
                    "schematicPath": str(path),
                    "busPoint": {"x": 50.8, "y": 70},
                    "connections": [
                        {"reference": ref, "pin": "1", "net": f"D{i}"} for i, ref in enumerate(refs)
                    ],
                }
            )
            assert result["success"] is True, result
            assert "warnings" not in result, result

        nets = kicad_netlist(root_path)
        for i in range(4):
            assert frozenset({f"R{i + 1}.1", f"R{101 + i}.1"}) in nets.values()
        assert joined_pin_sets(root_path, [f"D{i}" for i in range(4)]) == kicad_pin_sets(root_path)

        bus_rules = {
            "net_not_bus_member",
            "bus_to_net_conflict",
            "bus_entry_needed",
            "bus_to_bus_conflict",
            "bus_definition_conflict",
            "label_dangling",
            "unconnected_wire_endpoint",
            "wire_dangling",
        }
        flagged = [v for v in kicad_erc(root_path) if v["type"] in bus_rules]
        assert flagged == []
        for path in (root_path, child_path):
            report = h._handle_list_schematic_buses({"schematicPath": str(path)})
            assert report["problems"] == []

    def test_connect_pins_to_bus_twice_is_refused(self, tmp_path):
        write_project(tmp_path)
        sheet = Sheet()
        sheet.res("R1", 66.04, 60.96, 90)
        path = tmp_path / "t.kicad_sch"
        path.write_text(sheet.text(True, ROOT_UUID))
        h = Handlers()
        h._handle_add_schematic_bus(
            {"schematicPath": str(path), "waypoints": [[50.8, 50.8], [50.8, 88.9]]}
        )
        params = {
            "schematicPath": str(path),
            "busPoint": {"x": 50.8, "y": 70},
            "connections": [{"reference": "R1", "pin": "1", "net": "D0"}],
        }
        assert h._handle_connect_pins_to_bus(params)["success"] is True
        before = path.read_text()
        again = h._handle_connect_pins_to_bus(params)
        assert again["success"] is False
        assert "already has a bus entry" in again["message"]
        assert path.read_text() == before

    def test_wide_vector_does_not_break_net_queries(self, tmp_path, monkeypatch):
        import commands.design_netlist as design_netlist

        # kicad-cli takes seconds to expand this bus; the parser is what is under test.
        monkeypatch.setattr(design_netlist, "export_design_netlist", lambda path: "stubbed")
        root = build_hier(tmp_path, "D[0..9999]", "A[0..1]", ["D0"], ["A0"])
        h = Handlers()
        assert h._handle_list_schematic_nets({"schematicPath": str(root)})["success"] is True
        assert h._handle_get_net_connections({"schematicPath": str(root), "netName": "D0"})[
            "success"
        ]

    def test_connect_pins_to_bus_rejects_non_members(self, tmp_path):
        write_project(tmp_path)
        sheet = Sheet()
        sheet.res("R1", 66.04, 60.96, 90)
        path = tmp_path / "t.kicad_sch"
        path.write_text(sheet.text(True, ROOT_UUID))
        h = Handlers()
        h._handle_add_schematic_bus(
            {"schematicPath": str(path), "waypoints": [[50.8, 50.8], [50.8, 88.9]]}
        )
        WireManager.add_label(path, "D[0..3]", [50.8, 50.8])
        before = path.read_text()
        result = h._handle_connect_pins_to_bus(
            {
                "schematicPath": str(path),
                "busPoint": {"x": 50.8, "y": 70},
                "connections": [{"reference": "R1", "pin": "1", "net": "D9"}],
            }
        )
        assert result["success"] is False
        assert "not a member" in result["message"]
        assert path.read_text() == before


# --------------------------------------------------------------------------- #
# Existing net tools on a bus
# --------------------------------------------------------------------------- #


class TestNetToolsOnBuses:
    @pytest.fixture
    def flat(self, tmp_path) -> Path:
        write_project(tmp_path)
        sheet = Sheet()
        sheet.segment("bus", (25.4, 50.8), (101.6, 50.8))
        sheet.label("D[0..3]", (25.4, 50.8))
        sheet.label("E[0..1]", (10.16, 10.16))
        member_drop(sheet, "R1", 38.1, "D1")
        member_drop(sheet, "R2", 50.8, "D1")
        path = tmp_path / "t.kicad_sch"
        path.write_text(sheet.text(True, ROOT_UUID))
        return path

    def test_floating_labels(self, flat):
        from commands.wire_connectivity import list_floating_labels

        schematic = SchematicManager.load_schematic(str(flat))
        floating = list_floating_labels(schematic, str(flat))
        assert {(f["name"], f["type"]) for f in floating} == {("E[0..1]", "bus_label")}

    def test_net_at_point_on_a_bus(self, flat):
        from commands.wire_connectivity import get_net_at_point

        schematic = SchematicManager.load_schematic(str(flat))
        result = get_net_at_point(schematic, str(flat), 90.0, 50.8)
        assert result["net_name"] == "D[0..3]"
        assert result["source"] == "bus"
        assert result["bus"]["members"] == ["D0", "D1", "D2", "D3"]

    def test_wire_on_a_bus_entry_is_not_orphaned(self, flat):
        from commands.schematic_analysis import find_orphaned_wires

        assert find_orphaned_wires(flat)["count"] == 0

    def test_get_net_connections_for_a_bus_label(self, flat):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
        result = iface._handle_get_net_connections(
            {"schematicPath": str(flat), "netName": "D[0..3]"}
        )
        assert result["success"] is True
        assert result["bus"]["members"] == ["D0", "D1", "D2", "D3"]
        d1 = {f'{c["component"]}.{c["pin"]}' for c in result["memberConnections"]["D1"]}
        assert d1 == {"R1.1", "R2.1"}
        assert result["memberConnections"]["D0"] == []
