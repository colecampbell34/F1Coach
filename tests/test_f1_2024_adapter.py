from __future__ import annotations

import struct
import unittest

from f1coach.adapters.f1_2024 import F124Adapter
from f1coach.models import CarTelemetrySnapshot, LapSnapshot, PacketId, SessionInfo


class F124AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = F124Adapter()

    def test_decodes_header(self) -> None:
        packet = self._header(PacketId.LAP_DATA)
        header = self.adapter.decode_header(packet)

        self.assertEqual(header.packet_format, 2024)
        self.assertEqual(header.game_year, 24)
        self.assertEqual(header.packet_id, PacketId.LAP_DATA)
        self.assertEqual(header.player_car_index, 0)

    def test_decodes_session_prefix(self) -> None:
        payload = struct.pack(self.adapter.SESSION_PREFIX_FORMAT, 1, 31, 24, 5, 5412, 10, 7, 0, 600, 900)
        packet = self._header(PacketId.SESSION) + payload

        message = self.adapter.decode(packet)

        self.assertIsInstance(message, SessionInfo)
        assert isinstance(message, SessionInfo)
        self.assertEqual(message.track_length_m, 5412)
        self.assertEqual(message.track_id, 7)
        self.assertEqual(message.air_temperature_c, 24)
        self.assertEqual(message.session_time_left_s, 600)

    def test_decodes_session_safety_car_status_when_available(self) -> None:
        payload = (
            struct.pack(self.adapter.SESSION_PREFIX_FORMAT, 1, 31, 24, 5, 5412, 10, 7, 0, 600, 900)
            + (b"\x00" * 6)
            + (b"\x00" * 21 * 5)
            + b"\x02"
        )
        packet = self._header(PacketId.SESSION) + payload

        message = self.adapter.decode(packet)

        self.assertIsInstance(message, SessionInfo)
        assert isinstance(message, SessionInfo)
        self.assertEqual(message.safety_car_status, 2)

    def test_decodes_player_lap_data(self) -> None:
        car = struct.pack(
            self.adapter.LAP_DATA_FORMAT,
            91_234,
            12_345,
            29_001,
            0,
            30_100,
            0,
            0,
            0,
            0,
            0,
            1234.5,
            1234.5,
            0.0,
            4,
            2,
            0,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
            4,
            2,
            0,
            0,
            0,
            0,
            315.2,
            3,
        )
        packet = self._header(PacketId.LAP_DATA) + car + (b"\x00" * self.adapter.LAP_DATA_SIZE * 21)

        message = self.adapter.decode(packet)

        self.assertIsInstance(message, LapSnapshot)
        assert isinstance(message, LapSnapshot)
        self.assertEqual(message.last_lap_time_ms, 91_234)
        self.assertEqual(message.current_lap_time_ms, 12_345)
        self.assertEqual(message.sector1_time_ms, 29_001)
        self.assertEqual(message.sector2_time_ms, 30_100)
        self.assertEqual(message.current_lap_num, 2)
        self.assertEqual(message.sector, 1)
        self.assertFalse(message.current_lap_invalid)

    def test_decodes_player_car_telemetry(self) -> None:
        car = struct.pack(
            self.adapter.CAR_TELEMETRY_FORMAT,
            287,
            0.91,
            -0.12,
            0.04,
            0,
            7,
            11_900,
            1,
            84,
            0b1111,
            410,
            412,
            398,
            399,
            91,
            92,
            89,
            88,
            97,
            98,
            96,
            95,
            108,
            23.4,
            23.5,
            23.6,
            23.7,
            0,
            0,
            0,
            0,
        )
        packet = self._header(PacketId.CAR_TELEMETRY) + car + (b"\x00" * self.adapter.CAR_TELEMETRY_SIZE * 21)

        message = self.adapter.decode(packet)

        self.assertIsInstance(message, CarTelemetrySnapshot)
        assert isinstance(message, CarTelemetrySnapshot)
        self.assertEqual(message.speed_kmh, 287)
        self.assertAlmostEqual(message.throttle, 0.91, places=2)
        self.assertEqual(message.gear, 7)
        self.assertTrue(message.drs)
        self.assertEqual(message.tyre_surface_temperatures_c, (91, 92, 89, 88))

    def _header(self, packet_id: int) -> bytes:
        return struct.pack(
            self.adapter.HEADER_FORMAT,
            2024,
            24,
            1,
            18,
            1,
            int(packet_id),
            123456789,
            12.34,
            99,
            99,
            0,
            255,
        )


if __name__ == "__main__":
    unittest.main()
