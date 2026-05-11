from __future__ import annotations

import unittest

from f1coach.coach import CompletedLap, LapCoach, LapSample, ReferenceProfile
from f1coach.models import CarTelemetrySnapshot, LapSnapshot, PacketHeader, SessionInfo


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

    def test_builds_ideal_reference_from_clean_microsectors(self) -> None:
        coach = LapCoach(sample_buckets=30)
        lap_a = self._completed_lap(1, 90_000, slow_second_half=True)
        lap_b = self._completed_lap(2, 89_000, slow_second_half=False)
        coach.clean_laps = [lap_a, lap_b]

        coach._rebuild_ideal_reference()

        self.assertIsNotNone(coach.ideal_reference)
        assert coach.ideal_reference is not None
        self.assertEqual(coach.ideal_reference.name, "Ideal lap")
        self.assertLessEqual(coach.ideal_reference.lap_time_ms, 89_000)
        self.assertTrue(coach.ideal_reference.segments)
        self.assertTrue(all(segment.source_lap_num in {1, 2} for segment in coach.ideal_reference.segments))

    def test_theoretical_best_sums_fastest_loops_from_any_clean_lap(self) -> None:
        coach = LapCoach(sample_buckets=60, theoretical_loop_count=3)
        lap_a = CompletedLap(
            1,
            90_000,
            30_000,
            30_000,
            False,
            self._piecewise_samples([28_000, 32_000, 30_000]),
        )
        lap_b = CompletedLap(
            2,
            87_000,
            30_000,
            29_000,
            False,
            self._piecewise_samples([31_000, 27_000, 29_000]),
        )
        coach.clean_laps = [lap_a, lap_b]

        coach._rebuild_ideal_reference()

        self.assertIsNotNone(coach.ideal_reference)
        assert coach.ideal_reference is not None
        self.assertEqual(coach.ideal_reference.lap_time_ms, 84_000)
        self.assertEqual([segment.source_lap_num for segment in coach.ideal_reference.segments], [1, 2, 2])

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

        self.assertTrue(any("exit traction" in suggestion for suggestion in suggestions))

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

    def _lap(
        self,
        lap_num: int,
        current_ms: int,
        last_ms: int,
        distance: float,
        invalid: bool = False,
        s1: int = 30_000,
        s2: int = 30_000,
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
            driver_status=1,
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
