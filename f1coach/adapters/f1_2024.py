from __future__ import annotations

import struct
from typing import ClassVar

from f1coach.adapters.base import UnsupportedPacket
from f1coach.models import (
    CarStatusSnapshot,
    CarTelemetrySnapshot,
    LapSnapshot,
    MotionExSnapshot,
    MotionSnapshot,
    PacketHeader,
    PacketId,
    SessionInfo,
    TelemetryMessage,
)


class F124Adapter:
    """Decoder for F1 24 UDP Format 2024 packets.

    The adapter deliberately normalizes only the fields the first coaching
    engine uses. Other fields can be added here without changing the coach.
    """

    name = "f1-24"

    HEADER_FORMAT: ClassVar[str] = "<HBBBBBQfIIBB"
    HEADER_SIZE: ClassVar[int] = struct.calcsize(HEADER_FORMAT)

    CAR_MOTION_FORMAT: ClassVar[str] = "<ffffffhhhhhhffffff"
    CAR_MOTION_SIZE: ClassVar[int] = struct.calcsize(CAR_MOTION_FORMAT)

    LAP_DATA_FORMAT: ClassVar[str] = "<IIHBHBHBHBfffBBBBBBBBBBBBBBBHHBfB"
    LAP_DATA_SIZE: ClassVar[int] = struct.calcsize(LAP_DATA_FORMAT)

    CAR_TELEMETRY_FORMAT: ClassVar[str] = "<HfffBbHBBH4H4B4BH4f4B"
    CAR_TELEMETRY_SIZE: ClassVar[int] = struct.calcsize(CAR_TELEMETRY_FORMAT)

    CAR_STATUS_FORMAT: ClassVar[str] = "<BBBBBfffHHBBHBBBbfffBfffB"
    CAR_STATUS_SIZE: ClassVar[int] = struct.calcsize(CAR_STATUS_FORMAT)

    SESSION_PREFIX_FORMAT: ClassVar[str] = "<BbbBHBbBHH"
    SESSION_PREFIX_SIZE: ClassVar[int] = struct.calcsize(SESSION_PREFIX_FORMAT)
    SESSION_SAFETY_CAR_OFFSET: ClassVar[int] = SESSION_PREFIX_SIZE + 6 + (21 * 5)

    MOTION_EX_FORMAT: ClassVar[str] = "<52f"
    MOTION_EX_SIZE: ClassVar[int] = struct.calcsize(MOTION_EX_FORMAT)

    def decode(self, packet: bytes) -> TelemetryMessage | None:
        if len(packet) < self.HEADER_SIZE:
            return None

        header = self.decode_header(packet)
        if header.packet_format != 2024 or header.game_year != 24:
            raise UnsupportedPacket(
                f"Expected F1 24 format 2024, got format={header.packet_format} year={header.game_year}"
            )

        packet_id = PacketId(header.packet_id)
        if packet_id == PacketId.SESSION:
            return self._decode_session(packet, header)
        if packet_id == PacketId.LAP_DATA:
            return self._decode_lap_data(packet, header)
        if packet_id == PacketId.CAR_TELEMETRY:
            return self._decode_car_telemetry(packet, header)
        if packet_id == PacketId.CAR_STATUS:
            return self._decode_car_status(packet, header)
        if packet_id == PacketId.MOTION:
            return self._decode_motion(packet, header)
        if packet_id == PacketId.MOTION_EX:
            return self._decode_motion_ex(packet, header)
        return None

    def decode_header(self, packet: bytes) -> PacketHeader:
        values = struct.unpack_from(self.HEADER_FORMAT, packet, 0)
        return PacketHeader(*values)

    def _player_index(self, header: PacketHeader) -> int:
        if not 0 <= header.player_car_index < 22:
            raise UnsupportedPacket(f"Invalid player car index: {header.player_car_index}")
        return header.player_car_index

    def _decode_session(self, packet: bytes, header: PacketHeader) -> SessionInfo:
        self._require_size(packet, self.HEADER_SIZE + self.SESSION_PREFIX_SIZE)
        values = struct.unpack_from(self.SESSION_PREFIX_FORMAT, packet, self.HEADER_SIZE)
        (
            weather,
            track_temperature_c,
            air_temperature_c,
            total_laps,
            track_length_m,
            session_type,
            track_id,
            _formula,
            _session_time_left,
            _session_duration,
        ) = values
        safety_car_status = 0
        safety_car_offset = self.HEADER_SIZE + self.SESSION_SAFETY_CAR_OFFSET
        if len(packet) > safety_car_offset:
            safety_car_status = struct.unpack_from("<B", packet, safety_car_offset)[0]
        return SessionInfo(
            header=header,
            track_length_m=track_length_m,
            track_id=track_id,
            session_type=session_type,
            total_laps=total_laps,
            weather=weather,
            air_temperature_c=air_temperature_c,
            track_temperature_c=track_temperature_c,
            safety_car_status=safety_car_status,
        )

    def _decode_lap_data(self, packet: bytes, header: PacketHeader) -> LapSnapshot:
        car_index = self._player_index(header)
        offset = self.HEADER_SIZE + car_index * self.LAP_DATA_SIZE
        self._require_size(packet, offset + self.LAP_DATA_SIZE)
        values = struct.unpack_from(self.LAP_DATA_FORMAT, packet, offset)
        return LapSnapshot(
            header=header,
            car_index=car_index,
            last_lap_time_ms=values[0],
            current_lap_time_ms=values[1],
            sector1_time_ms=self._sector_time_ms(values[2], values[3]),
            sector2_time_ms=self._sector_time_ms(values[4], values[5]),
            lap_distance_m=values[10],
            total_distance_m=values[11],
            car_position=values[13],
            current_lap_num=values[14],
            pit_status=values[15],
            sector=values[17],
            current_lap_invalid=bool(values[18]),
            driver_status=values[26],
            result_status=values[27],
            speed_trap_fastest_speed_kmh=values[32],
        )

    def _decode_car_telemetry(self, packet: bytes, header: PacketHeader) -> CarTelemetrySnapshot:
        car_index = self._player_index(header)
        offset = self.HEADER_SIZE + car_index * self.CAR_TELEMETRY_SIZE
        self._require_size(packet, offset + self.CAR_TELEMETRY_SIZE)
        values = struct.unpack_from(self.CAR_TELEMETRY_FORMAT, packet, offset)
        return CarTelemetrySnapshot(
            header=header,
            car_index=car_index,
            speed_kmh=values[0],
            throttle=values[1],
            steer=values[2],
            brake=values[3],
            clutch=values[4],
            gear=values[5],
            engine_rpm=values[6],
            drs=bool(values[7]),
            rev_lights_percent=values[8],
            brake_temperatures_c=tuple(values[10:14]),
            tyre_surface_temperatures_c=tuple(values[14:18]),
            tyre_inner_temperatures_c=tuple(values[18:22]),
            engine_temperature_c=values[22],
            tyre_pressures_psi=tuple(values[23:27]),
            surface_types=tuple(values[27:31]),
        )

    def _decode_car_status(self, packet: bytes, header: PacketHeader) -> CarStatusSnapshot:
        car_index = self._player_index(header)
        offset = self.HEADER_SIZE + car_index * self.CAR_STATUS_SIZE
        self._require_size(packet, offset + self.CAR_STATUS_SIZE)
        values = struct.unpack_from(self.CAR_STATUS_FORMAT, packet, offset)
        return CarStatusSnapshot(
            header=header,
            car_index=car_index,
            fuel_in_tank_kg=values[5],
            fuel_capacity_kg=values[6],
            fuel_remaining_laps=values[7],
            max_rpm=values[8],
            idle_rpm=values[9],
            max_gears=values[10],
            drs_allowed=bool(values[11]),
            actual_tyre_compound=values[13],
            visual_tyre_compound=values[14],
            tyres_age_laps=values[15],
            vehicle_fia_flags=values[16],
            ers_store_energy_j=values[19],
            ers_deploy_mode=values[20],
            ers_deployed_this_lap_j=values[23],
            network_paused=bool(values[24]),
        )

    def _decode_motion(self, packet: bytes, header: PacketHeader) -> MotionSnapshot:
        car_index = self._player_index(header)
        offset = self.HEADER_SIZE + car_index * self.CAR_MOTION_SIZE
        self._require_size(packet, offset + self.CAR_MOTION_SIZE)
        values = struct.unpack_from(self.CAR_MOTION_FORMAT, packet, offset)
        return MotionSnapshot(
            header=header,
            car_index=car_index,
            world_position=tuple(values[0:3]),
            world_velocity=tuple(values[3:6]),
            g_force_lateral=values[12],
            g_force_longitudinal=values[13],
            g_force_vertical=values[14],
            yaw=values[15],
            pitch=values[16],
            roll=values[17],
        )

    def _decode_motion_ex(self, packet: bytes, header: PacketHeader) -> MotionExSnapshot:
        self._require_size(packet, self.HEADER_SIZE + self.MOTION_EX_SIZE)
        values = struct.unpack_from(self.MOTION_EX_FORMAT, packet, self.HEADER_SIZE)
        front_wheels_angle = values[43] if len(values) > 43 else None
        return MotionExSnapshot(
            header=header,
            wheel_slip_ratio=tuple(values[16:20]),
            wheel_slip_angle=tuple(values[20:24]),
            front_wheels_angle=front_wheels_angle,
        )

    @staticmethod
    def _sector_time_ms(milliseconds_part: int, minute_part: int) -> int:
        return minute_part * 60_000 + milliseconds_part

    @staticmethod
    def _require_size(packet: bytes, minimum_size: int) -> None:
        if len(packet) < minimum_size:
            raise UnsupportedPacket(f"Packet too short: {len(packet)} bytes, expected at least {minimum_size}")
