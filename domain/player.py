"""国际服玩家详情唯一快照。"""

from dataclasses import dataclass


class PlayerDataError(ValueError):
    """表示玩家详情不可查询且没有可用缓存。"""


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    uid: str
    region_id: str
    region_name: str
    player_name: str | None
    head_photo: int | None
    level: int | None
    world_level: int | None
    role_num: int | None
    active_days: int | None
    created_at_ms: int | None
    energy: int | None
    max_energy: int | None
    store_energy: int | None
    max_store_energy: int | None
    energy_recover_time_ms: int | None
    store_energy_recover_time_ms: int | None
    liveness: int | None
    liveness_max: int | None
    liveness_unlock: bool | None
    weekly_inst_count: int | None
    battle_pass_present: bool
    battle_pass_level: int | None
    battle_pass_week_exp: int | None
    battle_pass_week_max_exp: int | None
    battle_pass_is_unlock: bool | None
    battle_pass_is_open: bool | None
    battle_pass_exp: int | None
    battle_pass_exp_limit: int | None
    sound_box: int | None
    boxes: tuple[tuple[str, int], ...] | None
    basic_boxes: tuple[tuple[str, int], ...] | None
    phantom_boxes: tuple[tuple[str, int], ...] | None
    refreshed_at: str
    is_cached_fallback: bool = False
