"""TTS 引擎统一接口。加新引擎就继承 BaseTTS，实现 synthesize。"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path


class TTSError(RuntimeError):
    pass


class BaseTTS:
    name = "base"
    ext = "mp3"

    def __init__(self, conf: dict | None = None):
        self.conf = conf or {}

    def is_ready(self) -> bool:
        return False

    def synthesize(self, text: str, out_dir: Path) -> Path | None:
        raise NotImplementedError

    def voice_tag(self) -> str:
        """缓存指纹。

        必须把音色、模型、语速也算进去，否则换音色之后同一句话
        会命中旧文件，听起来「换了跟没换一样」——之前就踩了这个坑。
        """
        c = self.conf or {}
        parts = [
            self.name,
            str(c.get("model") or ""),
            str(c.get("voice") or c.get("voice_type") or ""),
            str(c.get("cluster") or ""),
            str(c.get("speed") or ""),
        ]
        return "|".join(parts)

    def _cache_name(self, text: str, tag: str = "") -> str:
        h = hashlib.md5((tag + "|" + text).encode()).hexdigest()[:16]
        return f"tts_{h}.{self.ext}"

    def _out_path(self, text: str, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / self._cache_name(text, self.voice_tag())


class NullTTS(BaseTTS):
    """不出声，只走流程，用来看弹幕流正不正常。"""

    name = "none"

    def is_ready(self) -> bool:
        return True

    def synthesize(self, text: str, out_dir: Path) -> Path | None:
        return None
