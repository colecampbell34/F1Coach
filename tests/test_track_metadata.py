from __future__ import annotations

import unittest

from f1coach.track_metadata import FastF1CornerMetadata, TrackMetadata, CornerMarker


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def iterrows(self):
        for index, row in enumerate(self.rows):
            yield index, row


class FastF1CornerMetadataTests(unittest.TestCase):
    def test_extracts_corner_markers_from_fastf1_frame(self) -> None:
        frame = FakeFrame(
            [
                {"Distance": 500.0, "Number": 1, "Letter": ""},
                {"Distance": 750.0, "Number": 2, "Letter": "A"},
            ]
        )

        corners = FastF1CornerMetadata._corners_from_frame(frame, 5000)

        self.assertEqual([corner.label for corner in corners], ["T1", "T2A"])
        self.assertEqual([corner.normalized_distance for corner in corners], [0.1, 0.15])

    def test_returns_nearest_corner_within_tolerance(self) -> None:
        metadata = FastF1CornerMetadata()
        metadata._metadata[3] = TrackMetadata(
            track_id=3,
            event_name="Bahrain Grand Prix",
            year=2024,
            corners=(
                CornerMarker("T1", 250.0, 0.05),
                CornerMarker("T4", 1250.0, 0.25),
            ),
        )

        self.assertEqual(metadata.corner_label(3, 0.252, 5000), "T4")
        self.assertIsNone(metadata.corner_label(3, 0.40, 5000))


if __name__ == "__main__":
    unittest.main()
