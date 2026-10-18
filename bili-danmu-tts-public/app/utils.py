"""弹幕文本清洗 + 过滤。

B站弹幕里 [doge] 这种表情占位符会被 TTS 一个字母一个字母念出来，
所以必须干掉。还有一堆纯标点、纯数字、刷屏的，也别念。
"""

from __future__ import annotations

import re

_RE_EMOJI = re.compile(r"\[[^\[\]]{1,12}\]")
_RE_URL = re.compile(r"https?://\S+|www\.\S+")
_RE_AT = re.compile(r"@[\w\u4e00-\u9fa5\-]{1,20}")
_RE_SPACE = re.compile(r"\s{2,}")
_RE_MEANINGFUL = re.compile(r"[\u4e00-\u9fa5a-zA-Z]")
_RE_REPEAT = re.compile(r"(.)\1{4,}")


def clean_text(raw: str, max_chars: int = 60) -> str:
    s = raw or ""
    s = _RE_EMOJI.sub(" ", s)
    s = _RE_URL.sub(" ", s)
    s = _RE_AT.sub(" ", s)
    s = _RE_REPEAT.sub(r"\1\1\1", s)   # 哈哈哈哈哈 -> 哈哈哈
    s = _RE_SPACE.sub(" ", s).strip()

    # 只剩标点或数字的，不念
    if not _RE_MEANINGFUL.search(s):
        return ""
    if len(s) > max_chars:
        s = s[:max_chars] + "，后面的省略"
    return s


def should_mute(text: str, mute_keywords: list[str]) -> bool:
    low = text.lower()
    for kw in mute_keywords or []:
        if kw and kw.lower() in low:
            return True
    return False


def build_speech(raw: str, user: str, cfg) -> str:
    """把一条弹幕拼成要念的句子。"""
    max_chars = int(cfg.get("broadcast", "max_chars", default=60) or 60)
    body = clean_text(raw, max_chars)
    if not body:
        return ""
    if should_mute(body, cfg.get("broadcast", "mute_keywords", default=[])):
        return ""
    if cfg.get("broadcast", "read_username", default=True) and user:
        suffix = cfg.get("broadcast", "username_suffix", default="说，") or ""
        return f"{user}{suffix}{body}"
    return body


# 每种事件的开关配置项，以及默认值（进场太吵，默认关）
EVENT_SWITCH = {
    "danmaku": ("read_danmaku", True),
    "gift": ("read_gift", True),
    "guard": ("read_gift", True),
    "sc": ("read_sc", True),
    "follow": ("read_follow", True),
    "enter": ("read_enter", False),
}


def event_enabled(etype: str, cfg) -> bool:
    key, default = EVENT_SWITCH.get(etype, ("read_danmaku", True))
    return bool(cfg.get("broadcast", key, default=default))


def speech_for_event(ev: dict, cfg) -> str:
    """按事件类型拼播报文本。返回空串表示这条不播。"""
    etype = ev.get("type") or "danmaku"
    if not event_enabled(etype, cfg):
        return ""

    user = str(ev.get("user") or "").strip()
    max_chars = int(cfg.get("broadcast", "max_chars", default=60) or 60)

    if etype == "danmaku":
        return build_speech(ev.get("text", ""), user, cfg)

    if etype in ("gift", "guard"):
        gift = str(ev.get("gift") or ev.get("text") or "").strip()
        if not gift:
            return ""
        # 免费礼物量大，可以单独关掉
        if (
            etype == "gift"
            and not ev.get("paid")
            and cfg.get("broadcast", "ignore_free_gift", default=False)
        ):
            return ""
        num = int(ev.get("num") or 1)
        who = f"{user}老板" if user else "老板"
        if etype == "guard":
            return f"感谢{who}开通的{gift}"
        if num > 1:
            return f"感谢{who}赠送的{num}个{gift}"
        return f"感谢{who}赠送的{gift}"

    if etype == "sc":
        body = clean_text(ev.get("text", ""), max(20, max_chars))
        if not body:
            return ""
        if should_mute(body, cfg.get("broadcast", "mute_keywords", default=[])):
            return ""
        who = f"{user}老板" if user else "老板"
        price = ev.get("price")
        try:
            p = int(float(price or 0))
        except Exception:
            p = 0
        head = f"感谢{who}的{p}元SC" if p > 0 else f"感谢{who}的SC"
        return f"{head}，{body}"

    if etype == "follow":
        return f"感谢{user}的关注" if user else ""

    if etype == "enter":
        return f"欢迎{user}" if user else ""

    return ""

