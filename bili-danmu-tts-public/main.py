"""启动入口。

用 pythonw 启动（不弹黑框），所以任何报错都得自己兜住，
写到 error.log 里，免得窗口一闪就没影了。
"""

from __future__ import annotations

import ctypes
import sys
import traceback


def _enable_dpi_awareness():
    """Windows 高分屏下不糊。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    _enable_dpi_awareness()
    from app.ui import App

    App().mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            from pathlib import Path

            Path(__file__).resolve().parent.joinpath("error.log").write_text(
                tb, encoding="utf-8"
            )
        except Exception:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox

            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                "启动失败",
                "程序没起来，具体原因写在同目录的 error.log 里。\n\n"
                + tb.strip().splitlines()[-1][:200],
            )
            r.destroy()
        except Exception:
            pass
        sys.exit(1)
