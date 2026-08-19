"""数据模型：英雄/符文胜率条目"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ChampionStat:
    champion_id: int            # 游戏内数字 ID，跨数据源对齐主键
    name_zh: str = ""
    name_en: str = ""
    win_rate: float = 0.0       # 0~100
    tier: str = ""              # S+ ~ D 或中文档位
    sample: Optional[int] = None
    patch: str = ""


@dataclass
class AugmentStat:
    augment_id: str             # 数据源内部 ID（可用作图标文件名）
    name_zh: str = ""
    name_en: str = ""
    win_rate: float = 0.0
    tier: str = ""
    sample: Optional[int] = None
    patch: str = ""
    icon_url: str = ""         # 符文图标 CDN 地址（模板匹配用）


@dataclass
class ItemInfo:
    """装备条目"""
    item_id: int = 0
    name: str = ""
    icon_url: str = ""


@dataclass
class AugmentRec:
    """单英雄海克斯推荐（官方 rank 顺序）"""
    augment_id: str = ""
    name_zh: str = ""
    rarity_name: str = ""       # silver/gold/prismatic
    rank: int = 0                # 1=最优先（腾讯官方顺序）
    win_rate: Optional[float] = None
    pick_rate: Optional[float] = None
    icon_url: str = ""


@dataclass
class BuildRec:
    """一套推荐出装流派"""
    tags: str = ""
    games: int = 0
    win_rate: float = 0.0
    core_items: list[ItemInfo] = None        # 核心三件套
    starting_items: list[ItemInfo] = None    # 出门装
    situational_items: list[ItemInfo] = None  # 情境装
    summoner_spells: list[str] = None         # 召唤师技能名


@dataclass
class AugmentTrio:
    """三强化组合（aramgg 客户端样本）"""
    augment_ids: list[str] = None
    names: list[str] = None
    win_rate: float = 0.0
    games: int = 0


@dataclass
class ChampionDetail:
    """单英雄详情：海克斯推荐 + 出装推荐"""
    champion_id: int = 0
    patch: str = ""
    augments: list[AugmentRec] = None       # 按 rank 升序
    builds: list[BuildRec] = None           # 按场次降序
    augment_trios: list[AugmentTrio] = None


@dataclass
class TeamLineup:
    """一方的 5 个英雄"""
    champions: list[ChampionStat] = None

    @property
    def avg_win_rate(self) -> float:
        if not self.champions:
            return 0.0
        return sum(c.win_rate for c in self.champions) / len(self.champions)


@dataclass
class MatchupView:
    """双方阵容胜率对比（悬浮窗核心数据）"""
    my_team: list[ChampionStat] = None
    their_team: list[ChampionStat] = None
    patch: str = ""

    @property
    def my_avg(self) -> float:
        return _avg(self.my_team)

    @property
    def their_avg(self) -> float:
        return _avg(self.their_team)


def _avg(items: Optional[list[ChampionStat]]) -> float:
    if not items:
        return 0.0
    return sum(i.win_rate for i in items) / len(items)
