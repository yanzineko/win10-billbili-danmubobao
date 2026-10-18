"""配置读写。所有密钥都存在 config.json 里，放本地别提交。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG = {
    "room": {
        "room_id": "",          # 直播间号，短号长号都行
        "cookie": "",           # B站 cookie，风控时填 SESSDATA=buvid3=...
    },
    "tts": {
        "engine": "aliyun",     # aliyun | doubao | none
        # 阿里云百炼（DashScope）
        "aliyun": {
            "api_key": "",
            "model": "cosyvoice-v3-flash",
            "voice": "longanyang",
        },
        # 火山引擎豆包语音合成
        "doubao": {
            "appid": "",
            "access_token": "",
            "cluster": "volcano_tts",
            "voice_type": "zh_female_vv_mars_bigtts",  # 2.0 大模型音色，必须以 _mars_bigtts 结尾
            "endpoint": "https://openspeech.bytedance.com/api/v1/tts",
        },
        "speed": 1.0,           # 语速 0.5 ~ 2.0
        "volume": 0.9,          # 播放音量 0 ~ 1
    },
    "broadcast": {
        "read_danmaku": True,       # 播弹幕
        "read_gift": True,          # 播礼物（含上舰）
        "read_sc": True,            # 播醒目留言
        "read_follow": True,        # 播关注
        "read_enter": False,        # 播进场（人多时很吵，默认关）
        "ignore_free_gift": False,  # 只播付费礼物（辣条那种免费的不念）
        "enter_cooldown": 600,      # 同一个人的进场问候冷却（秒）
        "enter_per_min": 6,         # 每分钟最多播几条进场，防止大房间刷屏
        "follow_per_min": 10,       # 每分钟最多播几条关注
        "read_username": True,      # 念出昵称
        "username_suffix": "说，",  # 昵称后面接的词
        "max_chars": 60,            # 超长弹幕截断
        "max_queue": 8,             # 待播队列上限，超了就丢新的
        "min_interval": 0.25,       # 两条之间最短间隔（秒）
        "skip_emoji_only": True,    # 纯表情/纯符号 不播
        "mute_keywords": [],        # 命中这些词就不播
    },
    "log": {
        "enabled": True,
        "dir": "logs",
    },
    "ui": {
        "max_lines": 300,        # 界面上最多显示多少条
        "always_on_top": False,
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _deep_merge(DEFAULT_CONFIG, raw)
            except Exception:
                self.data = copy.deepcopy(DEFAULT_CONFIG)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, *keys, default=None):
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set(self, value, *keys):
        node = self.data
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value


def ensure_dirs(cfg: Config):
    d = cfg.get("log", "dir", default="logs")
    p = Path(d)
    if not p.is_absolute():
        p = ROOT / d
    p.mkdir(parents=True, exist_ok=True)
    return p
