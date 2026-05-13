from __future__ import annotations

import math
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


F1_2024_FASTF1_EVENTS: dict[int, str] = {
    0: "Australian Grand Prix",
    2: "Chinese Grand Prix",
    3: "Bahrain Grand Prix",
    4: "Spanish Grand Prix",
    5: "Monaco Grand Prix",
    6: "Canadian Grand Prix",
    7: "British Grand Prix",
    9: "Hungarian Grand Prix",
    10: "Belgian Grand Prix",
    11: "Italian Grand Prix",
    12: "Singapore Grand Prix",
    13: "Japanese Grand Prix",
    14: "Abu Dhabi Grand Prix",
    15: "United States Grand Prix",
    16: "São Paulo Grand Prix",
    17: "Austrian Grand Prix",
    19: "Mexico City Grand Prix",
    20: "Azerbaijan Grand Prix",
    26: "Dutch Grand Prix",
    27: "Emilia Romagna Grand Prix",
    29: "Saudi Arabian Grand Prix",
    30: "Miami Grand Prix",
    31: "Las Vegas Grand Prix",
    32: "Qatar Grand Prix",
}

F1_2024_TRACK_NAMES: dict[int, str] = {
    0: "Albert Park",
    1: "Circuit Paul Ricard",
    2: "Shanghai International Circuit",
    3: "Bahrain International Circuit",
    4: "Circuit de Barcelona-Catalunya",
    5: "Circuit de Monaco",
    6: "Circuit Gilles Villeneuve",
    7: "Silverstone Circuit",
    8: "Hockenheimring",
    9: "Hungaroring",
    10: "Circuit de Spa-Francorchamps",
    11: "Autodromo Nazionale Monza",
    12: "Marina Bay Street Circuit",
    13: "Suzuka Circuit",
    14: "Yas Marina Circuit",
    15: "Circuit of the Americas",
    16: "Interlagos",
    17: "Red Bull Ring",
    18: "Sochi Autodrom",
    19: "Autodromo Hermanos Rodriguez",
    20: "Baku City Circuit",
    21: "Bahrain Short",
    22: "Silverstone Short",
    23: "Circuit of the Americas Short",
    24: "Suzuka Short",
    25: "Hanoi Street Circuit",
    26: "Circuit Zandvoort",
    27: "Autodromo Internazionale Enzo e Dino Ferrari",
    28: "Autodromo Internacional do Algarve",
    29: "Jeddah Corniche Circuit",
    30: "Miami International Autodrome",
    31: "Las Vegas Strip Circuit",
    32: "Lusail International Circuit",
}


def track_name_for_id(track_id: int | None) -> str | None:
    if track_id is None:
        return None
    return F1_2024_TRACK_NAMES.get(track_id) or F1_2024_FASTF1_EVENTS.get(track_id)


@dataclass(frozen=True, slots=True)
class CornerMarker:
    label: str
    distance_m: float
    normalized_distance: float


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    track_id: int
    event_name: str
    year: int
    corners: tuple[CornerMarker, ...]


class FastF1CornerMetadata:
    def __init__(
        self,
        year: int = 2024,
        session_name: str = "Q",
        event_map: dict[int, str] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.year = year
        self.session_name = session_name
        self.event_map = event_map or F1_2024_FASTF1_EVENTS
        self.cache_dir = cache_dir or Path(tempfile.gettempdir()) / "f1coach-fastf1-cache"
        self._metadata: dict[int, TrackMetadata | None] = {}
        self._loading: set[int] = set()
        self._errors: dict[int, str] = {}
        self._lock = threading.Lock()

    def begin_load(self, track_id: int | None, track_length_m: int | None) -> None:
        if track_id is None or track_length_m is None or track_length_m <= 0:
            return
        if track_id not in self.event_map:
            return
        with self._lock:
            if track_id in self._metadata or track_id in self._loading:
                return
            self._loading.add(track_id)
        thread = threading.Thread(
            target=self._load_safe,
            args=(track_id, track_length_m),
            name=f"fastf1-corners-{track_id}",
            daemon=True,
        )
        thread.start()

    def corner_label(
        self,
        track_id: int | None,
        normalized_distance: float,
        track_length_m: int | None,
        tolerance_pct: float = 4.0,
    ) -> str | None:
        if track_id is None or track_length_m is None or track_length_m <= 0:
            return None
        metadata = self.metadata(track_id)
        if metadata is None or not metadata.corners:
            return None

        best: tuple[float, CornerMarker] | None = None
        for corner in metadata.corners:
            diff_pct = abs(corner.normalized_distance - normalized_distance) * 100
            if best is None or diff_pct < best[0]:
                best = (diff_pct, corner)
        if best is None or best[0] > tolerance_pct:
            return None
        return best[1].label

    def metadata(self, track_id: int | None) -> TrackMetadata | None:
        if track_id is None:
            return None
        with self._lock:
            return self._metadata.get(track_id)

    def error(self, track_id: int | None) -> str | None:
        if track_id is None:
            return None
        with self._lock:
            return self._errors.get(track_id)

    def _load_safe(self, track_id: int, track_length_m: int) -> None:
        try:
            metadata = self._load(track_id, track_length_m)
        except Exception as exc:
            with self._lock:
                self._metadata[track_id] = None
                self._errors[track_id] = f"{type(exc).__name__}: {exc}"
                self._loading.discard(track_id)
            return
        with self._lock:
            self._metadata[track_id] = metadata
            self._loading.discard(track_id)

    def _load(self, track_id: int, track_length_m: int) -> TrackMetadata:
        event_name = self.event_map[track_id]
        fastf1 = self._import_fastf1()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fastf1.Cache.enable_cache(str(self.cache_dir))
        session = fastf1.get_session(self.year, event_name, self.session_name)
        session.load(laps=True, telemetry=True, weather=False, messages=False)
        circuit_info = session.get_circuit_info()
        if circuit_info is None:
            raise ValueError(f"No FastF1 circuit info for {event_name}")
        corners = self._corners_from_frame(circuit_info.corners, track_length_m)
        if not corners:
            raise ValueError(f"No FastF1 corner distances for {event_name}")
        return TrackMetadata(
            track_id=track_id,
            event_name=event_name,
            year=self.year,
            corners=tuple(corners),
        )

    @staticmethod
    def _import_fastf1() -> Any:
        import fastf1

        return fastf1

    @staticmethod
    def _corners_from_frame(frame: Any, track_length_m: int) -> list[CornerMarker]:
        corners: list[CornerMarker] = []
        for _, row in frame.iterrows():
            distance = float(row["Distance"])
            if math.isnan(distance):
                continue
            number = int(row["Number"])
            letter = str(row["Letter"] or "").strip()
            label = f"T{number}{letter}"
            normalized = max(0.0, min(0.999, distance / track_length_m))
            corners.append(
                CornerMarker(
                    label=label,
                    distance_m=distance,
                    normalized_distance=normalized,
                )
            )
        return sorted(corners, key=lambda corner: corner.normalized_distance)
