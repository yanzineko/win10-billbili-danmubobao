from __future__ import annotations

from .aliyun import AliyunTTS
from .base import BaseTTS, NullTTS, TTSError
from .doubao import DoubaoTTS

__all__ = ["BaseTTS", "NullTTS", "TTSError", "AliyunTTS", "DoubaoTTS", "build_engine"]


def build_engine(cfg) -> BaseTTS:
    """按配置造一个引擎。cfg 是 Config 实例。"""
    engine = (cfg.get("tts", "engine", default="aliyun") or "aliyun").lower()
    speed = cfg.get("tts", "speed", default=1.0)
    volume = cfg.get("tts", "volume", default=0.9)

    if engine == "aliyun":
        conf = dict(cfg.get("tts", "aliyun", default={}) or {})
    elif engine == "doubao":
        conf = dict(cfg.get("tts", "doubao", default={}) or {})
    else:
        return NullTTS({})

    conf["speed"] = speed
    conf["volume"] = volume
    if engine == "aliyun":
        return AliyunTTS(conf)
    return DoubaoTTS(conf)
