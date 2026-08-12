"""版本号比较（点分数字段，忽略 rc/dev 等后缀）：Agent 与控制平面版本一致性提醒用。"""


def _segments(version: str) -> tuple[int, ...]:
    """拆出点分数字段（非数字段如 rc/dev 视作边界，忽略其后）。"""
    parts: list[int] = []
    for seg in version.strip().split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_compare(a: str, b: str) -> int:
    """a 与 b 比较：a<b→-1，a==b→0，a>b→1；无法解析时按字符串比较兜底。"""
    sa, sb = _segments(a), _segments(b)
    if not sa or not sb:
        return (a > b) - (a < b)
    return (sa > sb) - (sa < sb)
