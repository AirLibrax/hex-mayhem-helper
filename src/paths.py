"""路径工具：资源定位与数据目录（源码/打包兼容，绿色便携）"""
from __future__ import annotations

import sys
from pathlib import Path


def resource_base() -> Path:
    """资源根目录：源码运行时为项目根，打包后为 PyInstaller 解压目录"""
    if getattr(sys, "frozen", False):  # PyInstaller 打包运行
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent  # src/ 的上级 = 项目根


def data_dir() -> Path:
    """数据目录（配置/缓存/日志/截图）：
    优先程序所在目录（绿色便携：整个文件夹可拷走，数据随行）；
    目录不可写（如解压在受保护位置）时回退用户目录。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".hmh_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        fallback = Path.home() / ".hex-mayhem-helper"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
