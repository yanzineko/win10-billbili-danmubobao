"""tkinter 界面。

原则：网络和 TTS 都在后台线程，所有控件操作只在主线程。
跨线程通信统一往 self.events 里塞，主线程 after 轮询。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .bili_client import BiliClient, BiliError
from .config import Config, ensure_dirs
from .danmu_ws import DanmuClient
from .logger import DanmuLogger
from .player import Broadcaster
from .tts import build_engine
from .utils import event_enabled, speech_for_event

ENGINES = {"阿里云（百炼）": "aliyun", "豆包（火山）": "doubao", "静音，只记日志": "none"}
ENGINE_REV = {v: k for k, v in ENGINES.items()}

FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = Config()
        self.log_dir = ensure_dirs(self.cfg)
        self.logger = DanmuLogger(
            self.log_dir, enabled=bool(self.cfg.get("log", "enabled", default=True))
        )

        self.events: queue.Queue = queue.Queue()
        self.ws_client: DanmuClient | None = None
        self.bili: BiliClient | None = None
        self._recent: dict = {}
        self._rate: dict[str, list[float]] = {}
        self.broadcaster = Broadcaster(self.cfg, self.logger, self._push_event)
        self.broadcaster.set_engine(build_engine(self.cfg))
        self.broadcaster.start()

        self.title("B站弹幕播报机")
        self.configure(bg="#f7f7f8")
        self._init_scaling()
        self._build_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll_events)

        self._push_event("status", "就绪。填直播间号，点连接。")

    # ---------- 外观 ----------

    def _init_scaling(self):
        """高分屏适配。

        进程是 DPI aware 的，Tk 的 1 单位就是 1 物理像素，
        150% 缩放的屏幕上 880x620 会小得像巴掌。所以按实际 DPI 放大一遍，
        字体和窗口一起放，比例才不会乱。
        """
        scale = 1.0
        try:
            scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except Exception:
            scale = 1.0
        self.scale = scale
        try:
            self.tk.call("tk", "scaling", scale * 96 / 72)
        except Exception:
            pass
        w, h = int(880 * scale), int(620 * scale)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, int((sh - h) * 0.35))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(int(720 * scale), int(480 * scale))

    def _build_style(self):
        st = ttk.Style(self)
        s = getattr(self, "scale", 1.0)
        # 用系统原生主题，clam 在 Windows 上那个复选框会画成叉，很难看
        for name in ("vista", "winnative", "aqua", "clam"):
            if name in st.theme_names():
                st.theme_use(name)
                break
        st.configure(".", font=FONT)
        st.configure("TLabel", foreground="#333333")
        st.configure("Hint.TLabel", foreground="#8a8a8a", font=FONT_SMALL)
        st.configure("TButton", padding=(int(10 * s), int(4 * s)))
        st.configure("Accent.TButton", padding=(int(14 * s), int(4 * s)))
        st.configure("TEntry", padding=int(3 * s))

    # ---------- 布局 ----------

    def _build_ui(self):
        s = getattr(self, "scale", 1.0)
        px = lambda *v: tuple(int(x * s) for x in v)

        top = ttk.Frame(self, padding=px(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text="直播间").pack(side="left")
        self.room_var = tk.StringVar(
            value=str(self.cfg.get("room", "room_id", default="") or "")
        )
        self.room_entry = ttk.Entry(top, textvariable=self.room_var, width=14)
        self.room_entry.pack(side="left", padx=(6, 6))
        self.room_entry.bind("<Return>", lambda e: self.toggle_connect())

        self.btn_conn = ttk.Button(
            top, text="连接", width=7, style="Accent.TButton", command=self.toggle_connect
        )
        self.btn_conn.pack(side="left")

        ttk.Label(top, text="  语音").pack(side="left")
        self.engine_var = tk.StringVar(
            value=ENGINE_REV.get(
                self.cfg.get("tts", "engine", default="aliyun"), "阿里云（百炼）"
            )
        )
        cb = ttk.Combobox(
            top,
            textvariable=self.engine_var,
            values=list(ENGINES.keys()),
            width=15,
            state="readonly",
        )
        cb.pack(side="left", padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_engine_change())

        self.read_user_var = tk.BooleanVar(
            value=bool(self.cfg.get("broadcast", "read_username", default=True))
        )
        ttk.Checkbutton(
            top,
            text="念昵称",
            variable=self.read_user_var,
            command=self._on_read_user_change,
        ).pack(side="left", padx=(12, 0))

        ttk.Button(top, text="设置", command=self.open_settings).pack(side="right")

        # 播报内容开关：每一类都能单独关
        fbar = ttk.Frame(self, padding=px(12, 0, 12, 6))
        fbar.pack(fill="x")
        ttk.Label(fbar, text="播报", style="Hint.TLabel").pack(side="left", padx=(0, 8))
        self.switch_vars: dict[str, tk.BooleanVar] = {}
        for text, key, default in (
            ("弹幕", "read_danmaku", True),
            ("礼物", "read_gift", True),
            ("SC", "read_sc", True),
            ("关注", "read_follow", True),
            ("进场", "read_enter", False),
            ("只播付费礼物", "ignore_free_gift", False),
        ):
            var = tk.BooleanVar(
                value=bool(self.cfg.get("broadcast", key, default=default))
            )
            self.switch_vars[key] = var
            ttk.Checkbutton(
                fbar,
                text=text,
                variable=var,
                command=lambda k=key: self._on_switch(k),
            ).pack(side="left", padx=(0, 10))

        # 弹幕列表
        body = ttk.Frame(self, padding=px(12, 0, 12, 0))
        body.pack(fill="both", expand=True)

        wrap = tk.Frame(body, bg="#ffffff", highlightthickness=1,
                        highlightbackground="#e3e3e6")
        wrap.pack(fill="both", expand=True)

        self.text = tk.Text(
            wrap,
            wrap="word",
            font=FONT,
            bg="#ffffff",
            fg="#222222",
            relief="flat",
            padx=int(12 * s),
            pady=int(8 * s),
            state="disabled",
            cursor="arrow",
        )
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure("time", foreground="#a0a0a0", font=FONT_SMALL)
        self.text.tag_configure("user", foreground="#1a6fbf")
        self.text.tag_configure("body", foreground="#222222")
        self.text.tag_configure("sc", foreground="#d2691e")
        self.text.tag_configure("gift", foreground="#d9822b")
        self.text.tag_configure("guard", foreground="#8e44ad")
        self.text.tag_configure("follow", foreground="#2e8b57")
        self.text.tag_configure("enter", foreground="#8a8a8a", font=FONT_SMALL)
        self.text.tag_configure("sys", foreground="#999999", font=FONT_SMALL)
        self.text.tag_configure("err", foreground="#c0392b", font=FONT_SMALL)
        self.text.tag_configure(
            "ph", foreground="#c8c8cc", justify="center",
            font=("Microsoft YaHei UI", 11), spacing1=int(160 * s),
        )
        self.text.configure(state="normal")
        self.text.insert("end", "填好直播间号，点上面的「连接」\n\n第一次用先去「设置」里填语音密钥", "ph")
        self.text.configure(state="disabled")

        # 底部
        bottom = ttk.Frame(self, padding=px(12, 8, 12, 10))
        bottom.pack(fill="x")

        self.btn_pause = ttk.Button(bottom, text="暂停播报", command=self.toggle_pause)
        self.btn_pause.pack(side="left")
        ttk.Button(bottom, text="清空队列", command=self.clear_queue).pack(
            side="left", padx=6
        )
        ttk.Button(bottom, text="试一句", command=self.test_speak).pack(side="left")

        self.stat_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.stat_var, style="Hint.TLabel").pack(
            side="right"
        )

        self.status_var = tk.StringVar(value="")
        self.status_label = ttk.Label(
            self, textvariable=self.status_var, style="Hint.TLabel", padding=px(14, 0, 14, 8)
        )
        self.status_label.pack(fill="x")

    # ---------- 事件环 ----------

    def _push_event(self, kind: str, payload):
        """线程安全地往 UI 塞事件。"""
        self.events.put((kind, payload))

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "danmaku":
                    self._render_danmaku(payload)
                elif kind == "status":
                    self.status_var.set(str(payload))
                    self._append_sys(str(payload))
                elif kind == "error":
                    self._append_sys(str(payload), tag="err")
                elif kind == "conn_ok":
                    self.text.configure(state="normal")
                    self.text.delete("1.0", "end")
                    self.text.configure(state="disabled")
                    self.btn_conn.configure(text="断开", state="normal")
                    self.status_var.set(f"正在监听直播间 {payload}")
                elif kind == "conn_fail":
                    self.ws_client = None
                    self.btn_conn.configure(text="连接", state="normal")
                    self.status_var.set("连接失败")
                    self._append_sys(f"连接失败：{payload}", tag="err")
                    messagebox.showerror("连不上", str(payload)[:800])
        except queue.Empty:
            pass
        spoken, dropped = self.broadcaster.stats
        qsize = self.broadcaster.q.qsize()
        self.stat_var.set(f"已播 {spoken}　队列 {qsize}　丢弃 {dropped}")
        self.after(150, self._poll_events)

    def _append_sys(self, msg: str, tag: str = "sys"):
        self._insert(f"[系统] {msg}\n", tag)

    def _insert(self, text: str, tag: str):
        at_bottom = self.text.yview()[1] > 0.98
        self.text.configure(state="normal")
        self.text.insert("end", text, tag)
        limit = int(self.cfg.get("ui", "max_lines", default=300) or 300)
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > limit * 1.5:
            self.text.delete("1.0", f"{line_count - limit}.0")
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def _render_danmaku(self, d: dict):
        etype = d.get("type") or "danmaku"
        tag = etype if etype in ("sc", "gift", "guard", "follow", "enter") else "body"
        label = ""
        body = str(d.get("text") or "")

        if etype == "gift":
            label = "[礼物] "
            num = int(d.get("num") or 1)
            body = f"赠送 {d.get('gift', '')}" + (f" ×{num}" if num > 1 else "")
            price = d.get("price") or 0
            if price:
                body += f"（{price} 元）"
        elif etype == "guard":
            label = "[上舰] "
            body = f"开通 {d.get('gift', '')}"
        elif etype == "sc":
            label = "[醒目留言] "
            price = d.get("price") or 0
            if price:
                label = f"[SC {price}元] "
        elif etype == "follow":
            label = "[关注] "
            body = "关注了主播"
        elif etype == "enter":
            label = "[进场] "
            body = "进入直播间"

        self._text_line(d.get("time", ""), d.get("user", ""), body, tag=tag, label=label)

    def _text_line(
        self, t: str, user: str, body: str, tag: str = "body", label: str = ""
    ):
        at_bottom = self.text.yview()[1] > 0.98
        self.text.configure(state="normal")
        self.text.insert("end", f"[{t}] ", "time")
        if label:
            self.text.insert("end", label, tag)
        if user:
            self.text.insert("end", f"{user} ", "user")
        self.text.insert("end", f"{body}\n", tag)
        limit = int(self.cfg.get("ui", "max_lines", default=300) or 300)
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > limit * 1.5:
            self.text.delete("1.0", f"{line_count - limit}.0")
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    # ---------- 连接 ----------

    def _is_connected(self) -> bool:
        return self.ws_client is not None

    def toggle_connect(self):
        if self._is_connected():
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        room = self.room_var.get().strip()
        if not room:
            messagebox.showwarning("缺东西", "直播间号还没填。")
            return
        self.cfg.set(room, "room", "room_id")
        self.cfg.save()

        self.btn_conn.configure(text="连接中…", state="disabled")
        self.status_var.set("正在解析房间…")
        threading.Thread(target=self._connect_worker, args=(room,), daemon=True).start()

    def _connect_worker(self, room: str):
        try:
            self.bili = BiliClient(self.cfg.get("room", "cookie", default="") or "")
            self.bili.warmup()
            rid = self.bili.resolve_room_id(room)

            # 有 cookie 必须拿到真实 uid，否则 B 站会掐断连（uid=0 但带着 SESSDATA）
            info_login = self.bili.login_info()
            if info_login["is_login"]:
                self._push_event(
                    "status", f"已登录：{info_login['uname']}，正在拿弹幕服务器…"
                )
            elif (self.cfg.get("room", "cookie", default="") or "").strip():
                self._push_event(
                    "status",
                    "cookie 没生效（未登录），先用游客身份连。"
                    "多半是 SESSDATA 过期或粘错了。",
                )
            else:
                self._push_event("status", f"房间号 {rid}，正在拿弹幕服务器…")

            info = self.bili.get_danmu_info(rid)
            self.ws_client = DanmuClient(
                client=self.bili,
                room_id=rid,
                token=info["token"],
                hosts=info["hosts"],
                on_danmaku=self._on_danmaku,
                on_status=lambda s: self._push_event("status", s),
                uid=info_login["uid"],
                cookie_header=self.bili.cookie_header() if info_login["is_login"] else "",
            )
            self.ws_client.start()
            self.events.put(("conn_ok", rid))
        except BiliError as e:
            self.events.put(("conn_fail", str(e)))
        except Exception as e:
            self.events.put(("conn_fail", f"{type(e).__name__}: {e}"))

    def disconnect(self):
        if self.ws_client:
            self.ws_client.stop()
            self.ws_client = None
        self.btn_conn.configure(text="连接", state="normal")
        self.status_var.set("已断开")

    # ---------- 弹幕处理 ----------

    def _on_danmaku(self, d: dict):
        """在 WS 线程里被调用。不管播不播，界面都先显示出来。"""
        etype = d.get("type") or "danmaku"
        self._push_event("danmaku", d)

        if not event_enabled(etype, self.cfg):
            return
        if self._is_duplicate(d):
            return
        speech = speech_for_event(d, self.cfg)
        if not speech:
            return
        self.broadcaster.say(
            speech,
            {
                "user": d.get("user", ""),
                "type": etype,
                "raw": d.get("raw") or d.get("text", ""),
            },
            priority=etype in ("gift", "guard", "sc"),
        )

    def _is_duplicate(self, d: dict) -> bool:
        """礼物连击和反复进场都会刷屏，这里做去重 + 限速。"""
        import time as _t

        etype = d.get("type") or "danmaku"
        user = d.get("user") or ""
        now = _t.time()

        if etype == "enter":
            window = float(
                self.cfg.get("broadcast", "enter_cooldown", default=600) or 0
            )
            key = ("enter", user)
            limit = int(self.cfg.get("broadcast", "enter_per_min", default=6) or 0)
        elif etype in ("gift", "guard"):
            window = 3.0
            key = ("gift", user, d.get("gift") or d.get("text"))
            limit = 0
        elif etype == "follow":
            window = 0.0
            key = ("follow", user)
            limit = int(self.cfg.get("broadcast", "follow_per_min", default=10) or 0)
        else:
            return False

        last = self._recent.get(key)
        self._recent[key] = now
        if len(self._recent) > 1500:
            cutoff = now - 3600
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}
        if last is not None and (now - last) < window:
            return True
        if limit > 0 and not self._rate_ok(etype, limit, now):
            return True
        return False

    def _rate_ok(self, kind: str, per_min: int, now: float) -> bool:
        """滑动窗口限速：一类事件每分钟最多播 per_min 条。"""
        hits = [t for t in self._rate.get(kind, []) if now - t < 60]
        if len(hits) >= per_min:
            self._rate[kind] = hits
            return False
        hits.append(now)
        self._rate[kind] = hits
        return True

    def _on_switch(self, key: str):
        self.cfg.set(bool(self.switch_vars[key].get()), "broadcast", key)
        self.cfg.save()

    # ---------- 按钮 ----------

    def toggle_pause(self):
        paused = self.btn_pause.cget("text") == "暂停播报"
        self.broadcaster.pause(paused)
        self.btn_pause.configure(text="继续播报" if paused else "暂停播报")

    def clear_queue(self):
        self.broadcaster.clear_queue()
        self.status_var.set("队列清空了")

    def test_speak(self):
        eng = self.broadcaster.engine
        if eng is None or not eng.is_ready():
            messagebox.showinfo("还没配好", "先去设置里把语音服务的密钥填上。")
            return
        self.broadcaster.say("测试一下，能听到我说话吗？", {"user": "", "raw": "测试"})
        self.status_var.set("已投递一条测试播报，稍等一秒")

    def _on_engine_change(self):
        key = ENGINES.get(self.engine_var.get(), "aliyun")
        self.cfg.set(key, "tts", "engine")
        self.cfg.save()
        self.broadcaster.set_engine(build_engine(self.cfg))
        eng = self.broadcaster.engine
        if eng is not None and not eng.is_ready():
            self.status_var.set(f"{self.engine_var.get()} 还没填密钥，去设置里补一下")
        else:
            self.status_var.set(f"语音换成 {self.engine_var.get()} 了")

    def _on_read_user_change(self):
        self.cfg.set(bool(self.read_user_var.get()), "broadcast", "read_username")
        self.cfg.save()

    # ---------- 设置窗口 ----------

    def open_settings(self):
        return SettingsWindow(self)

    def reload_engine(self):
        self.broadcaster.set_engine(build_engine(self.cfg))

    # ---------- 退出 ----------

    def _on_close(self):
        try:
            self.disconnect()
            self.broadcaster.stop()
        finally:
            self.destroy()


class SettingsWindow(tk.Toplevel):
    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.cfg = app.cfg
        self.title("设置")
        sc = getattr(app, "scale", 1.0)
        self.geometry(f"{int(600 * sc)}x{int(430 * sc)}")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=12)

        self._tab_bili(nb)
        self._tab_aliyun(nb)
        self._tab_doubao(nb)
        self._tab_broadcast(nb)
        self._load()

        bar = ttk.Frame(self, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="保存", style="Accent.TButton", command=self.save).pack(
            side="right"
        )
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=6)
        ttk.Label(
            bar, text=f"存到 {self.cfg.path.name}", style="Hint.TLabel"
        ).pack(side="left")

    def _row(self, parent, r: int, label: str, hint: str = ""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=6)
        var = tk.StringVar()
        e = ttk.Entry(parent, textvariable=var, width=42)
        e.grid(row=r, column=1, sticky="we", pady=6, padx=(8, 0))
        if hint:
            ttk.Label(parent, text=hint, style="Hint.TLabel").grid(
                row=r + 1, column=1, sticky="w", padx=(8, 0)
            )
        return var

    def _tab_bili(self, nb):
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text="B站")
        f.columnconfigure(1, weight=1)
        ttk.Label(
            f,
            text="一般情况下不用填 cookie。如果连接时报风控（-352），\n"
                 "就在浏览器登录 B站，F12 复制 Cookie 整串粘进来。",
            style="Hint.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.v_cookie = self._row(f, 1, "Cookie")

    def _tab_aliyun(self, nb):
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text="阿里云百炼")
        f.columnconfigure(1, weight=1)
        ttk.Label(
            f,
            text="百炼控制台拿 API-KEY（sk- 开头）。\n"
                 "模型建议 cosyvoice-v3-flash，音色填 longanyang 这种 v3 音色名。\n"
                 "（老的 qwen-tts 已经在 2026-10 下线了，填了也调不通）",
            style="Hint.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.v_ali_key = self._row(f, 1, "API Key")
        self.v_ali_model = self._row(f, 3, "模型")
        self.v_ali_voice = self._row(f, 5, "音色")

    def _tab_doubao(self, nb):
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text="豆包（火山）")
        f.columnconfigure(1, weight=1)
        ttk.Label(
            f,
            text="火山引擎 → 语音技术 → 语音合成，拿 APP ID 和 Access Token。\n"
                 "音色 id 必须和已开通的服务版本对应：\n"
                 "  开通「语音合成 2.0」→ 填 zh_female_vv_mars_bigtts（_mars_bigtts 结尾）\n"
                 "  开通「语音合成 1.0」→ 填 zh_female_qingxin（老的短名）\n"
                 "填错版本会报 3001 requested resource not granted。",
            style="Hint.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.v_db_appid = self._row(f, 1, "APP ID")
        self.v_db_token = self._row(f, 3, "Access Token")
        self.v_db_voice = self._row(f, 5, "音色 id")

    def _tab_broadcast(self, nb):
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text="播报规则")
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="语速").grid(row=0, column=0, sticky="w", pady=6)
        self.v_speed = tk.DoubleVar(
            value=float(self.cfg.get("tts", "speed", default=1.0))
        )
        ttk.Scale(
            f, from_=0.5, to=2.0, variable=self.v_speed, orient="horizontal",
            length=260,
        ).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(f, text="音量").grid(row=1, column=0, sticky="w", pady=6)
        self.v_volume = tk.DoubleVar(
            value=float(self.cfg.get("tts", "volume", default=0.9))
        )
        ttk.Scale(
            f, from_=0.0, to=1.0, variable=self.v_volume, orient="horizontal",
            length=260,
        ).grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(f, text="单条最长字数").grid(row=2, column=0, sticky="w", pady=6)
        self.v_maxchars = tk.StringVar(
            value=str(self.cfg.get("broadcast", "max_chars", default=60))
        )
        ttk.Entry(f, textvariable=self.v_maxchars, width=8).grid(
            row=2, column=1, sticky="w", padx=8
        )

        ttk.Label(f, text="待播队列上限").grid(row=3, column=0, sticky="w", pady=6)
        self.v_maxq = tk.StringVar(
            value=str(self.cfg.get("broadcast", "max_queue", default=8))
        )
        ttk.Entry(f, textvariable=self.v_maxq, width=8).grid(
            row=3, column=1, sticky="w", padx=8
        )

        ttk.Label(f, text="屏蔽词（逗号分隔）").grid(row=4, column=0, sticky="w", pady=6)
        self.v_mute = tk.StringVar(
            value=",".join(self.cfg.get("broadcast", "mute_keywords", default=[]) or [])
        )
        ttk.Entry(f, textvariable=self.v_mute, width=34).grid(
            row=4, column=1, sticky="w", padx=8
        )

        self.v_log = tk.BooleanVar(value=bool(self.cfg.get("log", "enabled", default=True)))
        ttk.Checkbutton(f, text="写日志到 logs/ 目录", variable=self.v_log).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

    # ---------- 加载 / 保存 ----------

    def _load(self):
        c = self.cfg
        self.v_cookie.set(c.get("room", "cookie", default="") or "")
        self.v_ali_key.set(c.get("tts", "aliyun", "api_key", default="") or "")
        self.v_ali_model.set(
            c.get("tts", "aliyun", "model", default="cosyvoice-v3-flash")
            or "cosyvoice-v3-flash"
        )
        self.v_ali_voice.set(
            c.get("tts", "aliyun", "voice", default="longanyang") or "longanyang"
        )
        self.v_db_appid.set(c.get("tts", "doubao", "appid", default="") or "")
        self.v_db_token.set(c.get("tts", "doubao", "access_token", default="") or "")
        self.v_db_voice.set(
            c.get("tts", "doubao", "voice_type", default="zh_female_vv_mars_bigtts")
            or "zh_female_qingxin"
        )

    def save(self):
        c = self.cfg
        c.set(self.v_cookie.get().strip(), "room", "cookie")
        c.set(self.v_ali_key.get().strip(), "tts", "aliyun", "api_key")
        c.set(
            self.v_ali_model.get().strip() or "cosyvoice-v3-flash",
            "tts", "aliyun", "model",
        )
        c.set(
            self.v_ali_voice.get().strip() or "longanyang",
            "tts", "aliyun", "voice",
        )
        c.set(self.v_db_appid.get().strip(), "tts", "doubao", "appid")
        c.set(self.v_db_token.get().strip(), "tts", "doubao", "access_token")
        c.set(
            self.v_db_voice.get().strip() or "zh_female_vv_mars_bigtts",
            "tts", "doubao", "voice_type",
        )
        c.set(round(float(self.v_speed.get()), 2), "tts", "speed")
        c.set(round(float(self.v_volume.get()), 2), "tts", "volume")

        try:
            c.set(max(10, int(self.v_maxchars.get())), "broadcast", "max_chars")
        except ValueError:
            pass
        try:
            c.set(max(1, int(self.v_maxq.get())), "broadcast", "max_queue")
        except ValueError:
            pass

        kws = [k.strip() for k in self.v_mute.get().split(",") if k.strip()]
        c.set(kws, "broadcast", "mute_keywords")
        c.set(bool(self.v_log.get()), "log", "enabled")
        c.save()

        self.app.logger.enabled = bool(self.v_log.get())
        self.app.reload_engine()
        self.app.status_var.set("设置保存好了")
        self.destroy()
