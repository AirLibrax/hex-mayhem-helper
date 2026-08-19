"""SQLite 缓存：抓取结果落本地，避免频繁请求数据站"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Optional

from ..config import DB_PATH
from .models import AugmentStat, ChampionStat

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS champions (
    champion_id INTEGER PRIMARY KEY,
    name_zh TEXT DEFAULT '',
    name_en TEXT DEFAULT '',
    win_rate REAL DEFAULT 0,
    tier TEXT DEFAULT '',
    sample INTEGER,
    patch TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS augments (
    augment_id TEXT PRIMARY KEY,
    name_zh TEXT DEFAULT '',
    name_en TEXT DEFAULT '',
    win_rate REAL DEFAULT 0,
    tier TEXT DEFAULT '',
    sample INTEGER,
    patch TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS champion_details (
    champion_id INTEGER PRIMARY KEY,
    patch TEXT DEFAULT '',
    detail_json TEXT DEFAULT '',
    updated_at REAL
);
"""




def _locked(fn):
    """sqlite3 连接跨线程保护（检测/手动截图/LCU 线程都会访问缓存）"""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class StatsCache:
    def __init__(self, db_path: Path = DB_PATH):
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """增量列迁移：旧库缺列时 ALTER TABLE 补齐"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(augments)")}
        if "icon_url" not in cols:
            self._conn.execute("ALTER TABLE augments ADD COLUMN icon_url TEXT DEFAULT ''")
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "champion_details" not in tables:
            self._conn.executescript(
                "CREATE TABLE IF NOT EXISTS champion_details ("
                " champion_id INTEGER PRIMARY KEY,"
                " patch TEXT DEFAULT '',"
                " detail_json TEXT DEFAULT '',"
                " updated_at REAL)"
            )

    # ---------- 写入 ----------
    @_locked
    def replace_champions(self, items: list[ChampionStat], provider: str) -> None:
        now = time.time()
        with self._conn:
            self._conn.execute("DELETE FROM champions")
            self._conn.executemany(
                """INSERT INTO champions
                   (champion_id, name_zh, name_en, win_rate, tier, sample, patch, provider, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(i.champion_id, i.name_zh, i.name_en, i.win_rate, i.tier,
                  i.sample, i.patch, provider, now) for i in items],
            )
        self._set_meta("champions_updated_at", str(now))
        self._set_meta("champions_provider", provider)
        log.info("英雄数据已缓存: %d 条 (provider=%s)", len(items), provider)

    @_locked
    def replace_augments(self, items: list[AugmentStat], provider: str) -> None:
        now = time.time()
        with self._conn:
            self._conn.execute("DELETE FROM augments")
            self._conn.executemany(
                """INSERT INTO augments
                   (augment_id, name_zh, name_en, win_rate, tier, sample, patch, provider, icon_url, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?) """,
                [(i.augment_id, i.name_zh, i.name_en, i.win_rate, i.tier,
                  i.sample, i.patch, provider, i.icon_url, now) for i in items],
            )
        self._set_meta("augments_updated_at", str(now))
        self._set_meta("augments_provider", provider)
        log.info("符文数据已缓存: %d 条 (provider=%s)", len(items), provider)

    # ---------- 查询 ----------
    @_locked
    def get_champion(self, champion_id: int) -> Optional[ChampionStat]:
        row = self._conn.execute(
            "SELECT * FROM champions WHERE champion_id=?", (champion_id,)
        ).fetchone()
        return _row_to_champion(row) if row else None

    @_locked
    def get_champions(self) -> list[ChampionStat]:
        rows = self._conn.execute("SELECT * FROM champions").fetchall()
        return [_row_to_champion(r) for r in rows]

    @_locked
    def get_augment(self, augment_id: str) -> Optional[AugmentStat]:
        row = self._conn.execute(
            "SELECT * FROM augments WHERE augment_id=?", (augment_id,)
        ).fetchone()
        return _row_to_augment(row) if row else None

    @_locked
    def get_augments(self) -> list[AugmentStat]:
        rows = self._conn.execute("SELECT * FROM augments").fetchall()
        return [_row_to_augment(r) for r in rows]

    @_locked
    def find_augment_by_name(self, name: str) -> Optional[AugmentStat]:
        """按名称模糊匹配（中英文），用于截图识别结果反查"""
        name = name.strip()
        if not name:
            return None
        for col in ("name_zh", "name_en"):
            row = self._conn.execute(
                f"SELECT * FROM augments WHERE {col}=? COLLATE NOCASE", (name,)
            ).fetchone()
            if row:
                return _row_to_augment(row)
        # 模糊：子串匹配
        row = self._conn.execute(
            "SELECT * FROM augments WHERE name_zh LIKE ? OR name_en LIKE ? LIMIT 1",
            (f"%{name}%", f"%{name}%"),
        ).fetchone()
        return _row_to_augment(row) if row else None

    # ---------- 单英雄详情缓存 ----------
    @_locked
    def save_champion_detail(self, detail: "ChampionDetail") -> None:
        import json
        from .models import ItemInfo
        now = time.time()

        def _item_dict(i):
            if isinstance(i, ItemInfo):
                return {"item_id": i.item_id, "name": i.name, "icon_url": i.icon_url}
            return i

        builds = []
        for b in detail.builds or []:
            d = b.__dict__.copy()
            for key in ("core_items", "starting_items", "situational_items"):
                items = d.get(key) or []
                d[key] = [_item_dict(x) for x in items]
            builds.append(d)

        payload = {
            "champion_id": detail.champion_id,
            "patch": detail.patch,
            "augments": [a.__dict__ for a in (detail.augments or [])],
            "builds": builds,
            "augment_trios": [t.__dict__ for t in (detail.augment_trios or [])],
        }
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO champion_details (champion_id, patch, detail_json, updated_at)"
                " VALUES (?,?,?,?)",
                (detail.champion_id, detail.patch, json.dumps(payload, ensure_ascii=False), now),
            )

    @_locked
    def get_champion_detail(self, champion_id: int) -> Optional["ChampionDetail"]:
        import json
        from .models import ChampionDetail as CD, AugmentRec, BuildRec, ItemInfo, AugmentTrio
        row = self._conn.execute(
            "SELECT * FROM champion_details WHERE champion_id=?", (champion_id,)
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["detail_json"])
            cd = CD(champion_id=champion_id, patch=row["patch"] or payload.get("patch", ""))
            cd.augments = [AugmentRec(**a) for a in payload.get("augments", [])]
            builds = []
            for b in payload.get("builds", []):
                br = BuildRec(
                    tags=b.get("tags", ""), games=b.get("games", 0),
                    win_rate=b.get("win_rate", 0.0),
                    core_items=[ItemInfo(**i) for i in b.get("core_items", []) or []],
                    starting_items=[ItemInfo(**i) for i in b.get("starting_items", []) or []],
                    situational_items=[ItemInfo(**i) for i in b.get("situational_items", []) or []],
                    summoner_spells=b.get("summoner_spells", []) or [],
                )
                builds.append(br)
            cd.builds = builds
            cd.augment_trios = [AugmentTrio(**t) for t in payload.get("augment_trios", [])]
            return cd
        except Exception:
            log.exception("单英雄详情缓存解析失败 cid=%s", champion_id)
            return None

    # ---------- 元信息 ----------
    @_locked
    def _set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
            )

    @_locked
    def _get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    @_locked
    def updated_at(self, kind: str) -> Optional[float]:
        v = self._get_meta(f"{kind}_updated_at")
        return float(v) if v else None

    @_locked
    def provider_of(self, kind: str) -> Optional[str]:
        return self._get_meta(f"{kind}_provider")

    @_locked
    def close(self) -> None:
        self._conn.close()


def _row_to_champion(row: sqlite3.Row) -> ChampionStat:
    return ChampionStat(
        champion_id=row["champion_id"],
        name_zh=row["name_zh"] or "",
        name_en=row["name_en"] or "",
        win_rate=row["win_rate"] or 0.0,
        tier=row["tier"] or "",
        sample=row["sample"],
        patch=row["patch"] or "",
    )


def _row_to_augment(row: sqlite3.Row) -> AugmentStat:
    return AugmentStat(
        augment_id=row["augment_id"] or "",
        name_zh=row["name_zh"] or "",
        name_en=row["name_en"] or "",
        win_rate=row["win_rate"] or 0.0,
        tier=row["tier"] or "",
        sample=row["sample"],
        patch=row["patch"] or "",
        icon_url=row["icon_url"] or "",
    )
