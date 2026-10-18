"""连 B站弹幕 websocket，解包，把文字弹幕丢出来。

协议：16 字节包头
  4 字节 总长度 | 2 字节 头长度(=16) | 2 字节 协议版本 | 4 字节 操作码 | 4 字节 序号
操作码 2=心跳 3=心跳回应 5=通知 7=认证 8=认证回应
协议版本 0=JSON 1=JSON 2=zlib 3=brotli（压缩包可以套娃，要递归解）
"""

from __future__ import annotations

import asyncio
import inspect
import json
import struct
import threading
import time
from typing import Callable

import zlib

try:
    import brotli
except Exception:  # 没装 brotli 就退化成 zlib
    brotli = None

import websockets

from .bili_client import BiliClient

HEADER_LEN = 16
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

# websockets 13 之前叫 extra_headers，之后改成 additional_headers
_WS_HEADER_KW = (
    "additional_headers"
    if "additional_headers" in inspect.signature(websockets.connect).parameters
    else "extra_headers"
)
# 15 之后会自动读系统代理。弹幕服务器在国内，直连最稳，
# 走代理经常握手失败（公司网/VPN 全局模式尤其明显），所以显式关掉。
_WS_CAN_NO_PROXY = "proxy" in inspect.signature(websockets.connect).parameters


def pack(op: int, body: bytes) -> bytes:
    return struct.pack(">IHHII", HEADER_LEN + len(body), HEADER_LEN, 1, op, 1) + body


def unpack(data: bytes) -> list[tuple[int, int, bytes]]:
    """把一坨二进制拆成 [(op, protover, body), ...]，压缩包递归展开。"""
    out: list[tuple[int, int, bytes]] = []
    offset = 0
    total = len(data)
    while offset + HEADER_LEN <= total:
        pkt_len, head_len, protover, op, _seq = struct.unpack(
            ">IHHII", data[offset : offset + HEADER_LEN]
        )
        if pkt_len < HEADER_LEN or offset + pkt_len > total:
            break
        body = data[offset + head_len : offset + pkt_len]
        if protover == 2:
            try:
                out.extend(unpack(zlib.decompress(body)))
            except Exception:
                pass
        elif protover == 3:
            if brotli is None:
                pass
            else:
                try:
                    out.extend(unpack(brotli.decompress(body)))
                except Exception:
                    pass
        else:
            out.append((op, protover, body))
        offset += pkt_len
    return out


