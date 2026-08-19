"""数据管理器：协调 Provider / 缓存 / 刷新调度 / 名称映射"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from ..config import Config
from .. import secrets
from . import PROVIDERS, create_provider
from .base import ProviderError
from .cache import StatsCache
from .models import ChampionStat, MatchupView

log = logging.getLogger(__name__)

DDragon_ZH_URL = (
    "https://ddragon.leagueoflegends.com/cdn/15.1.1/data/zh_CN/champion.json"
)


class DataManager:
    def __init__(self, config: Config, cache: StatsCache):
        self._config = config
        self._cache = cache
        self._zh_names: dict[int, str] = {}
        self._detail_loading: dict[int, bool] = {}
        self._lock = threading.Lock()
        self._load_zh_names()

    # ---------- 刷新 ----------
    def provider_name(self) -> str:
        return self._config.get("provider")

    def is_stale(self, kind: str = "champions") -> bool:
        hours = self._config.get("auto_refresh_hours") or 24
        ts = self._cache.updated_at(kind)
        if ts is None:
            return True
        return (time.time() - ts) > hours * 3600

    def refresh(self, provider: Optional[str] = None) -> tuple[int, int]:
        """同步刷新全部数据，返回 (英雄数, 符文数)"""
        name = provider or self.provider_name()
        prov = create_provider(name, secrets.resolve_api_key(self._config.get("aramgg_api_key") or ""))
        champions = prov.fetch_champions()
        augments = prov.fetch_augments()
        self._fill_zh_names(champions)
        self._cache.replace_champions(champions, name)
        self._cache.replace_augments(augments, name)
        return len(champions), len(augments)

    def refresh_async(self, provider: Optional[str] = None,
                      on_done=None, on_error=None) -> None:
        def run():
            try:
                n_c, n_a = self.refresh(provider)
                if on_done:
                    on_done(n_c, n_a)
            except Exception as e:
                log.exception("数据刷新失败")
                if on_error:
                    on_error(e)
        threading.Thread(target=run, daemon=True).start()

    def ensure_fresh(self, on_done=None, on_error=None) -> None:
        if self.is_stale("champions") or self.is_stale("augments"):
            log.info("数据过期，后台刷新")
            self.refresh_async(on_done=on_done, on_error=on_error)

    # ---------- 中文名 ----------
    def _load_zh_names(self) -> None:
        try:
            r = requests.get(DDragon_ZH_URL, timeout=10)
            r.raise_for_status()
            for cid, info in r.json()["data"].items():
                self._zh_names[int(info["key"])] = info["name"]
        except Exception:
            log.warning("DDragon 中文名加载失败，回退英文显示")

    def _fill_zh_names(self, champions: list[ChampionStat]) -> None:
        for c in champions:
            if not c.name_zh and c.champion_id in self._zh_names:
                c.name_zh = self._zh_names[c.champion_id]

    def champion_display_name(self, c: ChampionStat) -> str:
        return c.name_zh or c.name_en or f"#{c.champion_id}"

    # ---------- 查询 ----------
    def get_champion(self, champion_id: int) -> Optional[ChampionStat]:
        return self._cache.get_champion(champion_id)

    def get_matchup(self, my_ids: list[int], their_ids: list[int],
                    bench_ids: Optional[list[int]] = None) -> MatchupView:
        mine = [self.get_champion(i) for i in my_ids]
        theirs = [self.get_champion(i) for i in their_ids]
        mine = [c for c in mine if c]
        theirs = [c for c in theirs if c]
        bench = [self.get_champion(i) for i in (bench_ids or [])]
        bench = [c for c in bench if c]
        patch = self._cache.provider_of("champions") or ""
        return MatchupView(my_team=mine, their_team=theirs,
                           bench_team=bench, patch=patch)

    def lookup_augment(self, name: str):
        return self._cache.find_augment_by_name(name)

    def match_ocr_text(self, text: str):
        """OCR 文本 -> AugmentStat：鲁棒匹配

        OCR 有字符级噪声（错字/标点变体），策略：
        1. 预处理：去空格、去所有标点，只保留中文字符/数字/字母
        2. 子串匹配：预处理后库名包含于 OCR 文本
        3. 编辑距离兜底：OCR 前缀与库名相似度 > 0.55 命中
        """
        import difflib
        import re

        def _norm(s: str) -> str:
            return re.sub(r"[\W_]+|\s", "", s or "")

        clean = _norm(text)
        if not clean:
            return None
        augs = self._cache.get_augments()
        names = [(aug, _norm(aug.name_zh)) for aug in augs]
        names = [(a, n) for a, n in names if n]

        # 1) 子串匹配（取最早出现、同位置取最长）
        best = None  # (位置, 名称长度, aug)
        for aug, name in names:
            if name not in clean:
                continue
            pos = clean.index(name)
            if best is None or pos < best[0] or (pos == best[0] and len(name) > best[1]):
                best = (pos, len(name), aug)
        if best:
            return best[2]

        # 2) 编辑距离兜底：OCR 前 8 字与库名（标题一般 4~8 字）
        prefix = clean[:8]
        best_score, best_aug = 0.0, None
        for aug, name in names:
            ratio = difflib.SequenceMatcher(None, prefix, name).ratio()
            if ratio > 0.55 and ratio > best_score:
                best_score, best_aug = ratio, aug
        if best_aug:
            log.info("OCR 模糊匹配: %r -> %s (%.2f)", text[:20], best_aug.name_zh, best_score)
        return best_aug

    def get_augment_by_id(self, augment_id: str):
        return self._cache.get_augment(augment_id)

    # ---------- 单英雄详情（海克斯/装备推荐） ----------
    def get_champion_detail(self, champion_id: int):
        return self._cache.get_champion_detail(champion_id)

    def ensure_champion_detail(self, champion_id: int, force: bool = False):
        """缓存有且 patch 一致则直接返回；否则后台拉取详情并缓存。
        返回 (detail_or_None, is_loading)  —— loading 表示后台拉取中，稍后查缓存。"""
        cached = self._cache.get_champion_detail(champion_id)
        current_patch = self._current_patch()
        if cached and not force and (not current_patch or cached.patch == current_patch):
            return cached, False
        if not self._detail_loading.get(champion_id):
            self._detail_loading[champion_id] = True
            threading.Thread(
                target=self._fetch_detail_worker, args=(champion_id,), daemon=True
            ).start()
            return cached, True
        return cached, False

    def _current_patch(self) -> str:
        rows = self._cache.get_champions()
        return rows[0].patch if rows else ""

    def _fetch_detail_worker(self, champion_id: int) -> None:
        try:
            prov = create_provider("aramgg", secrets.resolve_api_key(self._config.get("aramgg_api_key") or ""))
            detail = prov.fetch_champion_detail(champion_id)
            self._cache.save_champion_detail(detail)
            log.info("单英雄详情已缓存: cid=%s patch=%s", champion_id, detail.patch)
        except Exception as e:
            log.warning("单英雄详情拉取失败 cid=%s: %s", champion_id, e)
        finally:
            self._detail_loading.pop(champion_id, None)

    def get_build_recommendation(self, champion_id: int):
        """返回主流派 BuildRec（按场次排序第一个）"""
        detail = self._cache.get_champion_detail(champion_id)
        if not detail or not detail.builds:
            return None
        return detail.builds[0]

    def all_augments(self):
        return self._cache.get_augments()
