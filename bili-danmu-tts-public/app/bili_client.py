"""B站直播接口：短号转真实房间号、拿弹幕服务器地址和鉴权 token。

B站这几年接口加过好几轮风控，所以这里做了两条路：
  1. getDanmuInfo（官方在用的，要 wbi 签名，最好带 cookie）
  2. getConf（老接口，不需要签名，偶尔还能用）
拿到 token 和 host 之后才连 websocket。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
from typing import Any

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
    11, 36, 20, 34, 44, 52,
]


class BiliError(RuntimeError):
    pass


def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _sign_wbi(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, Any]:
    mixin = _mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    items = sorted(params.items())
    # 值的 !'()* 字符要过滤掉，不然签名对不上
    query = urllib.parse.urlencode(
        [(k, re.sub(r"[!'()*]", "", str(v))) for k, v in items]
    )
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


def parse_cookie(raw: str) -> dict[str, str]:
    """解析 cookie，尽量什么写法都认。

    支持的输入：
      1. 完整串   SESSDATA=xxx; buvid3=yyy
      2. 只粘值   ab12cd34%2C1700000000... （插件里复制 value 就是这个）
      3. 带前缀   Cookie: SESSDATA=xxx; ...
      4. 多行粘贴（每行一个 cookie）
      5. Cookie-Editor 导出的 JSON 数组
    """
    raw = (raw or "").strip()
    if not raw:
        return {}

    # JSON 导出格式
    if raw.startswith("["):
        try:
            items = json.loads(raw)
            out = {}
            for it in items:
                if isinstance(it, dict) and it.get("name"):
                    out[str(it["name"])] = str(it.get("value") or "")
            if out:
                return out
        except Exception:
            pass

    # 去掉 "Cookie:" 前缀、把换行当分隔符
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):]
    raw = re.sub(r"[\r\n\t]+", "; ", raw)

    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip().strip(",")
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        elif ":" in part:
            k, v = part.split(":", 1)
        else:
            continue
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v:
            out[k] = v

    # 一个 key 都没有 → 用户粘的是 SESSDATA 的值
    if not out:
        out["SESSDATA"] = raw.strip().strip('"').strip("'")
    return out


class BiliClient:
    def __init__(self, cookie: str = ""):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Referer": "https://live.bilibili.com/",
                "Origin": "https://live.bilibili.com",
            }
        )
        for k, v in parse_cookie(cookie).items():
            self.session.cookies.set(k, v, domain=".bilibili.com")
        self._wbi: tuple[str, str] | None = None
        self._lock = threading.Lock()
        self._nav: dict | None = None

    # ---------- 基础请求 ----------

    def _get(self, url: str, params: dict | None = None, timeout: int = 10) -> dict:
        r = self.session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def warmup(self):
        """裸访问首页，让 B 站塞一个 buvid3，很多接口没这个就 -352。"""
        try:
            self.session.get("https://www.bilibili.com/", timeout=10)
        except Exception:
            pass

    def login_info(self) -> dict:
        """看 cookie 到底登没登录。

        这一步很关键：带 SESSDATA 却用 uid=0 去连弹幕服务器，
        B 站会直接掐断连接（现象就是「连接断开，2s 后重连」循环）。
        """
        if self._nav is not None:
            return self._nav
        info = {"is_login": False, "uid": 0, "uname": ""}
        try:
            data = self._get("https://api.bilibili.com/x/web-interface/nav")
            d = data.get("data") or {}
            if d.get("isLogin") and d.get("mid"):
                info = {
                    "is_login": True,
                    "uid": int(d["mid"]),
                    "uname": str(d.get("uname") or ""),
                }
        except Exception:
            pass
        self._nav = info
        return info

    def cookie_header(self) -> str:
        """把 session 里的 cookie 拼成 WS 握手用的 Cookie 头。"""
        return "; ".join(f"{k}={v}" for k, v in self.session.cookies.items())

    # ---------- 房间号 ----------

    def resolve_room_id(self, room: str | int) -> int:
        room = str(room).strip()
        if not room:
            raise BiliError("直播间号是空的")
        if room.isdigit():
            data = self._get(
                "https://api.live.bilibili.com/room/v1/Room/room_init", {"id": room}
            )
            if data.get("code") == 0:
                return int(data["data"]["room_id"])
            raise BiliError(f"房间号解析失败：{data.get('message')}")
        # 支持直接粘 https://live.bilibili.com/12345 这种
        m = re.search(r"live\.bilibili\.com/(\d+)", room)
        if m:
            return self.resolve_room_id(m.group(1))
        raise BiliError("看不懂这个直播间号/链接")

    # ---------- 弹幕服务器 ----------

    def _get_wbi_keys(self) -> tuple[str, str]:
        with self._lock:
            if self._wbi:
                return self._wbi
        data = self._get("https://api.bilibili.com/x/web-interface/nav")
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError("拿不到 wbi 签名 key（nav 接口没返回）")
        with self._lock:
            self._wbi = (img, sub)
        return img, sub

    def get_danmu_info(self, room_id: int) -> dict:
        """返回 {'token': str, 'hosts': [(host, wss_port), ...]}"""
        errors: list[str] = []

        # 路线 1：wbi 签名版
        for signed in (True, False):
            try:
                params: dict[str, Any] = {"id": room_id, "type": 0}
                if signed:
                    img, sub = self._get_wbi_keys()
                    params = _sign_wbi(params, img, sub)
                data = self._get(
                    "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
                    params,
                )
                if data.get("code") == 0:
                    d = data["data"]
                    hosts = [(h["host"], h["wss_port"]) for h in d["host_list"]]
                    return {"token": d["token"], "hosts": hosts}
                errors.append(f"getDanmuInfo(signed={signed}) code={data.get('code')} {data.get('message')}")
                if data.get("code") == -352 and not self.session.cookies.get("SESSDATA"):
                    errors.append("→ 这是风控，填 cookie 里带上 SESSDATA 再试")
            except Exception as e:
                errors.append(f"getDanmuInfo(signed={signed}) 异常: {e}")

        # 路线 2：老的 getConf
        try:
            data = self._get(
                "https://api.live.bilibili.com/room/v1/Danmu/getConf",
                {"room_id": room_id, "platform": "pc", "player": "web"},
            )
            if data.get("code") == 0:
                d = data["data"]
                hosts = []
                for h in d.get("host_server_list", []):
                    port = h.get("wss_port") or h.get("port")
                    hosts.append((h["host"], port))
                if d.get("token") and hosts:
                    return {"token": d["token"], "hosts": hosts}
            errors.append(f"getConf code={data.get('code')} {data.get('message')}")
        except Exception as e:
            errors.append(f"getConf 异常: {e}")

        raise BiliError("拿不到弹幕服务器：\n" + "\n".join(errors))
