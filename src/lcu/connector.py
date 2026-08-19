"""LCU 连接：读取客户端凭据、HTTP 请求、对局阶段监听

国服客户端通过 WeGame 启动，lockfile 文件方案优先，失败则扫描进程命令行兜底。
只读接口，不注入、不修改，与反作弊检测面无交集。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import psutil
import requests
import ssl
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

log = logging.getLogger(__name__)

CLIENT_PROCESS_NAME = "LeagueClientUx.exe"


class _LCUAdapter(HTTPAdapter):
    """LCU 专用 TLS 适配：客户端本地服务基于老版 CEF，TLS 实现旧。
    Python 3.10+ 默认 TLS1.2+ 协商/证书解析会报 ASN1 NOT_ENOUGH_DATA，
    这里放宽到 TLS1.0~1.2 并关闭证书校验。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


class GamePhase(str, Enum):
    NONE = "None"
    LOBBY = "Lobby"
    MATCHMAKING = "Matchmaking"
    READY_CHECK = "ReadyCheck"
    CHAMP_SELECT = "ChampSelect"
    GAME_START = "GameStart"
    IN_PROGRESS = "InProgress"
    END_OF_GAME = "EndOfGame"
    WAITING_FOR_STATS = "WaitingForStats"
    PRE_END_OF_GAME = "PreEndOfGame"


@dataclass
class LcuCredential:
    port: int
    token: str
    protocol: str = "https"


def _find_client_process() -> Optional[psutil.Process]:
    """按名称匹配客户端进程（放宽：名称含 LeagueClient 即命中，覆盖国际服/国服变体）"""
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "leagueclient" in name:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


_LOCKFILE_PATHS = [
    # 常见安装目录（国际服/官方启动器）
    r"C:\Riot Games\League of Legends\lockfile",
    r"C:\Program Files\Riot Games\League of Legends\lockfile",
    r"D:\Riot Games\League of Legends\lockfile",
    r"D:\Program Files\Riot Games\League of Legends\lockfile",
    r"E:\Riot Games\League of Legends\lockfile",
    # 国服 WeGame 常见目录
    r"C:\Program Files\WeGame\apps\League of Legends\lockfile",
    r"C:\WeGame\apps\League of Legends\lockfile",
    r"D:\WeGame\apps\League of Legends\lockfile",
]


def _read_lockfile() -> Optional[LcuCredential]:
    """方案一：读取客户端根目录 lockfile（name:pid:port:password:protocol）
    优先进程 exe 同目录，其次常见安装路径（WeGame 沙箱下进程路径可能读不到）。"""
    candidates: list[Path] = []
    proc = _find_client_process()
    if proc is not None:
        try:
            candidates.append(Path(proc.exe()).parent / "lockfile")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    for p in _LOCKFILE_PATHS:
        candidates.append(Path(p))
    for lockfile in candidates:
        try:
            text = lockfile.read_text(encoding="utf-8", errors="ignore").strip()
            parts = text.split(":")
            if len(parts) >= 5 and not parts[0].startswith("--"):
                # 标准格式: name:pid:port:password:protocol
                return LcuCredential(
                    port=int(parts[2]), token=parts[3], protocol=parts[4]
                )
        except (OSError, ValueError):
            continue
    return None


def _read_commandline() -> Optional[LcuCredential]:
    """方案二：扫描进程命令行里的 --app-port / --remoting-auth-token"""
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "leagueclient" not in name:
                continue
            cmdline = proc.info["cmdline"] or []
            port = token = None
            for arg in cmdline:
                m = re.search(r"--app-port=(\d+)", arg)
                if m:
                    port = int(m.group(1))
                m = re.search(r"--remoting-auth-token=([\w\-_]+)", arg)
                if m:
                    token = m.group(1)
            if port and token:
                return LcuCredential(port=port, token=token)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def discover_credential() -> Optional[LcuCredential]:
    return _read_lockfile() or _read_commandline()


class LcuClient:
    """封装对客户端本地接口的 HTTP 访问"""

    def __init__(self):
        self._cred: Optional[LcuCredential] = None
        self._session = requests.Session()
        self._session.verify = False  # 本地自签证书（需与自定义 SSL 上下文一致）
        self._session.mount("https://", _LCUAdapter())

    # ---------- 连接 ----------
    def connect(self) -> bool:
        cred = discover_credential()
        if cred is None:
            self._cred = None
            if not getattr(self, "_warned_nocred", False):
                log.warning("未发现 LCU 凭据（客户端未运行？）")
                self._warned_nocred = True
            return False
        self._warned_nocred = False
        self._cred = cred
        self._session.auth = ("riot", cred.token)
        try:
            self.get("/lol-summoner/v1/current-summoner")
            self._warned_auth = False
            return True
        except Exception as e:
            if not getattr(self, "_warned_auth", False):
                log.warning("LCU 凭据验证失败: %s", e)
                self._warned_auth = True
            self._cred = None
            return False

    @property
    def connected(self) -> bool:
        return self._cred is not None

    # ---------- HTTP ----------
    def _url(self, path: str) -> str:
        if not self._cred:
            raise ConnectionError("LCU 未连接")
        return f"{self._cred.protocol}://127.0.0.1:{self._cred.port}{path}"

    def get(self, path: str, timeout: float = 2.0):
        r = self._session.get(self._url(path), timeout=timeout)
        r.raise_for_status()
        if not r.content:
            return None
        return r.json()

    # ---------- 业务数据 ----------
    def current_phase(self) -> str:
        try:
            data = self.get("/lol-gameflow/v1/gameflow-phase")
            return data or GamePhase.NONE.value
        except Exception:
            return GamePhase.NONE.value

    def current_summoner(self) -> Optional[dict]:
        try:
            return self.get("/lol-summoner/v1/current-summoner")
        except Exception:
            return None

    def champ_select_session(self) -> Optional[dict]:
        """选人阶段会话：含 myTeam/theirTeam 及各自 championId"""
        try:
            return self.get("/lol-champ-select/v1/session")
        except Exception:
            return None

    def gameflow_session(self) -> Optional[dict]:
        """对局会话：游戏中含 gameData.players（双方玩家+英雄+队伍）"""
        try:
            return self.get("/lol-gameflow/v1/session")
        except Exception:
            return None

    # ---------- 监听 ----------
    def poll_phase(self, callback: Callable[[str, "LcuClient"], None],
                   interval: float, stop_event: threading.Event) -> None:
        """轮询对局阶段，阶段变化时回调。独立线程运行。"""
        last_phase = None
        while not stop_event.is_set():
            phase = self.current_phase()
            if phase != last_phase:
                log.info("对局阶段变化: %s -> %s", last_phase, phase)
                last_phase = phase
                try:
                    callback(phase, self)
                except Exception:
                    log.exception("阶段回调异常")
            stop_event.wait(interval)
