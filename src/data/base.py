"""数据源抽象接口：统一契约，两个 Provider 实现同一接口"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import AugmentStat, ChampionStat


class ProviderError(Exception):
    pass


class StatsProvider(ABC):
    """胜率数据源。实现方负责抓取并解析各自站点。"""

    name: str = ""
    display_name: str = ""

    @abstractmethod
    def fetch_champions(self) -> list[ChampionStat]:
        """抓取全部英雄在该模式下的胜率"""

    @abstractmethod
    def fetch_augments(self) -> list[AugmentStat]:
        """抓取全部海克斯符文胜率"""
