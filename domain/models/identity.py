"""国际服账号的复合身份。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class RegionUid:
    """以区服和 UID 共同标识一个国际服游戏账号。"""

    region_id: str
    uid: str

    def __post_init__(self) -> None:
        region_id = self.region_id.strip()
        uid = self.uid.strip()
        if not region_id:
            raise ValueError("区服标识不能为空")
        if not uid:
            raise ValueError("UID 不能为空")
        object.__setattr__(self, "region_id", region_id)
        object.__setattr__(self, "uid", uid)

    @property
    def cache_key(self) -> str:
        return f"{self.region_id}:{self.uid}"
