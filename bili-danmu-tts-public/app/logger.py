"""日志：一天一个 jsonl，一行一条，方便后面拿脚本随便统计。"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path


class DanmuLogger:
    def __init__(self, log_dir: Path, enabled: bool = True, keep_days: int = 30):
        self.dir = log_dir
        self.enabled = enabled
        self.keep_days = keep_days
        self._lock = threading.Lock()
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._cleanup()

    def _path(self) -> Path:
        day = dt.date.today().strftime("%Y-%m-%d")
        return self.dir / f"danmu-{day}.jsonl"

    def _cleanup(self):
        try:
            cutoff = dt.date.today() - dt.timedelta(days=self.keep_days)
            for p in self.dir.glob("danmu-*.jsonl"):
                stamp = p.stem.replace("danmu-", "")
                try:
                    if dt.date.fromisoformat(stamp) < cutoff:
                        p.unlink()
                except Exception:
                    continue
        except Exception:
            pass

    def write(self, record: dict):
        if not self.enabled:
            return
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with self._path().open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
