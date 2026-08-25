"""国际服不同接口之间的大区标识等价关系。"""

_REGION_HASH_TO_NAME = {
    "86d52186155b148b5c138ceb41be9650": "Asia",
    "591d6af3a3090d8ea00d8f86cf6d7501": "America",
    "6eb2a235b30d05efd77bedb5cf60999e": "Europe",
    "919752ae5ea09c1ced910dd668a63ffb": "HMT",
    "10cd7254d57e58ae560b15d51e34b4c8": "SEA",
}
_REGION_NAME_CASE = {name.casefold(): name for name in _REGION_HASH_TO_NAME.values()}


def canonical_region(value: object) -> str:
    """将启动器名称和攻略站 serverId 归一为同一个稳定比较值。"""

    raw = str(value or "").strip()
    if not raw:
        return ""
    display = _REGION_HASH_TO_NAME.get(raw.casefold())
    if display is not None:
        return display.casefold()
    return raw.casefold()


def region_display_name(value: object) -> str:
    """返回已知国际服大区的稳定英文显示名，未知值保持原样。"""

    raw = str(value or "").strip()
    if not raw:
        return ""
    return _REGION_HASH_TO_NAME.get(raw.casefold()) or _REGION_NAME_CASE.get(raw.casefold(), raw)


def regions_equivalent(left: object, right: object) -> bool:
    """仅在两个非空标识明确属于同一国际服大区时返回真。"""

    left_region = canonical_region(left)
    return bool(left_region) and left_region == canonical_region(right)
