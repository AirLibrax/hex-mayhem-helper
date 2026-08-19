"""配置管理：读写用户设置，数据源二选一"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import data_dir

APP_DIR = data_dir()
CONFIG_PATH = APP_DIR / "settings.json"
DB_PATH = APP_DIR / "stats_cache.db"

DEFAULT_SETTINGS = {
    "provider": "aramgg",          # aramgg | hexdata
    "aramgg_api_key": "",          # 覆盖 Key（留空用内置）
    "detect_monitor": 0,            # 符文检测屏幕：0=自动全部，N=指定物理屏
    "overlay_opacity": 0.92,       # 悬浮窗不透明度
    "overlay_x": 100,              # 悬浮窗位置
    "overlay_y": 100,
    "overlay_width": 360,
    "overlay_height": 300,
    "poll_interval": 1.0,          # LCU 轮询秒（载入画面窗口短，需快速捕获）
    "auto_refresh_hours": 24,      # 数据自动刷新间隔
}


@dataclass
class Settings:
    provider: str = "aramgg"
    aramgg_api_key: str = ""
    detect_monitor: int = 0
    overlay_opacity: float = 0.92
    overlay_x: int = 100
    overlay_y: int = 100
    overlay_width: int = 360
    overlay_height: int = 300
    poll_interval: float = 2.0
    auto_refresh_hours: int = 24

    @property
    def provider_label(self) -> str:
        return {"aramgg": "aramgg.com", "hexdata": "hexdata.com.cn"}.get(
            self.provider, self.provider
        )


class Config:
    """JSON 配置读写，容错：文件损坏时回退默认值"""

    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            self.save()
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = dict(DEFAULT_SETTINGS)
                merged.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
                self._data = merged
        except (json.JSONDecodeError, OSError):
            # 配置文件损坏，回退默认并重建
            self._data = dict(DEFAULT_SETTINGS)
            self.save()

    def save(self) -> None:
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, key: str):
        return self._data.get(key, DEFAULT_SETTINGS.get(key))

    def set(self, key: str, value) -> None:
        if key not in DEFAULT_SETTINGS:
            return
        self._data[key] = value
        self.save()

    def to_settings(self) -> Settings:
        s = Settings(**{k: v for k, v in self._data.items() if k in Settings.__dataclass_fields__})
        return s

    def apply(self, s: Settings) -> None:
        for k, v in asdict(s).items():
            if k in DEFAULT_SETTINGS:
                self._data[k] = v
        self.save()
