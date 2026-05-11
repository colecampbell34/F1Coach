from __future__ import annotations

import unittest

from f1coach.coach import CompletedLap, LapCoach, LapSample
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
                    fuel_kg=None,
                    lateral_g=None,
                    longitudinal_g=None,
                    avg_slip_ratio=None,
                    avg_slip_angle=None,
                    world_position=(distance * 100, 0.0, distance * 50),
                )
            )
        return CompletedLap(lap_num, lap_ms, 30_000, 30_000, False, samples)


if __name__ == "__main__":
    unittest.main()
