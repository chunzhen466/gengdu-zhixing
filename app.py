# -*- coding: utf-8 -*-
"""app.py —— 「耕读知行」智能体 Web 服务（仅用标准库）

启动：python app.py
默认地址：http://127.0.0.1:8848
测试账号：见 config.json 的 auth 段

接口一览
  GET  /                     对话界面
  POST /api/login            测试账号登录，返回会话令牌
  POST /api/chat             对话（需令牌）
  GET  /api/plots            全部地块实时数据（需令牌）
  GET  /api/plots/<plot_id>  单块地判读（需令牌）
  POST /api/ingest           设备数据上报（ESP32 → 本接口，需设备密钥）
  GET  /api/health           健康检查与统计（公开）
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from agent_core import Agent, load_config
from kb import KnowledgeBase
from tools import IoTStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

CONFIG = load_config()
SERVER_CFG = CONFIG.get("server", {})
AUTH_CFG = CONFIG.get("auth", {})
WORKSPACE = CONFIG.get("workspace", {})
DEVICE_KEY = CONFIG.get("device_key", "gengdu-device-2026")


def public_workspace() -> Dict[str, Any]:
    """去掉以 _ 开头的注释字段，避免把配置说明暴露到前端。"""
    return {k: v for k, v in (WORKSPACE or {}).items() if not k.startswith("_")}

KB = KnowledgeBase(os.path.join(BASE_DIR, "knowledge"))
STORE = IoTStore()
AGENT = Agent(KB, STORE, CONFIG)

# 会话令牌（内存态，重启即失效——演示场景足够，且避免误留凭据）
TOKENS: Dict[str, float] = {}
TOKEN_TTL = 12 * 3600
_LOCK = threading.Lock()


def issue_token() -> str:
    token = secrets.token_urlsafe(24)
    with _LOCK:
        TOKENS[token] = time.time() + TOKEN_TTL
    return token


def valid_token(token: Optional[str]) -> bool:
    if not token:
        return False
    with _LOCK:
        exp = TOKENS.get(token)
        if exp is None:
            return False
        if exp < time.time():
            TOKENS.pop(token, None)
            return False
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "GengduZhixing/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, payload, "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"ok": False, "error": message}, code)

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _token(self) -> Optional[str]:
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return self.headers.get("X-Token")

    def _authed(self) -> bool:
        if valid_token(self._token()):
            return True
        self._error(401, "未登录或会话已过期，请重新登录")
        return False

    def log_message(self, fmt: str, *args) -> None:      # 精简日志
        return

    # ---------- 静态资源 ----------
    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        # 只允许 web 目录下的文件
        target = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(target):
            self._error(404, "页面不存在")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        try:
            with open(target, "rb") as fh:
                data = fh.read()
        except OSError:
            self._error(500, "读取文件失败")
            return
        self._send(200, data, ctype)

    # ---------- 路由 ----------
    def do_GET(self) -> None:                                    # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/health":
                self._json({
                    "ok": True, "service": "耕读知行智能体",
                    "knowledge": KB.stats(), "iot": STORE.stats(),
                    "llm": "enabled" if AGENT.llm.enabled else "template-mode",
                })
            elif path == "/api/plots":
                if not self._authed():
                    return
                self._json({"ok": True, "plots": STORE.list_plots()})
            elif path.startswith("/api/plots/"):
                if not self._authed():
                    return
                plot_id = path.rsplit("/", 1)[-1]
                self._json(STORE.get_plot_data(plot_id))
            elif path == "/api/meta":
                self._json({"ok": True, "workspace": public_workspace(),
                            "llm_enabled": AGENT.llm.enabled,
                            "knowledge": KB.stats()})
            else:
                self._error(404, "接口不存在")
        except Exception:                                        # noqa: BLE001
            traceback.print_exc()
            self._error(500, "服务端内部错误")

    def do_POST(self) -> None:                                   # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                self._login()
            elif path == "/api/chat":
                self._chat()
            elif path == "/api/ingest":
                self._ingest()
            else:
                self._error(404, "接口不存在")
        except Exception:                                        # noqa: BLE001
            traceback.print_exc()
            self._error(500, "服务端内部错误")

    # ---------- 具体业务 ----------
    def _login(self) -> None:
        data = self._body()
        user = str(data.get("username", "")).strip()
        pwd = str(data.get("password", "")).strip()
        if not AUTH_CFG:
            self._json({"ok": True, "token": issue_token(), "workspace": public_workspace(), "note": "未配置鉴权"})
            return
        if user == str(AUTH_CFG.get("username", "")) and \
                pwd == str(AUTH_CFG.get("password", "")):
            self._json({"ok": True, "token": issue_token(),
                        "workspace": public_workspace()})
        else:
            self._error(401, "账号或密码不正确")

    def _chat(self) -> None:
        if not self._authed():
            return
        data = self._body()
        message = str(data.get("message", "")).strip()
        history = data.get("history") or []
        if not isinstance(history, list):
            history = []
        if len(message) > 2000:
            self._error(400, "问题太长了，请精简到 2000 字以内")
            return
        result = AGENT.chat(message, history)
        result["ok"] = True
        self._json(result)

    def _ingest(self) -> None:
        """ESP32 等设备上报。请求头 X-Device-Key 必须与 config.json 一致。"""
        if (self.headers.get("X-Device-Key") or "") != DEVICE_KEY:
            self._error(403, "设备密钥错误")
            return
        data = self._body()
        plot_id = str(data.get("plot_id", "")).strip()
        if not plot_id:
            self._error(400, "缺少 plot_id")
            return
        reading = STORE.ingest(
            plot_id=plot_id,
            soil=data.get("soil"), temp=data.get("temp"),
            light=data.get("light"), battery=data.get("battery"),
            device=str(data.get("device", "esp32")),
        )
        self._json({"ok": True, "received": reading.as_dict()})


def main() -> None:
    # 云平台（Render / Railway / 阿里云等）通过环境变量注入端口，
    # 优先读 PORT，本地启动则回落到 config.json
    host = str(os.environ.get("HOST") or SERVER_CFG.get("host", "0.0.0.0"))
    port = int(os.environ.get("PORT") or SERVER_CFG.get("port", 8848))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    public = os.environ.get("PUBLIC_URL")
    print("=" * 62)
    print("  耕读知行 · 校园劳动实践物联网智能体")
    print("=" * 62)
    print(f"  知识库 : {KB.stats()['chunks']} 个小节 / "
          f"{KB.stats()['documents']} 篇文档")
    print(f"  感知层 : {STORE.stats()['plots']} 块地，"
          f"{STORE.stats()['real_devices']} 台真实设备在线")
    print(f"  大模型 : {'已接入 ' + AGENT.llm.model if AGENT.llm.enabled else '未接入（模板模式，功能可用）'}")
    print(f"  监听   : {host}:{port}")
    print(f"  访问   : {public or f'http://127.0.0.1:{port}'}")
    print(f"  测试账号: {AUTH_CFG.get('username','—')} / {AUTH_CFG.get('password','—')}")
    print("=" * 62)
    print("  按 Ctrl+C 停止服务")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
