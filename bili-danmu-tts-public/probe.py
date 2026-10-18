import sys, time, json
sys.path.insert(0, ".")
from app.bili_client import BiliClient
from app.danmu_ws import DanmuClient

rid_in = sys.argv[1] if len(sys.argv) > 1 else "7734200"
secs = int(sys.argv[2]) if len(sys.argv) > 2 else 20

got, ops = [], {}

def on_danmaku(d):
    got.append(d)
    if len(got) <= 8:
        print("  弹幕:", d)

def on_status(s):
    print("  状态:", s)

c = BiliClient("")
c.warmup()
rid = c.resolve_room_id(rid_in)
info = c.get_danmu_info(rid)
print("房间", rid, "host", info["hosts"][0])

cli = DanmuClient(c, rid, info["token"], info["hosts"], on_danmaku, on_status)

_orig = cli._dispatch
def patched(body):
    try:
        p = json.loads(body.decode("utf-8", "ignore"))
        for it in (p if isinstance(p, list) else [p]):
            k = str(it.get("cmd", ""))
            ops[k] = ops.get(k, 0) + 1
    except Exception:
        pass
    _orig(body)
cli._dispatch = patched

cli.start()
time.sleep(secs)
cli.stop()
time.sleep(1)
print("cmd 分布:", dict(sorted(ops.items(), key=lambda x: -x[1])[:8]))
print("弹幕数:", len(got))
