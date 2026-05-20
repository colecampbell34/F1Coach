from __future__ import annotations

import unittest

from f1coach.coach import CompletedLap, LapCoach, LapSample, ReferenceProfile
from f1coach.models import CarStatusSnapshot, CarTelemetrySnapshot, LapSnapshot, PacketHeader, SessionInfo


class FakeCornerMetadata:
    def __init__(self) -> None:
        self.loaded: list[tuple[int | None, int | None]] = []

    def begin_load(self, track_id: int | None, track_length_m: int | None) -> None:
        self.loaded.append((track_id, track_length_m))

    def corner_label(
        self,
        track_id: int | None,
        normalized_distance: float,
        track_length_m: int | None,
    ) -> str | None:
        if track_id == 3 and track_length_m == 5000 and 0.28 <= normalized_distance <= 0.34:
            return "T10"
        return None

    def metadata(self, track_id: int | None) -> None:
        return None

    def error(self, track_id: int | None) -> None:
        return None


class LapCoachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.header = PacketHeader(2024, 24, 1, 18, 1, 2, 1, 0.0, 1, 1, 0, 255)

    def test_sets_first_clean_lap_as_reference(self) -> None:
        coach = LapCoach(sample_buckets=10)
        notices: list[str] = []
        notices.extend(coach.update(SessionInfo(self.header, 5000, 0, 10, 3, 0, 22, 30)))
        notices.extend(coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0)))
        notices.extend(coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000)))
        notices.extend(coach.update(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10)))

        self.assertTrue(any("Set as first personal best" in notice for notice in notices))
        self.assertIsNotNone(coach.best_lap)

    def test_ignores_invalid_reference_lap(self) -> None:
        coach = LapCoach(sample_buckets=10)
        notices: list[str] = []
        coach.update(SessionInfo(self.header, 5000, 0, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0))
        coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000, invalid=True))
        notices.extend(coach.update(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10)))

        self.assertTrue(any("invalid" in notice for notice in notices))
        self.assertIsNone(coach.best_lap)

    def test_practice_session_invalid_flags_do_not_exclude_lap(self) -> None:
        coach = LapCoach(sample_buckets=10)
        notices: list[str] = []
        coach.update(SessionInfo(self.header, 5000, 3, 1, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0))
        coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000, invalid=True))
        notices.extend(coach.update(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10, invalid=True)))

        self.assertIsNotNone(coach.best_lap)
        assert coach.best_lap is not None
        self.assertFalse(coach.best_lap.invalid)
        self.assertTrue(coach.best_lap.game_invalid)
        self.assertTrue(any("practice-program invalid flag ignored" in notice for notice in notices))

    def test_snapshot_uses_track_name(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 4940, 12, 10, 3, 0, 22, 30))

        state = coach.snapshot()

        self.assertEqual(state["session"]["trackName"], "Marina Bay Street Circuit")

    def test_live_sample_uses_latest_telemetry(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 3, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=120, throttle=0.2, brake=0.3))
        coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000))
        coach.update(self._telemetry(speed=240, throttle=0.9, brake=0.0))

        sample = coach.snapshot()["current"]["sample"]

        self.assertIsNotNone(sample)
        self.assertEqual(sample["speedKmh"], 240)
        self.assertEqual(sample["throttle"], 0.9)

    def test_practice_program_driver_statuses_build_live_sample(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 3, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=120, throttle=0.2, brake=0.3))

        coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000, driver_status=3))

        sample = coach.snapshot()["current"]["sample"]
        self.assertIsNotNone(sample)
        self.assertEqual(sample["speedKmh"], 120)

    def test_snapshot_exposes_raw_telemetry_without_lap_sample(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 3, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=121, throttle=0.25, brake=0.0))
        coach.update(self._lap(lap_num=1, current_ms=0, last_ms=0, distance=0, driver_status=3))

        current = coach.snapshot()["current"]

        self.assertIsNone(current["sample"])
        self.assertEqual(current["telemetry"]["speedKmh"], 121)
        self.assertEqual(current["telemetry"]["throttle"], 0.25)

    def test_track_map_uses_static_best_lap_not_active_trace(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.active_samples = [self._sample(10_000, 0.20, 180, 0.5, 0.0)]

        self.assertEqual(coach.snapshot()["trackMap"]["samples"], [])

        coach.best_lap = self._completed_lap(1, 90_000, slow_second_half=False)
        state = coach.snapshot()

        self.assertEqual(state["trackMap"]["source"], "Best lap 1")
        self.assertGreater(len(state["trackMap"]["samples"]), 0)

    def test_completed_lap_snapshot_includes_review_metrics_and_samples(self) -> None:
        coach = LapCoach(sample_buckets=10)
        lap = CompletedLap(
            4,
            91_500,
            30_000,
            31_000,
            False,
            [
                self._sample(10_000, 0.10, 180, 0.5, 0.0, ers_j=20_000),
                self._sample(20_000, 0.30, 220, 0.9, 0.0, ers_j=185_000),
            ],
        )
        coach.completed_laps = [lap]
        coach.best_lap = lap
        coach.clean_laps = [CompletedLap(2, 90_000, 29_000, 31_000, False, [])]

        summary = coach.snapshot()["completedLaps"][0]

        self.assertEqual(summary["deltaToReferenceMs"], 1_500)
        self.assertEqual(summary["ersUsedKj"], 185)
        self.assertEqual(len(summary["samples"]), 2)
        self.assertEqual(summary["overview"]["topSpeedKmh"], 220)
        self.assertEqual(summary["overview"]["sampleCount"], 2)

    def test_completed_lap_keeps_dense_brake_samples_for_trace(self) -> None:
        coach = LapCoach(sample_buckets=2)
        coach.active_lap_num = 1
        coach.active_samples = [
            self._sample(10_000, 0.100, 230, 0.90, 0.00),
            self._sample(10_050, 0.101, 220, 0.20, 0.92),
            self._sample(10_100, 0.102, 216, 0.05, 0.00),
            self._sample(70_000, 0.700, 280, 1.00, 0.00),
        ]

        lap = coach._complete_lap(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10))

        self.assertIsNotNone(lap)
        assert lap is not None
        self.assertIn(0.92, [sample.brake for sample in lap.samples])
        self.assertEqual(len(lap.samples), 4)

    def test_snapshot_includes_full_race_review_power_ranking(self) -> None:
        coach = LapCoach(sample_buckets=30)
        laps = [
            CompletedLap(1, 91_000, 30_000, 30_200, False, self._piecewise_samples([30_000, 30_200, 30_800])),
            CompletedLap(2, 90_200, 29_700, 30_000, False, self._piecewise_samples([29_700, 30_000, 30_500])),
            CompletedLap(3, 90_600, 29_900, 30_100, False, self._piecewise_samples([29_900, 30_100, 30_600])),
            CompletedLap(4, 92_500, 30_800, 30_900, True, self._piecewise_samples([30_800, 30_900, 30_800])),
            CompletedLap(5, 90_100, 29_600, 30_000, False, self._piecewise_samples([29_600, 30_000, 30_500])),
        ]
        for lap, position, fuel_kg in zip(laps, [8, 7, 6, 6, 5], [34.0, 32.6, 31.3, 30.0, 28.8]):
            lap.race_position = position
            lap.fuel_kg = fuel_kg
        coach.completed_laps = laps
        coach.clean_laps = [lap for lap in laps if not lap.invalid]
        coach.best_lap = laps[-1]

        review = coach.snapshot()["raceReview"]

        self.assertEqual(review["summary"]["totalLaps"], 5)
        self.assertEqual(review["summary"]["cleanLaps"], 4)
        self.assertEqual(review["summary"]["scoredLaps"], 3)
        self.assertEqual(review["summary"]["invalidLaps"], 1)
        self.assertEqual(review["summary"]["bestLap"]["lapNum"], 5)
        self.assertEqual(len(review["lapTable"]), 5)
        self.assertGreaterEqual(review["powerRanking"]["score"], 0)
        self.assertLessEqual(review["powerRanking"]["score"], 10)
        self.assertIn("Pace", {factor["name"] for factor in review["factors"]})
        self.assertTrue(review["phaseBreakdown"])
        self.assertEqual([sector["sector"] for sector in review["sectorTrend"]], ["S1", "S2", "S3"])
        self.assertEqual([point["position"] for point in review["trends"]["position"]], [8, 7, 6, 6, 5])
        self.assertTrue(review["trends"]["pace"])
        self.assertTrue(any(stat["label"] == "Positions" for stat in review["funStats"]))
        self.assertTrue(any(stat["label"] == "Fuel Burn" for stat in review["funStats"]))

    def test_power_ranking_excludes_neutralized_and_non_representative_laps(self) -> None:
        coach = LapCoach(sample_buckets=30)
        laps = [
            CompletedLap(1, 122_000, 40_000, 41_000, False, self._piecewise_samples([40_000, 41_000, 41_000])),
            CompletedLap(2, 90_000, 30_000, 30_000, False, self._piecewise_samples([30_000, 30_000, 30_000])),
            CompletedLap(
                3,
                155_000,
                52_000,
                52_000,
                False,
                self._piecewise_samples([52_000, 52_000, 51_000]),
                pit_statuses=(1,),
            ),
            CompletedLap(
                4,
                142_000,
                47_000,
                47_000,
                False,
                self._piecewise_samples([47_000, 47_000, 48_000]),
                safety_car_statuses=(1,),
            ),
            CompletedLap(
                5,
                91_000,
                30_200,
                30_300,
                False,
                self._piecewise_samples([30_200, 30_300, 30_500]),
                fia_flag_statuses=(3,),
            ),
            CompletedLap(6, 90_400, 30_100, 30_100, False, self._piecewise_samples([30_100, 30_100, 30_200])),
            CompletedLap(7, 90_300, 30_100, 30_100, False, self._piecewise_samples([30_100, 30_100, 30_100])),
            CompletedLap(8, 111_000, 37_000, 37_000, False, self._piecewise_samples([37_000, 37_000, 37_000])),
            CompletedLap(9, 89_900, 29_900, 30_000, True, self._piecewise_samples([29_900, 30_000, 30_000])),
            CompletedLap(10, 90_100, 30_000, 30_000, False, []),
        ]
        coach.completed_laps = laps
        coach.clean_laps = [lap for lap in laps if not lap.invalid]
        coach.best_lap = laps[1]

        review = coach.snapshot()["raceReview"]
        table = {row["lapNum"]: row for row in review["lapTable"]}

        self.assertEqual(review["summary"]["scoredLaps"], 3)
        self.assertEqual(review["summary"]["bestLap"]["lapNum"], 2)
        self.assertEqual(review["summary"]["averageLapTimeMs"], 90_233)
        self.assertFalse(table[1]["rankingEligible"])
        self.assertIn("lap 1", table[1]["exclusionReason"])
        self.assertFalse(table[3]["rankingEligible"])
        self.assertIn("pit lane", table[3]["exclusionReason"])
        self.assertFalse(table[4]["rankingEligible"])
        self.assertIn("safety car", table[4]["exclusionReason"])
        self.assertFalse(table[5]["rankingEligible"])
        self.assertIn("non-green", table[5]["exclusionReason"])
        self.assertFalse(table[8]["rankingEligible"])
        self.assertIn("non-representative slow", table[8]["exclusionReason"])
        self.assertFalse(table[9]["rankingEligible"])
        self.assertIn("invalid", table[9]["exclusionReason"])
        self.assertFalse(table[10]["rankingEligible"])
        self.assertIn("no telemetry", table[10]["exclusionReason"])
        self.assertTrue(table[2]["rankingEligible"])
        self.assertTrue(table[6]["rankingEligible"])
        self.assertTrue(table[7]["rankingEligible"])

    def test_lap_overview_flags_input_overlap(self) -> None:
        coach = LapCoach(sample_buckets=10)
        lap = CompletedLap(
            8,
            91_500,
            30_000,
            31_000,
            False,
            [
                self._sample(10_000, 0.10, 180, 0.40, 0.25),
                self._sample(20_000, 0.30, 220, 0.90, 0.00),
                self._sample(30_000, 0.50, 160, 0.20, 0.35),
                self._sample(40_000, 0.70, 250, 1.00, 0.00),
            ],
        )

        summary = coach._lap_summary(lap)

        self.assertEqual(summary["overview"]["brakeThrottleOverlapPct"], 50.0)
        self.assertLess(summary["overview"]["controlScore"], 70)
        self.assertTrue(any("overlap" in note for note in summary["overviewNotes"]))

    def test_uses_active_lap_sector_times_when_lap_rolls_over(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 0, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0))
        coach.update(self._lap(lap_num=1, current_ms=50_000, last_ms=0, distance=3000, s1=28_111, s2=30_222))
        coach.update(self._lap(lap_num=2, current_ms=100, last_ms=89_500, distance=10, s1=0, s2=0))

        self.assertIsNotNone(coach.best_lap)
        assert coach.best_lap is not None
        self.assertEqual(coach.best_lap.sector1_time_ms, 28_111)
        self.assertEqual(coach.best_lap.sector2_time_ms, 30_222)

    def test_completed_laps_use_internal_counter_when_game_lap_number_resets(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 0, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0))

        coach.update(self._lap(lap_num=1, current_ms=50_000, last_ms=0, distance=3000))
        coach.update(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10))
        coach.update(self._lap(lap_num=1, current_ms=100, last_ms=0, distance=10))
        coach.update(self._lap(lap_num=2, current_ms=100, last_ms=91_000, distance=10))

        self.assertEqual([lap.lap_num for lap in coach.completed_laps], [1, 2])

    def test_track_change_starts_new_session(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 1, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0))
        coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000))
        coach.update(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10))

        notices = coach.update(SessionInfo(self.header, 4300, 2, 10, 3, 0, 22, 30))

        self.assertTrue(any("New track detected" in notice for notice in notices))
        self.assertEqual(coach.track_id, 2)
        self.assertIsNone(coach.best_lap)
        self.assertEqual(coach.clean_laps, [])

    def test_manual_new_session_clears_laps_and_reference(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 1, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0))
        coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000))
        coach.update(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10))

        coach.start_new_session("Manual new session started.")

        self.assertIsNone(coach.best_lap)
        self.assertIsNone(coach.reference_profile())
        self.assertEqual(coach.completed_laps, [])

    def test_completed_laps_calculate_theoretical_best_from_best_sectors(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap_a = CompletedLap(1, 90_000, 30_000, 31_000, False, [])
        lap_b = CompletedLap(2, 89_000, 29_000, 30_000, False, [])
        coach.clean_laps = [lap_a, lap_b]
        coach.best_lap = lap_b

        reference = coach.reference_profile()

        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference.source, "sector-theoretical")
        self.assertEqual(reference.lap_time_ms, 88_000)
        self.assertEqual(reference.sector1_time_ms, 29_000)
        self.assertEqual(reference.sector2_time_ms, 30_000)
        self.assertEqual(reference.sector3_time_ms, 29_000)

    def test_sector_theoretical_insights_use_same_time_target_as_map(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap = CompletedLap(
            3,
            96_000,
            30_000,
            36_000,
            False,
            self._piecewise_samples([30_000, 36_000, 30_000]),
        )
        coach.completed_laps = [lap]
        coach.best_lap = lap
        coach.clean_laps = [
            lap,
            CompletedLap(2, 90_000, 30_000, 30_000, False, self._piecewise_samples([30_000, 30_000, 30_000])),
        ]

        summary = coach.snapshot()["completedLaps"][0]

        self.assertEqual(summary["deltaToReferenceMs"], 6_000)
        self.assertTrue(summary["insights"])
        self.assertTrue(
            all(insight["reference_source"] == "reference segment from lap 2" for insight in summary["insights"])
        )
        self.assertFalse(
            any("theoretical sector pace" in insight["detail"] for insight in summary["insights"])
        )
        self.assertTrue(any(33 <= insight["start_pct"] <= 67 for insight in summary["insights"]))

    def test_sector_theoretical_lap_with_all_best_sectors_compares_to_itself(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap = CompletedLap(
            2,
            90_000,
            30_000,
            30_000,
            False,
            self._piecewise_samples([30_000, 30_000, 30_000]),
        )
        coach.completed_laps = [lap]
        coach.best_lap = lap
        coach.clean_laps = [lap]

        reference = coach.reference_profile()
        summary = coach.snapshot()["completedLaps"][0]

        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference.source, "sector-theoretical")
        self.assertEqual(reference.lap_time_ms, lap.lap_time_ms)
        self.assertEqual(summary["deltaToReferenceMs"], 0)
        self.assertEqual(summary["insights"], [])

    def test_completed_lap_records_fuel_and_tyre_status(self) -> None:
        coach = LapCoach(sample_buckets=10)
        coach.update(SessionInfo(self.header, 5000, 0, 10, 3, 0, 22, 30))
        coach.update(self._telemetry(speed=250, throttle=1.0, brake=0.0))
        coach.update(self._status(fuel_kg=13.4, fuel_laps=5.6, visual_tyre=16, actual_tyre=18))
        coach.update(self._lap(lap_num=1, current_ms=10_000, last_ms=0, distance=1000))
        coach.update(self._lap(lap_num=2, current_ms=100, last_ms=90_000, distance=10))

        self.assertIsNotNone(coach.best_lap)
        assert coach.best_lap is not None
        self.assertEqual(coach.best_lap.fuel_kg, 13.4)
        self.assertEqual(coach.best_lap.fuel_remaining_laps, 5.6)
        self.assertEqual(coach.best_lap.visual_tyre_compound, 16)
        summary = coach.snapshot()["completedLaps"][0]
        self.assertEqual(summary["fuelKg"], 13.4)
        self.assertEqual(summary["fuelRemainingLaps"], 5.6)
        self.assertEqual(summary["tyreCompound"], "Soft")
        self.assertEqual(summary["position"], 1)

    def test_identifies_ers_underuse_on_corner_exit(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap_samples = [
            self._sample(10_000, 0.30, 130, 0.65, 0.0, ers_j=100_000),
            self._sample(11_000, 0.32, 162, 0.95, 0.0, ers_j=130_000),
        ]
        ref_samples = [
            self._sample(10_000, 0.30, 142, 0.70, 0.0, ers_j=100_000),
            self._sample(10_650, 0.32, 178, 1.00, 0.0, ers_j=200_000),
        ]
        lap = CompletedLap(4, 80_000, 25_000, 27_000, False, lap_samples)
        reference = ReferenceProfile("Imported", "test", 79_000, 24_500, 26_800, ref_samples)

        insights = coach.analyze_lap(lap, reference)

        self.assertTrue(any(insight.category == "ERS deployment" for insight in insights))

    def test_ers_insight_stays_in_priority_stack(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap = CompletedLap(4, 80_000, 25_000, 27_000, False, [
            self._sample(1_000, 0.02, 180, 0.05, 0.70),
            self._sample(2_000, 0.04, 110, 0.10, 0.55),
            self._sample(10_000, 0.20, 130, 0.70, 0.0, ers_j=100_000),
            self._sample(10_500, 0.22, 165, 0.96, 0.0, ers_j=112_000),
            self._sample(30_000, 0.55, 190, 0.20, 0.60),
            self._sample(31_000, 0.57, 118, 0.10, 0.50),
            self._sample(50_000, 0.82, 170, 0.10, 0.62),
            self._sample(51_000, 0.84, 116, 0.10, 0.52),
        ])
        reference = ReferenceProfile("Imported", "test", 79_000, 24_500, 26_800, [
            self._sample(1_000, 0.02, 190, 0.05, 0.45),
            self._sample(1_450, 0.04, 126, 0.12, 0.30),
            self._sample(10_000, 0.20, 138, 0.70, 0.0, ers_j=100_000),
            self._sample(10_450, 0.22, 176, 1.00, 0.0, ers_j=130_000),
            self._sample(30_000, 0.55, 201, 0.18, 0.40),
            self._sample(30_450, 0.57, 132, 0.12, 0.26),
            self._sample(50_000, 0.82, 184, 0.10, 0.42),
            self._sample(50_450, 0.84, 130, 0.12, 0.28),
        ])

        insights = coach.analyze_lap(lap, reference)

        self.assertEqual(len(insights), 3)
        self.assertTrue(any(insight.category == "ERS deployment" for insight in insights))

    def test_reports_setup_trend_for_repeated_traction_loss(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap_samples = [
            self._sample(10_000, 0.10, 90, 0.75, 0.0, slip=0.32),
            self._sample(11_000, 0.13, 120, 0.90, 0.0, slip=0.34),
            self._sample(20_000, 0.50, 95, 0.76, 0.0, slip=0.31),
            self._sample(21_000, 0.53, 126, 0.92, 0.0, slip=0.33),
        ]
        ref_samples = [
            self._sample(10_000, 0.10, 98, 0.70, 0.0, slip=0.12),
            self._sample(10_700, 0.13, 136, 0.92, 0.0, slip=0.14),
            self._sample(20_000, 0.50, 102, 0.72, 0.0, slip=0.11),
            self._sample(20_700, 0.53, 140, 0.94, 0.0, slip=0.12),
        ]
        lap = CompletedLap(5, 80_000, 25_000, 27_000, False, lap_samples)
        reference = ReferenceProfile("Imported", "test", 79_000, 24_500, 26_800, ref_samples)
        coach.latest_insights = [
            coach._classify_segment(0.10, 0.13, 300, lap_samples[:2], ref_samples[:2]),
            coach._classify_segment(0.50, 0.53, 280, lap_samples[2:], ref_samples[2:]),
        ]

        suggestions = coach._setup_suggestions(lap, reference)

        self.assertTrue(any("exit slip" in suggestion for suggestion in suggestions))

    def test_setup_signals_include_baseline_when_no_pattern_is_detected(self) -> None:
        coach = LapCoach(sample_buckets=30)
        samples = [
            self._sample(10_000, 0.10, 180, 0.55, 0.0),
            self._sample(20_000, 0.30, 220, 0.90, 0.0),
            self._sample(30_000, 0.60, 250, 1.00, 0.0),
        ]
        lap = CompletedLap(4, 90_000, 30_000, 30_000, False, samples)
        reference = ReferenceProfile("Personal best", "test", 90_000, 30_000, 30_000, samples)
        coach.latest_insights = []

        suggestions = coach._setup_suggestions(lap, reference)

        self.assertTrue(suggestions)
        self.assertTrue(any("no clear setup change" in suggestion for suggestion in suggestions))

    def test_dynamic_context_adds_corner_specific_advice(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap_samples = [
            self._sample(10_000, 0.30, 180, 0.05, 0.72),
            self._sample(11_050, 0.32, 95, 0.10, 0.58),
        ]
        ref_samples = [
            self._sample(10_000, 0.30, 190, 0.05, 0.52),
            self._sample(10_550, 0.32, 112, 0.12, 0.35),
        ]
        lap = CompletedLap(6, 80_000, 25_000, 27_000, False, lap_samples)
        reference = ReferenceProfile("Imported", "test", 79_000, 24_500, 26_800, ref_samples)

        insights = coach.analyze_lap(lap, reference)

        self.assertTrue(any("Corner 1" in insight.area for insight in insights))
        self.assertFalse(any("Bahrain" in insight.area for insight in insights))
        self.assertTrue(any("Brake 5-10 m earlier into Corner 1" in insight.recommendation for insight in insights))

    def test_snapshot_includes_lap_specific_insights(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap_samples = [
            self._sample(10_000, 0.30, 180, 0.05, 0.72),
            self._sample(11_050, 0.32, 95, 0.10, 0.58),
        ]
        ref_samples = [
            self._sample(10_000, 0.30, 190, 0.05, 0.52),
            self._sample(10_550, 0.32, 112, 0.12, 0.35),
        ]
        lap = CompletedLap(6, 80_000, 25_000, 27_000, False, lap_samples)
        coach.completed_laps = [lap]
        coach.external_reference = ReferenceProfile("Imported", "test", 79_000, 24_500, 26_800, ref_samples)

        state = coach.snapshot()

        insights = state["completedLaps"][0]["insights"]
        self.assertTrue(insights)
        self.assertIn("Corner 1", insights[0]["area"])
        self.assertNotIn("Compare the speed trace", insights[0]["recommendation"])

    def test_fastf1_metadata_can_enrich_dynamic_corner_labels(self) -> None:
        metadata = FakeCornerMetadata()
        coach = LapCoach(sample_buckets=30, corner_metadata=metadata)
        coach.update(SessionInfo(self.header, 5000, 3, 10, 3, 0, 22, 30))
        lap_samples = [
            self._sample(10_000, 0.30, 180, 0.05, 0.72),
            self._sample(11_050, 0.32, 95, 0.10, 0.58),
        ]
        ref_samples = [
            self._sample(10_000, 0.30, 190, 0.05, 0.52),
            self._sample(10_550, 0.32, 112, 0.12, 0.35),
        ]
        lap = CompletedLap(6, 80_000, 25_000, 27_000, False, lap_samples)
        reference = ReferenceProfile("Imported", "test", 79_000, 24_500, 26_800, ref_samples)

        insights = coach.analyze_lap(lap, reference)

        self.assertIn((3, 5000), metadata.loaded)
        self.assertTrue(any("T10 entry" in insight.area for insight in insights))

    def test_race_goal_changes_track_specific_advice(self) -> None:
        coach = LapCoach(sample_buckets=30, driving_goal="race")
        lap_samples = [
            self._sample(10_000, 0.36, 205, 0.05, 0.70),
            self._sample(10_900, 0.40, 112, 0.08, 0.55),
        ]
        ref_samples = [
            self._sample(10_000, 0.36, 210, 0.05, 0.48),
            self._sample(10_450, 0.40, 128, 0.10, 0.34),
        ]
        lap = CompletedLap(7, 80_000, 25_000, 27_000, False, lap_samples)
        reference = ReferenceProfile("Imported", "test", 79_000, 24_500, 26_800, ref_samples)

        insights = coach.analyze_lap(lap, reference)

        self.assertTrue(any("Corner 1" in insight.area for insight in insights))
        self.assertFalse(any("Austria" in insight.area for insight in insights))
        self.assertTrue(any("Brake earlier and cleaner into Corner 1" in insight.recommendation for insight in insights))

    def _telemetry(self, speed: int, throttle: float, brake: float) -> CarTelemetrySnapshot:
        return CarTelemetrySnapshot(
            header=self.header,
            car_index=0,
            speed_kmh=speed,
            throttle=throttle,
            steer=0.0,
            brake=brake,
            clutch=0,
            gear=6,
            engine_rpm=11_500,
            drs=False,
            rev_lights_percent=50,
            brake_temperatures_c=(400, 400, 400, 400),
            tyre_surface_temperatures_c=(90, 90, 90, 90),
            tyre_inner_temperatures_c=(95, 95, 95, 95),
            engine_temperature_c=105,
            tyre_pressures_psi=(23.1, 23.1, 23.1, 23.1),
            surface_types=(0, 0, 0, 0),
        )

    def _status(
        self,
        fuel_kg: float,
        fuel_laps: float,
        visual_tyre: int,
        actual_tyre: int,
    ) -> CarStatusSnapshot:
        return CarStatusSnapshot(
            header=self.header,
            car_index=0,
            fuel_in_tank_kg=fuel_kg,
            fuel_capacity_kg=110.0,
            fuel_remaining_laps=fuel_laps,
            max_rpm=13_000,
            idle_rpm=4_000,
            max_gears=8,
            drs_allowed=True,
            actual_tyre_compound=actual_tyre,
            visual_tyre_compound=visual_tyre,
            tyres_age_laps=2,
            vehicle_fia_flags=0,
            ers_store_energy_j=3_000_000,
            ers_deploy_mode=2,
            ers_deployed_this_lap_j=50_000,
            network_paused=False,
        )

    def _lap(
        self,
        lap_num: int,
        current_ms: int,
        last_ms: int,
        distance: float,
        invalid: bool = False,
        s1: int = 30_000,
        s2: int = 30_000,
        driver_status: int = 1,
    ) -> LapSnapshot:
        return LapSnapshot(
            header=self.header,
            car_index=0,
            last_lap_time_ms=last_ms,
            current_lap_time_ms=current_ms,
            sector1_time_ms=s1,
            sector2_time_ms=s2,
            lap_distance_m=distance,
            total_distance_m=distance,
            car_position=1,
            current_lap_num=lap_num,
            sector=0,
            current_lap_invalid=invalid,
            pit_status=0,
            driver_status=driver_status,
            result_status=2,
            speed_trap_fastest_speed_kmh=0.0,
        )

    def _completed_lap(self, lap_num: int, lap_ms: int, slow_second_half: bool) -> CompletedLap:
        samples: list[LapSample] = []
        for index in range(31):
            distance = index / 30
            penalty = 3500 if slow_second_half and distance > 0.5 else 0
            sample_time = int(distance * lap_ms + penalty * distance)
            samples.append(
                LapSample(
                    lap_time_ms=sample_time,
                    lap_distance_m=distance * 5000,
                    normalized_distance=min(0.999, distance),
                    speed_kmh=280,
                    throttle=1.0,
                    brake=0.0,
                    steer=0.0,
                    gear=8,
                    engine_rpm=12_000,
                    ers_percent=None,
                    ers_deploy_mode=None,
                    ers_deployed_this_lap_j=None,
                    fuel_kg=None,
                    lateral_g=None,
                    longitudinal_g=None,
                    avg_slip_ratio=None,
                    avg_slip_angle=None,
                    world_position=(distance * 100, 0.0, distance * 50),
                )
            )
        return CompletedLap(lap_num, lap_ms, 30_000, 30_000, False, samples)

    def _sample(
        self,
        lap_time_ms: int,
        normalized_distance: float,
        speed_kmh: int,
        throttle: float,
        brake: float,
        steer: float = 0.0,
        slip: float | None = None,
        ers_j: float | None = None,
    ) -> LapSample:
        return LapSample(
            lap_time_ms=lap_time_ms,
            lap_distance_m=normalized_distance * 5000,
            normalized_distance=normalized_distance,
            speed_kmh=speed_kmh,
            throttle=throttle,
            brake=brake,
            steer=steer,
            gear=5,
            engine_rpm=10_500,
            ers_percent=None,
            ers_deploy_mode=None,
            ers_deployed_this_lap_j=ers_j,
            fuel_kg=None,
            lateral_g=None,
            longitudinal_g=None,
            avg_slip_ratio=slip,
            avg_slip_angle=None,
            world_position=(normalized_distance * 100, 0.0, normalized_distance * 50),
        )

    def _piecewise_samples(self, segment_times: list[int]) -> list[LapSample]:
        samples: list[LapSample] = []
        boundaries = [0.0, 1 / 3, 2 / 3, 0.999]
        cumulative = [0]
        for segment_time in segment_times:
            cumulative.append(cumulative[-1] + segment_time)
        for index in range(61):
            distance = min(0.999, index / 60)
            if distance <= boundaries[1]:
                ratio = distance / boundaries[1] if boundaries[1] else 0
                lap_time_ms = int(cumulative[0] + ratio * segment_times[0])
            elif distance <= boundaries[2]:
                ratio = (distance - boundaries[1]) / (boundaries[2] - boundaries[1])
                lap_time_ms = int(cumulative[1] + ratio * segment_times[1])
            else:
                ratio = (distance - boundaries[2]) / (boundaries[3] - boundaries[2])
                lap_time_ms = int(cumulative[2] + ratio * segment_times[2])
            samples.append(self._sample(lap_time_ms, distance, 250, 1.0, 0.0))
        return samples


if __name__ == "__main__":
    unittest.main()
