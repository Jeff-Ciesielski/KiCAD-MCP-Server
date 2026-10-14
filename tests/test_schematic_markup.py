"""Tests for text note editing, junction editing and the DNP flag."""

import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.symbol_dnp import set_dnp, symbol_dnp  # noqa: E402


def _iface() -> Any:
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        return KiCADInterface.__new__(KiCADInterface)


TEXTS = textwrap.dedent("""\
    (kicad_sch (version 20250114) (generator "test")
    \t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
    \t(paper "A4")
    \t(text "I = V / R" (exclude_from_sim no) (at 50 50 0)
    \t\t(effects (font (size 1.27 1.27) (bold yes)) (justify left bottom))
    \t\t(uuid "11111111-0000-0000-0000-000000000001"))
    \t(text_box "box note" (exclude_from_sim no) (at 80 80 0) (size 20 10)
    \t\t(effects (font (size 1.27 1.27)) (justify left top))
    \t\t(uuid "11111111-0000-0000-0000-000000000002"))
    \t(text "twin" (at 10 10 0) (effects (font (size 1.27 1.27))) (uuid "t1"))
    \t(text "twin" (at 10 10 0) (effects (font (size 1.27 1.27))) (uuid "t2"))
    )
    """)


@pytest.fixture
def texts(tmp_path: Path) -> Path:
    path = tmp_path / "t.kicad_sch"
    path.write_text(TEXTS, encoding="utf-8")
    return path


def test_edit_text_by_uuid(texts: Path) -> None:
    result = _iface()._handle_edit_schematic_text(
        {
            "schematicPath": str(texts),
            "uuid": "11111111-0000-0000-0000-000000000001",
            "text": "I = V / R\nR = 4.7k",
            "newPosition": {"x": 60, "y": 55},
            "bold": False,
            "justify": "center",
        }
    )
    assert result["success"], result
    after = result["after"]
    assert after["text"] == "I = V / R\nR = 4.7k"
    assert after["position"] == {"x": 60, "y": 55}
    assert (after["bold"], after["justify"], after["uuid"]) == (
        False,
        "center",
        "11111111-0000-0000-0000-000000000001",
    )
    content = texts.read_text(encoding="utf-8")
    assert '"I = V / R\\nR = 4.7k"' in content
    assert "(justify bottom)" in content and "center" not in content


def test_edit_text_box_by_position_keeps_size(texts: Path) -> None:
    result = _iface()._handle_edit_schematic_text(
        {"schematicPath": str(texts), "position": [80.2, 80], "fontSize": 2}
    )
    assert result["success"], result
    assert result["after"]["kind"] == "text_box"
    assert result["after"]["font_size"] == 2
    assert result["after"]["size"] == {"width": 20, "height": 10}


def test_text_at_shared_position_needs_uuid(texts: Path) -> None:
    result = _iface()._handle_delete_schematic_text(
        {"schematicPath": str(texts), "position": [10, 10]}
    )
    assert not result["success"] and "uuid" in result["message"]
    result = _iface()._handle_delete_schematic_text({"schematicPath": str(texts), "uuid": "t2"})
    assert result["success"], result
    remaining = _iface()._handle_list_schematic_texts({"schematicPath": str(texts)})["texts"]
    assert [t["uuid"] for t in remaining if t["text"] == "twin"] == ["t1"]


# A T: a wire from (20,10) ends on the middle of the wire (10,10)-(30,10)
# coming down from above, a plain crossing at (50,10), and a corner at (60,10).
JUNCTIONS = textwrap.dedent("""\
    (kicad_sch (version 20250114) (generator "test")
    \t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
    \t(paper "A4")
    \t(wire (pts (xy 10 10) (xy 30 10)) (stroke (width 0) (type default)) (uuid "w1"))
    \t(wire (pts (xy 20 0) (xy 20 10)) (stroke (width 0) (type default)) (uuid "w2"))
    \t(junction (at 20 10) (diameter 0) (color 0 0 0 0) (uuid "j1"))
    \t(wire (pts (xy 40 10) (xy 60 10)) (stroke (width 0) (type default)) (uuid "w3"))
    \t(wire (pts (xy 50 0) (xy 50 20)) (stroke (width 0) (type default)) (uuid "w4"))
    \t(wire (pts (xy 60 10) (xy 60 30)) (stroke (width 0) (type default)) (uuid "w5"))
    )
    """)