class DanmuClient:
    """在后台线程里跑 asyncio，界面线程只收回调。"""

    def __init__(
        self,
        client: BiliClient,
        room_id: int,
        token: str,
        hosts: list[tuple[str, int]],
        on_danmaku: Callable[[dict], None],
        on_status: Callable[[str], None],
        buvid: str = "",
        uid: int = 0,
        cookie_header: str = "",
    ):
        self.client = client
        self.room_id = room_id
        self.token = token
        self.hosts = hosts
        self.on_danmaku = on_danmaku
        self.on_status = on_status
        self.buvid = buvid or (client.session.cookies.get("buvid3") or "")
        # 登录态：uid 和 SESSDATA 必须一致。
        # 只带 SESSDATA 却写 uid=0，B站会秒踢连接，这个坑很隐蔽。
        self.uid = int(uid or 0)
        self.cookie_header = cookie_header if self.uid else ""

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stop = threading.Event()

    # ---------- 生命周期 ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        loop, task = self._loop, self._task
        if loop and task:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._main())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.on_status(f"连接线程出错：{e}")
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    # ---------- 主循环（带重连） ----------

    async def _main(self):
        backoff = 2
        while not self._stop.is_set():
            host, port = self.hosts[0]
            try:
                await self._connect_once(host, port)
                backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_status(f"连接断开：{e}，{backoff}s 后重连")
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            # 重连前换一个 host，顺便刷新 token
            self.hosts = self.hosts[1:] + self.hosts[:1]
            try:
                info = await asyncio.to_thread(self.client.get_danmu_info, self.room_id)
                self.token = info["token"]
                if info["hosts"]:
                    self.hosts = info["hosts"]
            except Exception:
                pass

    async def _connect_once(self, host: str, port: int):
        uri = f"wss://{host}:{port}/sub"
        self.on_status(f"连接 {uri}")
        opts: dict = {
            "ping_interval": None,
            "max_size": None,
            "open_timeout": 10,
            "close_timeout": 3,
            "user_agent_header": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        # 登录了就把 cookie 一起带进握手，昵称才不会变成 a***
        if self.cookie_header:
            opts[_WS_HEADER_KW] = {"Cookie": self.cookie_header}
        if _WS_CAN_NO_PROXY:
            opts["proxy"] = None
        async with websockets.connect(uri, **opts) as ws:
            auth = {
                "uid": self.uid,
                "roomid": self.room_id,
                "protover": 3,
                "platform": "web",
                "type": 2,
                "key": self.token,
            }
            if self.buvid:
                auth["buvid"] = self.buvid
            await ws.send(pack(OP_AUTH, json.dumps(auth).encode()))

            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                started = False
                while not self._stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    if isinstance(raw, str):
                        continue
                    for op, _ver, body in unpack(raw):
                        if op == OP_AUTH_REPLY:
                            code = -1
                            try:
                                code = json.loads(body or b"{}").get("code", 0)
                            except Exception:
                                pass
                            if code != 0:
                                self.on_status(f"鉴权失败 code={code}")
                                raise RuntimeError(f"auth failed code={code}")
                            started = True
                            self.on_status(f"已连上直播间 {self.room_id}，等弹幕")
                        elif op == OP_HEARTBEAT_REPLY:
                            if not started:
                                self.on_status("心跳正常")
                        elif op == OP_MESSAGE:
                            self._dispatch(body)
            finally:
                hb.cancel()

    async def _heartbeat(self, ws):
        while not self._stop.is_set():
            try:
                await ws.send(pack(OP_HEARTBEAT, b"[object Object]"))
            except Exception:
                return
            await asyncio.sleep(25)

    # ---------- 事件分发 ----------

    def _dispatch(self, body: bytes):
        try:
            payload = json.loads(body.decode("utf-8", errors="ignore"))
        except Exception:
            return
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            cmd = str(item.get("cmd", ""))
            try:
                if cmd.startswith("DANMU_MSG"):
                    self._on_plain(item)
                elif cmd in ("SEND_GIFT", "SEND_GIFT_V2"):
                    self._on_gift(item)
                elif cmd == "SUPER_CHAT_MESSAGE":
                    self._on_sc(item)
                elif cmd == "GUARD_BUY":
                    self._on_guard(item)
                elif cmd in ("INTERACT_WORD", "INTERACT_WORD_V2"):
                    self._on_interact(item)
            except Exception:
                continue

    def _emit(self, **kw):
        kw.setdefault("time", time.strftime("%H:%M:%S"))
        self.on_danmaku(kw)

    def _on_plain(self, item: dict):
        info = item.get("info") or []
        if len(info) < 3:
            return
        text = str(info[1])
        user = ""
        if isinstance(info[2], list) and len(info[2]) > 1:
            user = str(info[2][1])
        if not text.strip():
            return
        self._emit(type="danmaku", user=user, text=text)

    def _on_gift(self, item: dict):
        data = item.get("data") or {}

        # 新版礼物是 protobuf（SEND_GIFT_V2），老版是明文 JSON
        if data.get("pb"):
            try:
                import base64

                from .pb import parse_gift

                g = parse_gift(base64.b64decode(data["pb"]))
            except Exception:
                return
            gift = g["gift"] or ("礼物" if g["gift_id"] else "")
            if not g["uname"] or not gift:
                return
            self._emit(
                type="gift",
                user=g["uname"],
                text=gift,
                gift=gift,
                num=g["num"],
                price=g["price"],
                paid=bool(g["coin_type"] == "gold" or g["price"] >= 0.1),
                raw=f"赠送 {gift} x{g['num']}",
            )
            return

        user = str(data.get("uname") or "")
        gift = str(data.get("giftName") or data.get("gift_name") or "").strip()
        if not user or not gift:
            return
        try:
            num = int(data.get("num") or 1)
        except Exception:
            num = 1
        # total_coin 单位是金瓜子，1000 = 1 元
        try:
            coin = float(data.get("total_coin") or 0) / 1000.0
        except Exception:
            coin = 0.0
        self._emit(
            type="gift",
            user=user,
            text=gift,
            gift=gift,
            num=num,
            price=round(coin, 2),
            paid=bool(data.get("coin_type") == "gold" or coin >= 0.1),
            raw=f"赠送 {gift} x{num}",
        )

    def _on_guard(self, item: dict):
        data = item.get("data") or {}
        user = str(data.get("username") or data.get("uname") or "")
        if not user:
            return
        level = int(data.get("guard_level") or 3)
        name = {1: "总督", 2: "提督", 3: "舰长"}.get(level, "舰长")
        self._emit(
            type="guard",
            user=user,
            text=name,
            gift=name,
            num=int(data.get("num") or 1),
            raw=f"开通{name}",
        )

    def _on_sc(self, item: dict):
        data = item.get("data") or {}
        text = str(data.get("message") or "").strip()
        if not text:
            return
        self._emit(
            type="sc",
            user=str(
                (data.get("user_info") or {}).get("uname") or data.get("uname") or ""
            ),
            text=text,
            price=data.get("price"),
            raw=f"SC {text}",
        )

    def _on_interact(self, item: dict):
        data = item.get("data") or {}
        user = str(data.get("uname") or "")
        try:
            mt = int(data.get("msg_type") or 0)
        except Exception:
            mt = 0

        # 新版是 protobuf，全塞在 data.pb 里
        if not user and data.get("pb"):
            try:
                import base64

                from .pb import parse_interact

                info = parse_interact(base64.b64decode(data["pb"]))
                user = info["uname"]
                mt = info["msg_type"]
            except Exception:
                return

        if not user:
            return
        if mt == 1:
            # 进场量最大，同一个人反复进出会在上层去重，这里只负责分发
            self._emit(type="enter", user=user, text="来了", raw="进入直播间")
        elif mt in (2, 4, 5):
            self._emit(type="follow", user=user, text="关注了", raw="关注了主播")
