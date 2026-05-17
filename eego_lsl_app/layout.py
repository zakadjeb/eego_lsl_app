from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Electrode:
    name: str
    x: float
    y: float
    role: str = "reference"  # reference, eog, ref, gnd, auxiliary


def contact_role(name: str) -> str:
    n = name.strip().rstrip(":").upper()
    if n == "EOG":
        return "eog"
    if n in {"REF", "REFERENCE"}:
        return "ref"
    if n in {"GND", "GROUND"}:
        return "gnd"
    return "reference"


def reference_electrodes(electrodes: Iterable[Electrode]) -> list[Electrode]:
    """Contacts that are part of the 64 referential SDK channels.

    For the EE-21x/EE-22x 64-channel cap, Appendix A lists EOG as Ref 32.
    Therefore EOG must stay in the 64-channel reference order for EEG and
    impedance mapping. REF and GND are separate physical contacts.
    """
    return [e for e in electrodes if e.role in {"reference", "eog"}]


def impedance_contacts(electrodes: Iterable[Electrode]) -> list[Electrode]:
    """Contacts expected to appear as impedance_reference columns.

    The 64 referential impedance columns follow the EEG reference order, which
    includes EOG as Ref 32 in the eego manual. REF may appear as an additional
    impedance_reference column after the 64 referential inputs. GND is handled
    separately when the SDK exposes it as impedance_ground.
    """
    return [e for e in electrodes if e.role != "gnd"]


def auxiliary_contacts(electrodes: Iterable[Electrode]) -> list[Electrode]:
    return [e for e in electrodes if e.role in {"eog", "ref", "gnd", "auxiliary"}]


def load_layout(path: str | Path) -> list[Electrode]:
    """Load electrode layout from TXT, CSV, TSV, or JSON.

    Supported text formats include ANT cap files such as:
        Fp1: 82.7 29.37
        Fp2  82.7 -29.47

    ANT/eego cap files usually store coordinates as:
        x = anterior/posterior axis, positive toward the front
        y = left/right axis, positive toward the participant's left

    The GUI converts this to a normal EEG topomap view where the nose/front is up.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_json(path)
    if suffix in {".csv", ".tsv"}:
        return _load_csv(path, delimiter="," if suffix == ".csv" else "\t")
    return _load_txt(path)


def _load_json(path: Path) -> list[Electrode]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("electrodes", [])
    electrodes = []
    for item in data:
        name = str(item["name"]).strip().rstrip(":")
        electrodes.append(Electrode(name, float(item["x"]), float(item["y"]), str(item.get("role", contact_role(name))).lower()))
    if not electrodes:
        raise ValueError(f"No electrodes found in layout file: {path}")
    return electrodes


def _load_csv(path: Path, delimiter: str) -> list[Electrode]:
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8-sig"), delimiter=delimiter))
    if rows and {"name", "x", "y"}.issubset({k.lower() for k in rows[0].keys()}):
        electrodes = []
        for row in rows:
            lower = {k.lower(): v for k, v in row.items()}
            name = lower["name"].strip().rstrip(":")
            electrodes.append(Electrode(name, float(lower["x"]), float(lower["y"]), lower.get("role", contact_role(name)).strip().lower() if lower.get("role") else contact_role(name)))
        if not electrodes:
            raise ValueError(f"No electrodes found in layout file: {path}")
        return electrodes

    electrodes = []
    for row in csv.reader(path.open(newline="", encoding="utf-8-sig"), delimiter=delimiter):
        if len(row) >= 3 and row[0].strip() and not row[0].lower().startswith("name"):
            name = row[0].strip().rstrip(":")
            electrodes.append(Electrode(name, float(row[1]), float(row[2]), contact_role(name)))
    if not electrodes:
        raise ValueError(f"No electrodes found in layout file: {path}")
    return electrodes


def _load_txt(path: Path) -> list[Electrode]:
    electrodes: list[Electrode] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[\s,;]+", line.replace(":", " "))
        if len(parts) < 3:
            continue
        try:
            name = parts[0].strip().rstrip(":")
            electrodes.append(Electrode(name, float(parts[1]), float(parts[2]), contact_role(name)))
        except ValueError:
            continue
    if not electrodes:
        raise ValueError(f"No electrodes found in layout file: {path}")
    return electrodes


def normalize_to_canvas(
    electrodes: Iterable[Electrode],
    width: int,
    height: int,
    margin: int = 70,
    orientation: str = "eego_topomap",
) -> dict[str, tuple[float, float]]:
    """Map electrode coordinates to canvas coordinates.

    orientation="eego_topomap" is the default for ANT/eego cap files:
      - raw x controls anterior/posterior position: Fp/front goes up.
      - raw y controls left/right position: left electrodes go to the viewer's left.

    orientation="xy" keeps the older generic behaviour.
    """
    electrodes = list(electrodes)
    if not electrodes:
        return {}

    if orientation == "xy":
        plot_points = [(e.name, e.x, e.y) for e in electrodes]
    else:
        # ANT/eego convention: x = front/back, y = left/right.
        # Display convention: canvas x = left/right, canvas y = front/back.
        # Positive raw y is left hemisphere, so it must be drawn to the left.
        plot_points = [(e.name, -e.y, e.x) for e in electrodes]

    xs = [p[1] for p in plot_points]
    ys = [p[2] for p in plot_points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)

    available_w = max(width - 2 * margin, 100)
    available_h = max(height - 2 * margin, 100)
    scale = min(available_w / span_x, available_h / span_y)
    cx_raw = (min_x + max_x) / 2
    cy_raw = (min_y + max_y) / 2
    cx = width / 2
    cy = height / 2 + 10

    return {name: (cx + (x - cx_raw) * scale, cy - (y - cy_raw) * scale) for name, x, y in plot_points}


def synthetic_grid(names: list[str]) -> list[Electrode]:
    if not names:
        return []
    n = len(names)
    cols = math.ceil(math.sqrt(n))
    electrodes = []
    for i, name in enumerate(names):
        row, col = divmod(i, cols)
        electrodes.append(Electrode(name, float(col), float(-row)))
    return electrodes
