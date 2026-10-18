"""阿里云百炼（DashScope）语音合成。

2026-10 之后百炼把 qwen-tts 系列全下线了（调用直接 404 Model not exist），
现在官方推荐的是 cosyvoice-v3-flash，走 WebSocket 流式协议。
协议自己撸容易出错，所以用官方 dashscope SDK，一次 call 拿回整段音频。

音色是 v3 系列的 long* 名字，跟以前 qwen-tts 的 Cherry 那套不是一回事。
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseTTS, TTSError

DEFAULT_MODEL = "cosyvoice-v3-flash"
DEFAULT_VOICE = "longanyang"

# 老配置自动迁移，免得用户还要手动改 config.json
_DEAD_MODELS = {"", "qwen-tts", "qwen-tts-latest", "qwen-tts-realtime-latest"}
_DEAD_VOICES = {"", "Cherry", "Serena", "Ethan", "Chelsie"}


class AliyunTTS(BaseTTS):
    name = "aliyun"
    ext = "mp3"

    def __init__(self, conf: dict | None = None):
        super().__init__(conf)
        model = (self.conf.get("model") or "").strip()
        voice = (self.conf.get("voice") or "").strip()
        self.model = model if model and model not in _DEAD_MODELS else DEFAULT_MODEL
        self.voice = voice if voice and voice not in _DEAD_VOICES else DEFAULT_VOICE

    def is_ready(self) -> bool:
        return bool(self.conf.get("api_key"))

    def synthesize(self, text: str, out_dir: Path) -> Path:
        api_key = (self.conf.get("api_key") or "").strip()
        if not api_key:
            raise TTSError("阿里云没填 api_key")

        out = self._out_path(text, out_dir)
        if out.exists() and out.stat().st_size > 512:
            return out

        try:
            import dashscope
            from dashscope.audio.tts_v2 import SpeechSynthesizer
        except ImportError as e:
            raise TTSError(
                "缺 dashscope 库，跑一下 install.bat，或者 pip install dashscope"
            ) from e

        dashscope.api_key = api_key
        speed = float(self.conf.get("speed") or 1.0)
        volume = int(
            max(0.0, min(1.0, float(self.conf.get("volume", 0.9) or 0.9))) * 100
        )

        try:
            synth = SpeechSynthesizer(
                model=self.model,
                voice=self.voice,
                speech_rate=speed,
                volume=volume,
            )
            audio = synth.call(text)
        except Exception as e:
            raise TTSError(f"阿里云合成失败：{e}") from e

        if not audio:
            raise TTSError(
                f"阿里云没返回音频（模型 {self.model}，音色 {self.voice}）。"
                "多半是音色名和模型对不上，去控制台音色列表确认一下。"
            )

        out.write_bytes(bytes(audio))
        return out
