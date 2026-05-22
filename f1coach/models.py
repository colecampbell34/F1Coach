from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class PacketId(IntEnum):
    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_SETUPS = 5
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    FINAL_CLASSIFICATION = 8
    LOBBY_INFO = 9
    CAR_DAMAGE = 10
    SESSION_HISTORY = 11
    TYRE_SETS = 12
    MOTION_EX = 13
    TIME_TRIAL = 14


@dataclass(frozen=True, slots=True)
class PacketHeader:
    packet_format: int
    game_year: int
    game_major_version: int
    game_minor_version: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time: float
    frame_identifier: int
    overall_frame_identifier: int
    player_car_index: int
    secondary_player_car_index: int


@dataclass(frozen=True, slots=True)
class SessionInfo:
    header: PacketHeader
    track_length_m: int
    track_id: int
    session_type: int
    total_laps: int
    weather: int
    air_temperature_c: int
    track_temperature_c: int
    safety_car_status: int = 0
    session_time_left_s: int | None = None


@dataclass(frozen=True, slots=True)
class LapSnapshot:
    header: PacketHeader
    car_index: int
    last_lap_time_ms: int
    current_lap_time_ms: int
    sector1_time_ms: int
    sector2_time_ms: int
    lap_distance_m: float
    total_distance_m: float
    car_position: int
    current_lap_num: int
    sector: int
    current_lap_invalid: bool
    pit_status: int
    driver_status: int
    result_status: int
    speed_trap_fastest_speed_kmh: float


@dataclass(frozen=True, slots=True)
class CarTelemetrySnapshot:
    header: PacketHeader
    car_index: int
    speed_kmh: int
    throttle: float
    steer: float
    brake: float
    clutch: int
    gear: int
    engine_rpm: int
    drs: bool
    rev_lights_percent: int
    brake_temperatures_c: tuple[int, int, int, int]
    tyre_surface_temperatures_c: tuple[int, int, int, int]
    tyre_inner_temperatures_c: tuple[int, int, int, int]
    engine_temperature_c: int
    tyre_pressures_psi: tuple[float, float, float, float]
    surface_types: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class CarStatusSnapshot:
    header: PacketHeader
    car_index: int
    fuel_in_tank_kg: float
    fuel_capacity_kg: float
    fuel_remaining_laps: float
    max_rpm: int
    idle_rpm: int
    max_gears: int
    drs_allowed: bool
    actual_tyre_compound: int
    visual_tyre_compound: int
    tyres_age_laps: int
    vehicle_fia_flags: int
    ers_store_energy_j: float
    ers_deploy_mode: int
    ers_deployed_this_lap_j: float
    network_paused: bool


@dataclass(frozen=True, slots=True)
class MotionSnapshot:
    header: PacketHeader
    car_index: int
    world_position: tuple[float, float, float]
    world_velocity: tuple[float, float, float]
    g_force_lateral: float
    g_force_longitudinal: float
    g_force_vertical: float
    yaw: float
    pitch: float
    roll: float


@dataclass(frozen=True, slots=True)
class MotionExSnapshot:
    header: PacketHeader
    wheel_slip_ratio: tuple[float, float, float, float]
    wheel_slip_angle: tuple[float, float, float, float]
    front_wheels_angle: float | None


TelemetryMessage = (
    SessionInfo
    | LapSnapshot
    | CarTelemetrySnapshot
    | CarStatusSnapshot
    | MotionSnapshot
    | MotionExSnapshot
)
