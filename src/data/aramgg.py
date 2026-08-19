"""aramgg.com 官方数据 API Provider

接入 aramgg 作者开放的正式数据接口（data.dtodo.cn）：
- 鉴权: Bearer hx_live_... (API Key，开发者后台生成)
- 免费额度: 200 credits/天，champions.json + augments.json 各 1 credit
- 中文数据，英雄榜来自腾讯国服快照，符文含 winRate/tier/登场率
- winRate 为 0-1 小数，此处统一乘 100 存入模型
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from .base import ProviderError, StatsProvider
from .models import (
    AugmentRec, AugmentStat, AugmentTrio, BuildRec, ChampionDetail,
    ChampionStat, ItemInfo,
)

log = logging.getLogger(__name__)

API_BASE = "https://data.dtodo.cn/api/v1"
LOCALE = "zh-CN"
DEVELOPER_URL = "https://data.dtodo.cn/developer.html"
TIMEOUT = 60
MAX_RETRY = 2


class AramggProvider(StatsProvider):
    name = "aramgg"
    display_name = "aramgg.com（官方 API）"

    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or "").strip()

    def _get_json(self, path: str) -> dict:
        if not self.api_key:
            raise ProviderError(
                f"未配置 aramgg API Key。请到 {DEVELOPER_URL} 用 GitHub 登录生成，"
                "再填入设置（免费 200 credits/天）"
            )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRY + 1):
            try:
                r = requests.get(API_BASE + path, headers=headers, timeout=TIMEOUT)
                break
            except requests.RequestException as e:
                last_err = e
                if attempt < MAX_RETRY:
                    time.sleep(2 * (attempt + 1))
        else:
            raise ProviderError(f"aramgg API 请求失败: {last_err}")
        if r.status_code == 401:
            raise ProviderError("aramgg API Key 无效，请检查设置（可到开发者后台重新生成）")
        if r.status_code == 429:
            raise ProviderError("aramgg API 额度不足（免费 200 credits/天），请明天再试或检查用量")
        if r.status_code != 200:
            raise ProviderError(f"aramgg API 错误 {r.status_code}: {r.text[:200]}")
        return r.json()

    # ---------- 英雄 ----------
    def fetch_champions(self) -> list[ChampionStat]:
        env = self._get_json(f"/{LOCALE}/champions.json")
        items: list[ChampionStat] = []
        for c in env.get("data") or []:
            stats = c.get("stats")
            items.append(ChampionStat(
                champion_id=int(c["id"]),
                name_zh=c.get("name") or "",
                name_en=c.get("alias") or "",
                win_rate=round(stats["winRate"] * 100, 2) if stats and stats.get("winRate") is not None else 0.0,
                tier=_tier_label(stats.get("tier")) if stats else "",
                pick_rate=round((stats.get("pickRate") or 0.0) * 100, 2) if stats and stats.get("pickRate") is not None else None,
                sample=stats.get("games") if stats else None,
                patch=stats.get("gamePatch") or "" if stats else "",
            ))
        if not items:
            raise ProviderError("aramgg API 英雄榜为空")
        return items

    # ---------- 符文 ----------
    def fetch_augments(self) -> list[AugmentStat]:
        env = self._get_json(f"/{LOCALE}/augments.json")
        items: list[AugmentStat] = []
        for a in env.get("data") or []:
            stats = a.get("stats")
            if not stats or not stats.get("statsAvailable", True):
                continue
            win_rate = round((stats.get("winRate") or 0.0) * 100, 2)
            pick_rate = round((stats.get("pickRate") or 0.0) * 100, 2) if stats.get("pickRate") is not None else None
            items.append(AugmentStat(
                augment_id=str(a["id"]),
                name_zh=a.get("name") or "",
                name_en="",
                win_rate=win_rate,
                tier=_tier_label(stats.get("tier")),
                pick_rate=pick_rate,
                sample=stats.get("games"),
                patch=stats.get("gamePatch") or "",
                icon_url=a.get("iconUrl") or "",
            ))
        if not items:
            raise ProviderError("aramgg API 符文榜为空（statsAvailable 全为 false？）")
        return items


    # ---------- 单英雄详情 ----------
    def fetch_champion_detail(self, champion_id: int) -> ChampionDetail:
        """拉取单英雄详情：海克斯推荐（rank 排序）+ 出装推荐（builds）"""
        env = self._get_json(f"/{LOCALE}/champions/{champion_id}.json")
        data = env.get("data") or {}

        augments: list[AugmentRec] = []
        for a in data.get("augments") or []:
            stats = a.get("stats") or {}
            rank = stats.get("rank")
            if rank is None:
                continue
            wr = stats.get("winRate")
            augments.append(AugmentRec(
                augment_id=str(a.get("id")),
                name_zh=a.get("name") or "",
                rarity_name=a.get("rarityName") or "",
                rank=int(rank),
                win_rate=round(wr * 100, 2) if wr is not None else None,
                pick_rate=round((stats.get("pickRate") or 0.0) * 100, 2) if stats.get("pickRate") is not None else None,
                icon_url=a.get("iconUrl") or "",
            ))
        augments.sort(key=lambda x: x.rank)

        builds: list[BuildRec] = []
        for b in data.get("builds") or []:
            stats = b.get("stats") or {}
            tags = b.get("tags") or {}
            tag_text = ", ".join(str(v) for v in tags.values()) if isinstance(tags, dict) else str(tags)
            builds.append(BuildRec(
                tags=tag_text,
                games=stats.get("games") or 0,
                win_rate=round((stats.get("winRate") or 0.0) * 100, 2),
                core_items=_parse_items(b.get("coreItems")),
                starting_items=_parse_items(b.get("startingItems")),
                situational_items=_parse_items_flat(b.get("situationalItems")),
                summoner_spells=_parse_spells(b.get("summonerSpells")),
            ))
        builds.sort(key=lambda x: x.games, reverse=True)

        trios: list[AugmentTrio] = []
        for t in data.get("augmentTrios") or []:
            stats = t.get("stats") or {}
            trios.append(AugmentTrio(
                augment_ids=[str(i) for i in (t.get("augmentIds") or [])],
                names=[a.get("name") or "" for a in (t.get("augments") or [])],
                win_rate=round((stats.get("winRate") or 0.0) * 100, 2),
                games=t.get("games") or 0,
            ))
        trios.sort(key=lambda x: x.win_rate, reverse=True)

        champion = data.get("champion") or {}
        stats = champion.get("stats") or {}
        return ChampionDetail(
            champion_id=champion_id,
            patch=stats.get("gamePatch") or "",
            augments=augments,
            builds=builds,
            augment_trios=trios,
        )


def _parse_items(groups: Optional[list]) -> list[ItemInfo]:
    """coreItems/startingItems：按组数组（取第一组）或直接解析"""
    if not groups:
        return []
    first = groups[0]
    if isinstance(first, dict) and "items" in first:
        items = first.get("items") or []
    elif isinstance(first, dict) and "name" in first:
        items = groups
    else:
        return []
    return [ItemInfo(item_id=i.get("id") or 0, name=i.get("name") or "",
                     icon_url=i.get("iconUrl") or "") for i in items]


def _parse_items_flat(items: Optional[list]) -> list[ItemInfo]:
    """situationalItems：扁平数组"""
    if not items:
        return []
    return [ItemInfo(item_id=i.get("id") or 0, name=i.get("name") or "",
                     icon_url=i.get("iconUrl") or "") for i in items if isinstance(i, dict)]


def _parse_spells(groups: Optional[list]) -> list[str]:
    """summonerSpells：取第一组技能名"""
    if not groups:
        return []
    first = groups[0]
    spells = first.get("spells") if isinstance(first, dict) else None
    if not spells:
        return []
    return [s.get("name") or "" for s in spells]


def _tier_label(tier) -> str:
    """API tier 为数字 1~5（1 最强）；旧数据可能是 S+/S 等字母档"""
    if tier is None:
        return ""
    if isinstance(tier, (int, float)):
        return f"T{int(tier)}"
    return str(tier)
