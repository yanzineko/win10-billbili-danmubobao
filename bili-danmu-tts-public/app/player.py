"""音频播放 + 播报队列。

弹幕比嘴快，所以必须排队：一条播完再播下一条，绝不叠音。
队列满了丢新的（旧的已经念到一半了，丢了浪费）_。

播放用 Windows 自带的 MCI（winmm），它本身就支持 mp3，
不用额外装 pygame，省的打包和版本一堆破事。
"""

from __future__ import annotations

import ctypes
import itertools
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .config import ROOT, Config
from .tts import BaseTTS, TTSError

ALIAS = "danmutts"


class MciPlayer:
    """Windows 原生 MCI 播放器。"""

    def __init__(self):
        self.ok = False
        try:
            self.winmm = ctypes.WinDLL("winmm")
            self.ok = True
        except Exception:
            self.ok = False

    def _send(self, cmd: str) -> int:
        try:
            return self.winmm.mciSendStringW(ctypes.c_wchar_p(cmd), None, 0, None)
        except Exception:
            return -1

    def _query(self, cmd: str) -> str:
        buf = ctypes.create_unicode_buffer(256)
        try:
            if self.winmm.mciSendStringW(ctypes.c_wchar_p(cmd), buf, 255, None) == 0:
                return buf.value.strip()
        except Exception:
            pass
        return ""

    def close(self):
        self._send(f"close {ALIAS}")

    def play(self, path: Path, volume: float = 0.9, should_stop: Callable[[], bool] | None = None):
        if not self.ok:
            return
        self.close()
        if self._send(f'open "{path}" type mpegvideo alias {ALIAS}') != 0:
            # 有些 wav 用 mpegvideo 打不开，退回 waveaudio
            if self._send(f'open "{path}" alias {ALIAS}') != 0:
                return
        vol = int(max(0.0, min(1.0, volume)) * 1000)
        self._send(f"setaudio {ALIAS} volume to {vol}")
        if self._send(f"play {ALIAS}") != 0:
            self.close()
            return

        time.sleep(0.04)
        while True:
            if should_stop and should_stop():
                break
            mode = self._query(f"status {ALIAS} mode")
            if mode in ("stopped", ""):
                break
            time.sleep(0.05)
        self.close()


class PygamePlayer:
    """macOS / Linux 的兜底，装了 pygame 才有。"""

    def __init__(self):
        self.ok = False
        try:
            import pygame

            pygame.mixer.init(frequency=24000, size=-16, channels=2, buffer=1024)
            self.pg = pygame
            self.ok = True
        except Exception:
            self.ok = False

    def play(self, path: Path, volume: float = 0.9, should_stop: Callable[[], bool] | None = None):
        if not self.ok:
            return
        self.pg.mixer.music.load(str(path))
        self.pg.mixer.music.set_volume(max(0.0, min(1.0, volume)))
        self.pg.mixer.music.play()
        while self.pg.mixer.music.get_busy():
            if should_stop and should_stop():
                self.pg.mixer.music.stop()
                break
            time.sleep(0.05)

    def close(self):
        pass


class AudioPlayer:
    def __init__(self):
        self._stop_flag = threading.Event()
        if sys.platform == "win32":
            self._impl = MciPlayer()
            self.backend = "mci"
        else:
            self._impl = PygamePlayer()
            self.backend = "pygame"
        self.ok = self._impl.ok

    def play(self, path: Path, volume: float = 0.9):
        self._stop_flag.clear()
        self._impl.play(path, volume, self._stop_flag.is_set)

    def stop(self):
        self._stop_flag.set()
        self._impl.close()

    def quit(self):
        self._impl.close()


class Broadcaster:
    """收文字 -> 合成 -> 播 -> 记日志。全程一个工作线程，保证顺序。"""

    def __init__(
        self,
        cfg: Config,
        logger,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.cfg = cfg
        self.logger = logger
        self.on_event = on_event or (lambda kind, msg: None)
        self.player = AudioPlayer()
        self.engine: BaseTTS | None = None
        self.cache_dir = ROOT / ".cache_tts"

        maxq = int(cfg.get("broadcast", "max_queue", default=8) or 8)
        # 礼物和 SC 比弹幕重要，队列挤的时候优先播它们
        self.q: queue.PriorityQueue = queue.PriorityQueue(maxsize=max(1, maxq))
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = False
        self._dropped = 0
        self._spoken = 0

    # ---------- 控制 ----------

    def set_engine(self, engine: BaseTTS):
        self.engine = engine

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self.player.stop()

    def pause(self, flag: bool):
        self._paused = flag
        if flag:
            self.player.stop()

    def clear_queue(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    @property
    def stats(self) -> tuple[int, int]:
        return self._spoken, self._dropped

    # ---------- 投递 ----------

    def say(self, text: str, meta: dict | None = None, priority: bool = False) -> bool:
        if not text:
            return False
        try:
            self.q.put_nowait(
                (
                    0 if priority else 1,
                    next(self._seq),
                    {"text": text, "meta": meta or {}},
                )
            )
            return True
        except queue.Full:
            self._dropped += 1
            return False

    # ---------- 工作线程 ----------

    def _loop(self):
        volume = float(self.cfg.get("tts", "volume", default=0.9) or 0.9)
        gap = float(self.cfg.get("broadcast", "min_interval", default=0.25) or 0)
        while self._running:
            try:
                _prio, _seq, item = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._paused or self.engine is None:
                continue

            text = item["text"]
            meta = item["meta"]
            status = "ok"
            try:
                path = self.engine.synthesize(text, self.cache_dir)
                if path is None:
                    status = "muted"
                else:
                    self.player.play(path, volume)
                    self._spoken += 1
            except TTSError as e:
                status = f"tts_error: {e}"
                self.on_event("error", f"合成失败：{e}")
            except Exception as e:
                status = f"error: {e}"
                self.on_event("error", f"播报出错：{e}")

            if self.logger:
                self.logger.write(
                    {
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "user": meta.get("user", ""),
                        "text": meta.get("raw", text),
                        "tts_text": text,
                        "type": meta.get("type", "danmaku"),
                        "engine": getattr(self.engine, "name", "?"),
                        "status": status,
                    }
                )
            if gap > 0:
                time.sleep(gap)
        self.player.quit()
