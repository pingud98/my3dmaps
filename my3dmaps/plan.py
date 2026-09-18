"""Filament slot planning.

The printer has 4 simultaneous slots.  Because the model is decomposed into
elevation-ordered slabs (plus a thin surface skin), each color occupies a
contiguous Z range.  A 5th/6th color can therefore reuse a slot whose color
has already finished printing, at the cost of one manual filament swap at a
known layer height.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColorRange:
    color: str
    labels: list[str]
    z_min: float
    z_max: float


@dataclass
class Swap:
    z_mm: float  # pause *before* the layer starting at this height
    slot: int  # 1-based
    from_color: str
    to_color: str
    from_labels: list[str]
    to_labels: list[str]


@dataclass
class SlotPlan:
    slots: dict[str, int]  # color -> slot (1-based). Colors that reuse a slot appear with the same slot.
    initial: list[str]  # color loaded in each slot at the start (index = slot-1)
    swaps: list[Swap] = field(default_factory=list)
    feasible: bool = True
    ranges: list[ColorRange] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "slots": self.slots,
            "initial": self.initial,
            "swaps": [s.__dict__ for s in self.swaps],
            "ranges": [r.__dict__ for r in self.ranges],
        }


def plan_slots(ranges: list[ColorRange], max_slots: int = 4, margin_mm: float = 0.4) -> SlotPlan:
    """Greedy slot assignment in print order; reuse a slot once its color is finished."""
    ranges = sorted(ranges, key=lambda r: (r.z_min, r.z_max))
    slot_of: dict[str, int] = {}
    slot_busy_until: list[float] = []  # per slot: z_max of the color currently in it
    slot_color: list[str] = []
    initial: list[str] = []
    swaps: list[Swap] = []
    feasible = True
    for r in ranges:
        if r.color in slot_of:
            continue
        if len(slot_busy_until) < max_slots:
            # an empty slot: no swap needed
            slot_busy_until.append(r.z_max)
            slot_color.append(r.color)
            initial.append(r.color)
            slot_of[r.color] = len(slot_busy_until)
            continue
        # all slots taken: reuse one whose color finished below this one's start (manual swap)
        cand = [i for i, zmax in enumerate(slot_busy_until) if zmax + margin_mm <= r.z_min]
        if cand:
            i = min(cand, key=lambda k: slot_busy_until[k])
            old = slot_color[i]
            old_labels = next((x.labels for x in ranges if x.color == old), [])
            swaps.append(Swap(round(r.z_min, 3), i + 1, old, r.color, old_labels, r.labels))
            slot_color[i] = r.color
            slot_busy_until[i] = r.z_max
            slot_of[r.color] = i + 1
        else:
            feasible = False
            slot_of[r.color] = 0
    return SlotPlan(slot_of, initial, swaps, feasible, ranges)
