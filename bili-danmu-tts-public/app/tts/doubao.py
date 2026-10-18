"""豆包（火山引擎）语音合成。

接口：POST https://openspeech.bytedance.com/api/v1/tts
      Header: Authorization: Bearer;<access_token>   ← 注意是分号，不是空格
      返回 base64 音频

access_token 在火山引擎语音技术控制台拿，用「Bearer;」这种写法是官方要求，
写成空格会直接 401，踩过一次坑。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import requests

from .base import BaseTTS, TTSError

DEFAULT_EP = "https://openspeech.bytedance.com/api/v1/tts"


class DoubaoTTS(BaseTTS):
    name = "doubao"
    ext = "mp3"

    def is_ready(self) -> bool:
        return bool(self.conf.get("appid") and self.conf.get("access_token"))

    def synthesize(self, text: str, out_dir: Path) -> Path:
        appid = (self.conf.get("appid") or "").strip()
        token = (self.conf.get("access_token") or "").strip()
        if not appid or not token:
            raise TTSError("豆包没填 appid / access_token")

        out = self._out_path(text, out_dir)
        if out.exists() and out.stat().st_size > 512:
            return out

        payload = {
            "app": {
                "appid": appid,
                "token": token,
                "cluster": self.conf.get("cluster") or "volcano_tts",
            },
            "user": {"uid": "bili-danmu-tts"},
            "audio": {
                "voice_type": self.conf.get("voice_type") or "zh_female_vv_mars_bigtts",
                "encoding": "mp3",
                "speed_ratio": float(self.conf.get("speed") or 1.0),
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": "query",
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }

        try:
            r = requests.post(
                self.conf.get("endpoint") or DEFAULT_EP,
                json=payload,
                headers={
                    "Authorization": f"Bearer;{token}",
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
        except requests.RequestException as e:
            raise TTSError(f"豆包请求失败：{e}") from e

        if r.status_code != 200:
            raise TTSError(f"豆包 HTTP {r.status_code}：{r.text[:200]}")

        data = r.json()
        code = data.get("code")
        # 3000 = 成功
        if code != 3000:
            voice = payload["audio"]["voice_type"]
            msg = str(data.get("message") or "")
            # 3001 基本都是「音色版本」和「开通的服务版本」对不上，直接说清楚
            if code == 3001 and "not granted" in msg:
                hint = (
                    "音色和开通的服务版本不匹配。\n"
                    f"  当前填的是 {voice}\n"
                    "  开通「语音合成 2.0」→ 用 zh_female_vv_mars_bigtts 这类 _mars_bigtts 结尾的\n"
                    "  开通「语音合成 1.0」→ 用 zh_female_qingxin 这类老短名"
                )
                raise TTSError(f"豆包 3001：{msg}\n{hint}")
            raise TTSError(f"豆包返回 {code}：{msg}")

        raw = data.get("data")
        if not raw:
            raise TTSError("豆包没返回音频数据")
        out.write_bytes(base64.b64decode(raw))
        return out
