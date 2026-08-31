#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小姐姐放映厅 · 本地服务（零依赖，Python 3.7+）
作用：
  1. 托管本目录静态页面（index.html）
  2. /api/resolve  预解析随机视频的最终直链（服务端请求 JSON，绕过浏览器 CORS）
用法：
  python server.py            # 默认 127.0.0.1:8899 并自动打开浏览器
  python server.py --port 9000 --no-open
"""
import argparse
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT_DEFAULT = 8899
ALLOWED_HOSTS = {"api.yujn.cn"}
ACCESS_KEY = os.environ.get("ACCESS_TOKEN", "")   # 可选：设置后 /api/resolve 需带 ?key= 口令（公网部署防白嫖）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 国内 API 强制直连：绕过系统代理（代理会让请求慢 10 倍甚至失败）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def verify_url(u):
    """快速验证视频直链是否存活（拉取前 1KB，判断是否 video）"""
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Range": "bytes=0-1023"})
        with OPENER.open(req, timeout=4) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            return r.status in (200, 206) and "video" in ct
    except Exception:
        return False


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "XJJTheater/2.0"

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
                self.send_json({"ok": True, "service": "xjj-theater"})
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
        - 默认：遇见 API 的 JSON 形态（多分类）
        - src=douyin&token=xx：istero 抖音近期小姐姐（可选，失败自动回落）"""
        if ACCESS_KEY and (qs.get("key") or [""])[0] != ACCESS_KEY:
            self.send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        src = (qs.get("src") or [""])[0]
        token = (qs.get("token") or [""])[0]
        if src == "douyin" and token and self.try_istero(token):
            return
        self.resolve_yujn(qs)

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
            if not verify_url(u):
                raise ValueError("video dead")
            self.send_json({"ok": True, "url": u, "src": "douyin"})
            return True
        except Exception:
            return False   # 静默回落到 yujn

    def resolve_yujn(self, qs):
        """遇见 API 解析 + 快速验活（死链重取 1 次；总耗时优先，漏网由前端超时兜底）"""
        u = (qs.get("u") or [""])[0]
        host = urllib.parse.urlparse(u).netloc.lower()
        if urllib.parse.urlparse(u).scheme != "https" or host not in ALLOWED_HOSTS:
            self.send_json({"ok": False, "error": "url not allowed"}, 403)
            return
        if "type=json" not in u:
            u = u + ("&" if "?" in u else "?") + "type=json"
        last_url = ""
        last_err = "resolve failed"
        for attempt in range(2):
            try:
                if attempt:
                    import time
                    time.sleep(0.3)
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with OPENER.open(req, timeout=6) as r:
                    body = r.read().decode("utf-8", "ignore")
                data = json.loads(body)
                url = (data.get("data") or "").strip()
                if not url or not url.lower().startswith("http"):
                    raise ValueError("bad data")
                last_url = url
                if verify_url(url):
                    self.send_json({"ok": True, "url": url, "count": data.get("video_count"), "verified": True})
                    return
            except Exception as e:
                last_err = str(e)
                last_url = ""
        if last_url:
            self.send_json({"ok": True, "url": last_url, "verified": False})
        else:
            self.send_json({"ok": False, "error": last_err}, 502)


def main():
    global ACCESS_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="监听地址：本机 127.0.0.1，局域网/容器用 0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
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
    print("=" * 50)
    print("  小姐姐放映厅 已启动  ✨  %s" % url)
    print("  监听 %s:%d%s" % (args.host, args.port, "（含访问口令）" if ACCESS_KEY else ""))
    print("  Ctrl+C 停止")
    print("=" * 50)
    if not args.no_open and args.host != "0.0.0.0":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
