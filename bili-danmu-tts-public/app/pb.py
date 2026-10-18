"""极简 protobuf 解码。

B 站新版的进场/关注消息（INTERACT_WORD_V2）把内容塞进了 protobuf，
data 里只有一个 base64 的 pb 字段。这里不做完整 schema，只按字段号取值：

  InteractWordV2
  1 uid (varint) | 2 uname (string) | 5 msg_type (1进场 2关注 3分享)
  6 roomid | 7 timestamp

只依赖标准库，不引第三方 protobuf，省得打包变大。
"""

from __future__ import annotations


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint 越界")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 70:
            raise ValueError("varint 太长")


def fields(buf: bytes) -> list[tuple[int, int, object]]:
    """把一层 protobuf 拆成 [(字段号, wire_type, 值)]。

    wire_type 0 -> int；2 -> bytes 能按 utf-8 解就解成 str，否则保留 bytes。
    数据不完整或格式不对时，能解多少解多少，不抛异常。
    """
    out: list[tuple[int, int, object]] = []
    i = 0
    n = len(buf)
    try:
        while i < n:
            key, i = _varint(buf, i)
            fno, wt = key >> 3, key & 7
            if wt == 0:
                v, i = _varint(buf, i)
                out.append((fno, wt, v))
            elif wt == 2:
                ln, i = _varint(buf, i)
                raw = buf[i : i + ln]
                i += ln
                try:
                    out.append((fno, wt, raw.decode("utf-8")))
                except Exception:
                    out.append((fno, wt, raw))
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
    except (ValueError, IndexError):
        pass
    return out


def parse_interact(pb: bytes) -> dict:
    """解 InteractWordV2，只取我们关心的三个字段。"""
    info = {"uid": 0, "uname": "", "msg_type": 0, "room_id": 0}
    for fno, wt, val in fields(pb):
        if fno == 1 and wt == 0:
            info["uid"] = int(val)
        elif fno == 2 and isinstance(val, str):
            info["uname"] = val
        elif fno == 5 and wt == 0:
            info["msg_type"] = int(val)
        elif fno == 6 and wt == 0:
            info["room_id"] = int(val)
    return info


def _as_bytes(val) -> bytes:
    if isinstance(val, bytes):
        return val
    return str(val).encode("utf-8", "surrogateescape")


def parse_gift(pb: bytes) -> dict:
    """解 SEND_GIFT_V2。

    结构（实测）：
      1 uid | 2 uname | 10 礼物详情 | 11 数量
      10 里的：1 礼物 id | 2 礼物名 | 5 单价(金瓜子，1000=1元) | 8 币种
    免费礼物的包可能没有 10，只有 13 里的 id，这时名字解不出来，交给上层兜底。
    """
    info = {
        "uid": 0,
        "uname": "",
        "gift": "",
        "gift_id": 0,
        "num": 1,
        "price": 0.0,
        "coin_type": "",
    }
    for fno, wt, val in fields(pb):
        if fno == 1 and wt == 0:
            info["uid"] = int(val)
        elif fno == 2 and isinstance(val, str):
            info["uname"] = val
        elif fno == 11 and wt == 0:
            info["num"] = max(1, int(val))
        elif fno in (10, 13) and isinstance(val, (bytes, str)):
            for sf, sw, sv in fields(_as_bytes(val)):
                if sf == 1 and sw == 0 and not info["gift_id"]:
                    info["gift_id"] = int(sv)
                elif sf == 2 and isinstance(sv, str) and not info["gift"]:
                    info["gift"] = sv
                elif sf == 5 and sw == 0 and not info["price"]:
                    info["price"] = round(int(sv) / 1000.0, 3)
                elif sf == 8 and isinstance(sv, str):
                    info["coin_type"] = sv
                elif sf == 11 and sw == 0 and info["num"] == 1:
                    info["num"] = max(1, int(sv))
    return info