@pytest.fixture
def junctions(tmp_path: Path) -> Path:
    path = tmp_path / "j.kicad_sch"
    path.write_text(JUNCTIONS, encoding="utf-8")
    return path


def test_junction_at_crossing_and_corner(junctions: Path) -> None:
    iface = _iface()
    corner = iface._handle_add_schematic_junction(
        {"schematicPath": str(junctions), "position": {"x": 60, "y": 10}}
    )
    assert not corner["success"] and "2 wire" in corner["message"]
    crossing = iface._handle_add_schematic_junction(
        {"schematicPath": str(junctions), "position": {"x": 50, "y": 10}}
    )
    assert crossing["success"], crossing
    assert crossing["wireConnections"] == 4
    listed = iface._handle_list_schematic_junctions({"schematicPath": str(junctions)})
    assert sorted((j["position"]["x"], j["wireConnections"]) for j in listed["junctions"]) == [
        (20, 3),
        (50, 4),
    ]

    removed = iface._handle_delete_schematic_junction(
        {"schematicPath": str(junctions), "position": {"x": 50, "y": 10}}
    )
    assert removed["success"] and "note" in removed and "warning" not in removed


def test_deleting_a_t_junction_warns(junctions: Path) -> None:
    removed = _iface()._handle_delete_schematic_junction(
        {"schematicPath": str(junctions), "position": {"x": 20.1, "y": 10}}
    )
    assert removed["success"], removed
    assert "warning" in removed
    assert "(junction" not in junctions.read_text(encoding="utf-8")


# R1 has two units; its first unit has no (dnp) yet. The variant's (dnp yes)
# inside (instances) must not change.
SYMBOLS = textwrap.dedent("""\
    (kicad_sch (version 20250114) (generator "test")
    \t(lib_symbols (symbol "Device:R" (in_bom yes) (on_board yes) (dnp no)))
    \t(symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (in_bom yes) (on_board yes)
    \t\t(uuid "s1")
    \t\t(property "Reference" "R1" (at 10 8 0))
    \t\t(property "Description" "pin (A" (at 10 8 0))
    \t\t(instances (project "p" (path "/x" (reference "R1") (unit 1)
    \t\t\t(variant (name "Lite") (dnp yes))))))
    \t(symbol (lib_id "Device:R") (at 30 10 0) (unit 2) (in_bom yes) (on_board yes) (dnp no)
    \t\t(uuid "s2")
    \t\t(property "Reference" "R1" (at 30 8 0)))
    \t(symbol (lib_id "Device:R") (at 50 10 0) (unit 1) (in_bom yes) (on_board yes) (dnp no)
    \t\t(uuid "s3")
    \t\t(property "Reference" "R10" (at 50 8 0)))
    )
    """)


def test_set_dnp_on_every_unit_only() -> None:
    content, units = set_dnp(SYMBOLS, "R1", True)
    assert units == 2
    assert content.count("(dnp yes)") == 3  # the 2 units and the variant
    assert '(on_board yes) (dnp yes)\n\t\t(uuid "s1")' in content
    assert '(variant (name "Lite") (dnp yes))' in content
    r10 = content[content.index('(uuid "s3")') - 120 :]
    assert "(dnp no)" in r10
    assert '(lib_symbols (symbol "Device:R" (in_bom yes) (on_board yes) (dnp no)))' in content

    cleared, _ = set_dnp(content, "R1", False)
    assert cleared.count("(dnp yes)") == 1  # the variant keeps its own flag


def test_edit_component_dnp_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "s.kicad_sch"
    path.write_text(SYMBOLS, encoding="utf-8")
    iface = _iface()
    with patch.object(iface, "_reload_kicad_schematic"):
        result = iface._handle_edit_schematic_component(
            {"schematicPath": str(path), "reference": "R1", "dnp": True}
        )
    assert result["success"], result
    assert result["updated"] == {"dnp": True, "dnpUnits": 2}
    got = iface._handle_get_schematic_component({"schematicPath": str(path), "reference": "R1"})
    assert got["dnp"] is True
    assert symbol_dnp('(symbol (lib_id "x") (dnp no))') is False
