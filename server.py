#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小姐姐放映厅 · 本地服务（零依赖，Python 3.7+）
作用：
  1. 托管本目录静态页面（index.html）
  2. /api/resolve  解析随机视频直链（服务端请求，绕过浏览器 CORS）
     —— 并发拉取多个候选，探测真实分辨率/时长/码率，择优返回
用法：
  python server.py            # 默认 127.0.0.1:8899 并自动打开浏览器
  python server.py --port 9000 --no-open

可调环境变量：
  CANDIDATES   并发候选数，默认 3（越大画质越好、解析越慢）
  MIN_KBPS     最低码率门槛，默认 1100（低于此值视为糊，优先淘汰）
  PROBE        是否探测元数据，默认 1（0 关闭探测，退化为旧版随机直取）
  PROBE_CACHE  探测结果缓存秒数，默认 300
"""
import argparse
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT_DEFAULT = 8899
ALLOWED_HOSTS = {"api.yujn.cn"}
ACCESS_KEY = os.environ.get("ACCESS_TOKEN", "")   # 可选：设置后 /api/resolve 需带 ?key= 口令（公网部署防白嫖）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 国内 API 强制直连：绕过系统代理（代理会让请求慢 10 倍甚至失败）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------- 画质择优配置 ----------
CANDIDATES = int(os.environ.get("CANDIDATES", "3"))       # 并发候选数
MIN_KBPS = float(os.environ.get("MIN_KBPS", "1100"))      # 最低码率门槛
PROBE_ON = os.environ.get("PROBE", "1") != "0"            # 元数据探测开关
PROBE_TTL = int(os.environ.get("PROBE_CACHE", "300"))     # 探测缓存秒数
PROBE_STEPS = (65536, 262144, 1048576)                    # 渐进读取：moov 常在前 64KB 内
PROBE_TIMEOUT = 6

_probe_cache = {}          # url -> (expire_ts, meta)
_probe_lock = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=max(4, CANDIDATES * 2), thread_name_prefix="probe")


# ---------- MP4 元数据探测 ----------
def _range(url, start, end, timeout=PROBE_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Range": "bytes=%d-%d" % (start, end)})
    with OPENER.open(req, timeout=timeout) as r:
        return r.headers, r.read()


def file_size(url):
    """仅请求 2 字节拿总长度，比 HEAD 更稳（部分 CDN 不支持 HEAD）"""
    hd, _ = _range(url, 0, 1)
    cr = hd.get("Content-Range") or ""
    if "/" in cr:
        try:
            return int(cr.split("/")[-1])
        except ValueError:
            pass
    v = hd.get("Content-Length")
    try:
        return int(v) if v else None
    except ValueError:
        return None


def _dims(seg):
    """从 moov 段里找第一个视频轨的显示尺寸（tkhd 已含旋转矩阵，可直接判竖横屏）"""
    j = seg.find(b"trak")
    while j >= 0:
        k = seg.find(b"tkhd", j)
        if k < 0 or k > j + 400000:
            break
        if k < 4:
            break
        ln = struct.unpack(">I", seg[k - 4:k])[0]
        t = seg[k + 4:k + ln]
        if not t:
            break
        ver = t[0]
        w16 = 8 if ver == 1 else 4
        off = 4 + w16 * 4 + w16 + 16 + 36      # version+flags | 时间字段 | 保留+层+音量 | 矩阵
        if off + 8 <= len(t):
            w, h = struct.unpack(">II", t[off:off + 8])
            w, h = w >> 16, h >> 16            # 16.16 定点数
            if w > 100 and h > 100:            # 跳过音轨（0x0）
                return (w, h)
        j = seg.find(b"trak", j + 4)
    return None


def _duration(seg):
    k = seg.find(b"mvhd")
    if k < 0:
        return None
    t = seg[k + 4:]
    try:
        if t[0] == 1:
            ts, du = struct.unpack(">IQ", t[20:32])
        else:
            ts, du = struct.unpack(">II", t[12:20])
        return (du / ts) if ts else None
    except struct.error:
        return None


def probe_meta(url):
    """渐进读取文件头部，解析 moov 得到 分辨率/时长/码率。失败返回 None。"""
    now = time.time()
    with _probe_lock:
        hit = _probe_cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
    try:
        total = file_size(url)
        if not total:
            raise ValueError("no size")
        data = b""
        for b in PROBE_STEPS:
            end = min(b, total) - 1
            if end <= len(data):
                break
            _, chunk = _range(url, len(data), end)
            data += chunk
            # moov 完整落在已读区间内就收手
            mi = data.find(b"moov")
            if mi >= 0:
                size = struct.unpack(">I", data[mi - 4:mi])[0] if mi >= 4 else 0
                if size and mi + size <= len(data):
                    break
        mi = data.find(b"moov")
        if mi < 0:
            raise ValueError("no moov")
        seg = data[mi:min(len(data), mi + 400000)]
        wh = _dims(seg)
        dur = _duration(seg)
        meta = {
            "w": wh[0] if wh else None,
            "h": wh[1] if wh else None,
            "dur": round(dur, 1) if dur else None,
            "size": total,
            "kbps": int(total * 8 / dur / 1000) if (dur and dur > 0.5) else None,
        }
    except Exception:
        meta = None
    with _probe_lock:
        if len(_probe_cache) > 500:            # 简单防膨胀
            _probe_cache.clear()
        _probe_cache[url] = (now + PROBE_TTL, meta)
    return meta


def verify_url(u):
    """快速验证视频直链是否存活（拉取前 1KB，判断是否 video）"""
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Range": "bytes=0-1023"})
        with OPENER.open(req, timeout=4) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            return r.status in (200, 206) and "video" in ct
    except Exception:
        return False


def score(meta):
    """择优打分：码率为主，分辨率为辅；无元数据的候选取中间分，不至于被全灭"""
    if not meta:
        return 0.0
    kbps = meta.get("kbps")
    if not kbps:
        px = (meta.get("w") or 0) * (meta.get("h") or 0)
        return float(px) / 1000.0 if px else 0.0
    h = meta.get("h") or 0
    bonus = 1.15 if h >= 1080 else (1.0 if h >= 720 else 0.7)
    return kbps * bonus


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "XJJTheater/3.6"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        p = urllib.parse.urlparse(self.path).path
        if not p.startswith("/api/") and (
            p == "/" or p.endswith((".html", ".js", ".css")) or "." not in os.path.basename(p)
        ):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/health":
                self.send_json({"ok": True, "service": "xjj-theater",
                                "candidates": CANDIDATES, "minKbps": MIN_KBPS, "probe": PROBE_ON})
                return
            if parsed.path == "/api/resolve":
                self.api_resolve(urllib.parse.parse_qs(parsed.query))
                return
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            try:
                self.send_json({"ok": False, "error": str(e)}, 500)
            except Exception:
                self.close_connection = True

    def api_resolve(self, qs):
        """解析随机视频直链。
        - src=douyin&token=xx：istero 抖音近期小姐姐（可选，失败自动回落）
        - 默认：多候选并发解析 + 元数据探测，按码率择优

        可用参数：n=候选数  min=最低码率  probe=0 关闭探测
        """
        if ACCESS_KEY and (qs.get("key") or [""])[0] != ACCESS_KEY:
            self.send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        src = (qs.get("src") or [""])[0]
        token = (qs.get("token") or [""])[0]
        if src == "douyin" and token and self.try_istero(token):
            return
        self.resolve_best(qs)

    def try_istero(self, token):
        """istero 抖音近期视频（需免费 token）。成功返回 True 并已应答。"""
        try:
            req = urllib.request.Request(
                "https://api.istero.com/resource/v1/douyin/video/rand",
                headers={"User-Agent": UA, "Authorization": "Bearer " + token})
            with OPENER.open(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            if d.get("code") != 200:
                raise ValueError(d.get("message") or "code %s" % d.get("code"))
            u = (d.get("data") or {}).get("video") or ""
            if not u.startswith("http"):
                raise ValueError("no video url")
            meta = probe_meta(u) if PROBE_ON else None
            self.send_json({"ok": True, "url": u, "src": "douyin", "meta": meta})
            return True
        except Exception:
            return False   # 静默回落到 yujn

    # ---------- 多候选并发择优 ----------
    def _parse_int(self, qs, key, default, lo, hi):
        try:
            v = int((qs.get(key) or [default])[0])
            return max(lo, min(hi, v))
        except (ValueError, TypeError):
            return default

    def resolve_best(self, qs):
        n = self._parse_int(qs, "n", CANDIDATES, 1, 6)
        try:
            floor = float((qs.get("min") or [MIN_KBPS])[0])
        except (ValueError, TypeError):
            floor = MIN_KBPS
        probe = (qs.get("probe") or ["1"])[0] != "0" and PROBE_ON

        # 候选尽量分散到不同分类，避免三条都落在同一个池子里
        eps = (qs.get("u") or [])
        if not eps:
            self.send_json({"ok": False, "error": "missing u"}, 400)
            return
        hosts = {urllib.parse.urlparse(u).netloc.lower() for u in eps}
        if not hosts <= ALLOWED_HOSTS:
            self.send_json({"ok": False, "error": "url not allowed"}, 403)
            return
        if any(urllib.parse.urlparse(u).scheme != "https" for u in eps):
            self.send_json({"ok": False, "error": "https required"}, 403)
            return
        pool = list(eps)
        picked = []
        while len(picked) < n:
            if not pool:
                pool = list(eps)
            picked.append(pool.pop(0))

        def fetch(ep):
            """解析一个候选：拿直链 → 验活 → 探测元数据"""
            u = ep
            if "type=json" not in u:
                u = u + ("&" if "?" in u else "?") + "type=json"
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with OPENER.open(req, timeout=6) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                url = (data.get("data") or "").strip()
                if not url.lower().startswith("http"):
                    return None
                if not verify_url(url):
                    return None
                meta = probe_meta(url) if probe else None
                return {"url": url, "meta": meta, "count": data.get("video_count")}
            except Exception:
                return None

        results = []
        if n == 1:
            r = fetch(picked[0])
            if r:
                results.append(r)
        else:
            try:
                for r in POOL.map(fetch, picked):
                    if r:
                        results.append(r)
            except Exception:
                if not results and picked:
                    r = fetch(picked[0])
                    if r:
                        results.append(r)

        if not results:
            self.send_json({"ok": False, "error": "resolve failed"}, 502)
            return

        scored = sorted(results, key=lambda r: score(r["meta"]), reverse=True)
        # 达标者里挑最高分；全员不达标就退而求其次取最高分，避免死等
        passed = [r for r in scored
                  if (r["meta"] and r["meta"].get("kbps") and r["meta"]["kbps"] >= floor)]
        best = (passed or scored)[0]
        out = {"ok": True, "url": best["url"], "count": best.get("count"),
               "verified": True, "cands": len(results), "meta": best["meta"]}
        if passed:
            out["picked"] = "quality"
        else:
            out["picked"] = "fallback"
        self.send_json(out)


def main():
    global ACCESS_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="监听地址：本机 127.0.0.1，局域网/容器用 0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", PORT_DEFAULT)),
                    help="监听端口（默认 8899；可用环境变量 PORT 覆盖，便于 Docker 换端口）")
    ap.add_argument("--token", default="", help="可选：接口访问口令（也可用环境变量 ACCESS_TOKEN）")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    if args.token:
        ACCESS_KEY = args.token

    if not os.path.isfile(os.path.join(ROOT, "index.html")):
        print("[错误] 未找到 index.html", file=sys.stderr)
        sys.exit(1)

    url = "http://%s:%d/" % ("127.0.0.1" if args.host == "0.0.0.0" else args.host, args.port)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 56)
    print("  小姐姐放映厅 已启动  ✨  %s" % url)
    print("  监听 %s:%d%s" % (args.host, args.port, "（含访问口令）" if ACCESS_KEY else ""))
    print("  画质择优：并发 %d 候选 · 最低 %d kbps · 探测 %s"
          % (CANDIDATES, MIN_KBPS, "开" if PROBE_ON else "关"))
    print("  Ctrl+C 停止")
    print("=" * 56)
    if not args.no_open and args.host != "0.0.0.0":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
