"""路径工具：兼容源码运行与 PyInstaller 打包后的资源定位"""
from __future__ import annotations

import sys
from pathlib import Path


def resource_base() -> Path:
    """资源根目录：源码运行时为项目根，打包后为 PyInstaller 解压目录"""
    if getattr(sys, "frozen", False):  # PyInstaller 打包运行
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent  # src/ 的上级 = 项目根
