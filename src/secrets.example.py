"""API Key 配置示例（无内置 Key 的安全版本）

使用方式（三选一）：
1. 设置环境变量 HEX_MAYHEM_API_KEY=<你的 Key>
2. 复制本文件为 secrets.py 并填入 Key
3. 在软件设置的"aramgg API Key"输入框填写（持久化到本机配置）
"""
from __future__ import annotations

import os

# 无内置 Key：公开部署时保持为空，Key 从环境变量/设置读取
DEFAULT_API_KEY = ""


def resolve_api_key(override: str = "") -> str:
    if override and override.strip():
        return override.strip()
    env = os.environ.get("HEX_MAYHEM_API_KEY", "")
    if env and env.strip():
        return env.strip()
    return DEFAULT_API_KEY
