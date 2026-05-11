from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from f1coach.references import load_reference


class ReferenceTests(unittest.TestCase):
    def test_loads_imported_reference_json(self) -> None:
        payload = {
            "name": "Test Reference",
            "source": "unit-test",
            "lapTimeMs": 88_000,
            "assistProfile": {"input": "controller"},
            "samples": [
                {
                    "lapTimeMs": 1000,
                    "lapDistanceM": 50.0,
                    "normalizedDistance": 0.01,
                    "speedKmh": 250,
                    "throttle": 1.0,
                    "brake": 0.0,
                    "steer": 0.0,
                    "gear": 7,
                    "engineRpm": 11_000,
                    "worldPosition": [1.0, 2.0, 3.0],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            reference = load_reference(path)

        self.assertEqual(reference.name, "Test Reference")
        self.assertEqual(reference.lap_time_ms, 88_000)
        self.assertEqual(reference.assist_profile["input"], "controller")
        self.assertEqual(reference.samples[0].world_position, (1.0, 2.0, 3.0))


if __name__ == "__main__":
    unittest.main()
