"""hexdata.com.cn Provider：中文数据站，百万级样本

注意：完整列表页可能由 JS 渲染，requests 直接抓 HTML 时若拿不到数据，
会降级探测内嵌 JSON；仍失败则抛 ProviderError 提示用户切换数据源。
符文页路径未公开，按候选路径探测。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .base import ProviderError, StatsProvider
from .models import AugmentStat, ChampionStat

log = logging.getLogger(__name__)

BASE = "https://hexdata.com.cn"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TIMEOUT = 15

AUGMENT_PATH_CANDIDATES = ["/augments", "/augment", "/hextech", "/runes"]


class HexdataProvider(StatsProvider):
    name = "hexdata"
    display_name = "hexdata.com.cn"

    def _get(self, path: str) -> str:
        try:
            r = requests.get(BASE + path, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            raise ProviderError(f"hexdata.com.cn 请求失败: {e}") from e

    # ---------- 英雄 ----------
    def fetch_champions(self) -> list[ChampionStat]:
        html = self._get("/heroes")
        items = self._parse_champions_from_html(html)
        if not items:
            raise ProviderError(
                "hexdata.com.cn 英雄榜为动态渲染，无法直接抓取。请改用 aramgg.com 数据源"
            )
        return items

    def _parse_champions_from_html(self, html: str) -> list[ChampionStat]:
        items: list[ChampionStat] = []
        seen: set[int] = set()
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            links = [a for a in tr.find_all("a", href=re.compile(r"/hero/\d+"))]
            if not links:
                continue
            m = re.search(r"/hero/(\d+)", links[0]["href"])
            if not m:
                continue
            cid = int(m.group(1))
            if cid in seen:
                continue
            seen.add(cid)
            # 英雄名：链接文本，排除“查看详情”这类操作按钮
            name = ""
            for a in links:
                t = a.get_text(strip=True)
                if t and "查看" not in t:
                    name = t
                    break
            text = tr.get_text(" ", strip=True)
            wr_m = re.search(r"(\d{1,2}\.\d{1,2})%", text)
            sample_m = re.search(r"([\d,]+)\s*场", text)
            if wr_m:
                items.append(ChampionStat(
                    champion_id=cid,
                    name_zh=name,
                    win_rate=float(wr_m.group(1)),
                    sample=_parse_int(sample_m.group(1)) if sample_m else None,
                ))
        if items:
            return items
        # 路径二：内嵌 JSON（__NEXT_DATA__ / window.__INITIAL_STATE__ 等）
        for m in re.finditer(r"window\.__\w+__\s*=\s*(\{.*?\});", html, re.S):
            try:
                data = json.loads(m.group(1))
                found = _extract_champions_from_json(data)
                if found:
                    return found
            except json.JSONDecodeError:
                continue
        return items

    # ---------- 符文 ----------
    def fetch_augments(self) -> list[AugmentStat]:
        last_err: Optional[Exception] = None
        for path in AUGMENT_PATH_CANDIDATES:
            try:
                html = self._get(path)
                items = self._parse_augments_from_html(html)
                if items:
                    log.info("hexdata 符文页命中: %s (%d 条)", path, len(items))
                    return items
            except ProviderError as e:
                last_err = e
        raise ProviderError(
            f"hexdata.com.cn 未找到符文数据页（{', '.join(AUGMENT_PATH_CANDIDATES)}）: {last_err}"
        )

    def _parse_augments_from_html(self, html: str) -> list[AugmentStat]:
        items: list[AugmentStat] = []
        seen: set[str] = set()
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            links = [a for a in tr.find_all("a", href=re.compile(r"/augment(s)?/\d+"))]
            if not links:
                continue
            m = re.search(r"/augment(s)?/(\d+)", links[0]["href"])
            if not m:
                continue
            aug_id = m.group(2)
            if aug_id in seen:
                continue
            seen.add(aug_id)
            name = ""
            for a in links:
                t = a.get_text(strip=True)
                if t and "查看" not in t:
                    name = t
                    break
            text = tr.get_text(" ", strip=True)
            wr_m = re.search(r"(\d{1,2}\.\d{1,2})%", text)
            if wr_m:
                items.append(AugmentStat(
                    augment_id=aug_id,
                    name_zh=name,
                    win_rate=float(wr_m.group(1)),
                ))
        return items


def _parse_int(s: str) -> int:
    return int(s.replace(",", ""))


def _extract_champions_from_json(data) -> list[ChampionStat]:
    """递归找形如 {championId/champion_id/id, winRate/win_rate} 的对象列表"""
    results: list[ChampionStat] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    parsed = [_try_champion_dict(d) for d in v]
                    parsed = [p for p in parsed if p]
                    if parsed:
                        results.extend(parsed)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return results


def _try_champion_dict(d: dict) -> Optional[ChampionStat]:
    cid = d.get("championId") or d.get("champion_id") or d.get("id")
    wr = d.get("winRate") or d.get("win_rate") or d.get("winrate")
    if isinstance(cid, int) and isinstance(wr, (int, float)):
        return ChampionStat(
            champion_id=cid,
            name_zh=str(d.get("name") or d.get("name_zh") or ""),
            win_rate=float(wr),
        )
    return None
