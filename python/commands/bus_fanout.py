"""
Plan the bus entries, wires and labels that connect symbol pins to a bus.

Each pin gets one leg: a 45 degree bus entry on the bus, a straight wire from
the entry to the pin, and a net label on the wire naming the bus member the
pin carries. A vertical bus feeds pins with horizontal wires at the pin's y;
a horizontal bus feeds them with vertical wires at the pin's x. The entry's
wire end sits 2.54 mm off the bus toward the pin, and its bus end 2.54 mm
along the bus, before the pin if that point is on the bus, otherwise after it.

Example, a vertical bus from (50.8, 40.64) to (50.8, 81.28) and pin U1/3 at
(63.5, 55.88): the entry runs from (50.8, 53.34) to (53.34, 55.88), the wire
from (53.34, 55.88) to (63.5, 55.88), and the label sits at (53.34, 55.88)
with angle 0 so its text runs toward the pin.

This module only computes the plan; the caller writes it.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from commands.bus_connectivity import IU, Segment, on_segment

_IU_PER_MM = 10000
_ENTRY = 25400  # 2.54 mm, one entry step


@dataclass(frozen=True)
class PinRequest:
    reference: str
    pin: str
    net: str
    at: IU
    # Outward direction of the pin in degrees (0 right, 90 up, 180 left,
    # 270 down), or None when unknown.
    angle: Optional[float]


@dataclass(frozen=True)
class Leg:
    request: PinRequest
    entry_at: IU
    entry_size: IU
    wire: Optional[Segment]
    label_at: IU
    label_angle: int
    warning: Optional[str]


def mm(value: int) -> float:
    return value / _IU_PER_MM


def _sign(value: int) -> int:
    return 1 if value > 0 else -1


def _leg_on(seg: Segment, req: PinRequest) -> Optional[Tuple[int, Leg]]:
    """The leg from *req* to *seg*, with its length, or None if it cannot reach."""
    (ax, ay), (bx, by) = seg
    px, py = req.at
    if ax == bx and ay != by:
        offset = px - ax
        if abs(offset) < _ENTRY:
            return None
        side = _sign(offset)
        wire_end = (ax + side * _ENTRY, py)
        bus_ends = [(ax, py - _ENTRY), (ax, py + _ENTRY)]
        label_angle = 0 if side > 0 else 180
        facing = 180 if side > 0 else 0
    elif ay == by and ax != bx:
        offset = py - ay
        if abs(offset) < _ENTRY:
            return None
        side = _sign(offset)
        wire_end = (px, ay + side * _ENTRY)
        bus_ends = [(px - _ENTRY, ay), (px + _ENTRY, ay)]
        # Schematic y grows downward: a pin below the bus (side > 0) gets a
        # label whose text runs down, angle 270.
        label_angle = 270 if side > 0 else 90
        facing = 90 if side > 0 else 270
    else:
        return None

    bus_end = next((pt for pt in bus_ends if on_segment(pt, seg)), None)
    if bus_end is None:
        return None
    warning = None
    if req.angle is not None and round(req.angle) % 360 != facing:
        warning = (
            f"{req.reference}/{req.pin} points at {round(req.angle) % 360} degrees, away from "
            "the bus; the wire to it may cross its symbol"
        )
    leg = Leg(
        request=req,
        entry_at=bus_end,
        entry_size=(wire_end[0] - bus_end[0], wire_end[1] - bus_end[1]),
        wire=(wire_end, req.at) if wire_end != req.at else None,
        label_at=wire_end,
        label_angle=label_angle,
        warning=warning,
    )
    return abs(offset), leg


def plan_fanout(
    bus_segments: Sequence[Segment], requests: Sequence[PinRequest]
) -> Tuple[List[Leg], List[str]]:
    """Plan one leg per request on the nearest segment the pin faces.

    A segment the pin faces away from is used only when no other segment of
    the bus can take the pin; that leg carries a warning.

    Returns (legs, errors). Any error means the plan must not be written.
    """
    legs: List[Leg] = []
    errors: List[str] = []
    for req in requests:
        options = [found for seg in bus_segments if (found := _leg_on(seg, req)) is not None]
        if not options:
            errors.append(
                f"{req.reference}/{req.pin} at ({mm(req.at[0]):g}, {mm(req.at[1]):g}) cannot "
                "reach the bus: it must sit at least 2.54 mm to one side of a straight bus "
                "segment, level with a point 2.54 mm inside that segment's ends"
            )
            continue
        # Prefer a segment the pin faces; among those, the nearest.
        legs.append(min(options, key=lambda o: (o[1].warning is not None, o[0]))[1])

    used_entries = {}
    used_wire_ends = {}
    for leg in legs:
        name = f"{leg.request.reference}/{leg.request.pin}"
        entry_key = (leg.entry_at, leg.entry_size)
        if entry_key in used_entries:
            errors.append(f"{name} and {used_entries[entry_key]} need the same bus entry")
        used_entries.setdefault(entry_key, name)
        if leg.label_at in used_wire_ends:
            errors.append(f"{name} and {used_wire_ends[leg.label_at]} would share a wire end")
        used_wire_ends.setdefault(leg.label_at, name)
    return legs, errors
