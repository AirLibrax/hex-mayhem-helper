"""海克斯大乱斗助手 - 入口

架构：
- LCU 线程：轮询对局阶段，选人/对局中拉取双方英雄 -> 悬浮窗阵容胜率
- 截图线程：对局中检测符文弹窗，图标模板匹配 -> 悬浮窗符文推荐
- 数据层：aramgg / hexdata 双源，SQLite 缓存，设置内切换
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import cv2

from PySide6.QtCore import QObject, Qt, Signal, QTimer
from PySide6.QtWidgets import QApplication

from src.config import Config, load_api_key_file
from src.paths import data_dir
from src.data.manager import DataManager
from src.data.cache import StatsCache
from src.data.models import MatchupView
from src.lcu.connector import GamePhase, LcuClient
from src.capture.detector import AugmentDetector, ScreenCapturer
from src.ui import OverlayWindow
from src.version import VERSION

log = logging.getLogger(__name__)


class AppController(QObject):
    """跨线程信号桥：LCU 线程/截图线程 -> UI 线程"""
    status_changed = Signal(str)
    matchup_ready = Signal(object)
    augments_ready = Signal(object)
    build_ready = Signal(object)
    data_refreshed = Signal(str)
    data_failed = Signal(str)
    debug_done = Signal(object, object, object)   # (results, 截图路径, 命中屏序号)
    show_teams = Signal(bool, bool)    # (我方可见, 敌方可见) 按阶段控制


def parse_lineup_from_champ_select(session: dict) -> tuple[list[int], list[int], list[int]]:
    """从选人会话提取 (我方英雄ID, 敌方英雄ID, 公共台英雄ID)
    我方 = 队友已选；公共台（bench）= 未被选择的英雄，单独返回供下方展示。"""
    my_ids, their_ids = [], []
    for team, target in (("myTeam", my_ids), ("theirTeam", their_ids)):
        for cell in session.get(team) or []:
            cid = cell.get("championId") or 0
            if cid > 0:
                target.append(cid)
    bench_ids = [cid for cid in (session.get("benchChampionIds") or session.get("otherChampionIds") or []) if cid > 0]
    if not bench_ids:
        # 兜底：myTeam 里 championId 为 0 的格子可能是可选位
        pass
    return my_ids, their_ids, bench_ids


def parse_lineup_from_game(session: dict, my_puuid: str) -> tuple[list[int], list[int]]:
    """从对局会话提取 (我方英雄ID, 敌方英雄ID)，按 puuid 定位自己"""
    my_ids, their_ids = [], []
    players = (session.get("gameData") or {}).get("players") or []
    my_team = None
    for p in players:
        if p.get("puuid") == my_puuid:
            my_team = p.get("team")
            break
    for p in players:
        cid = p.get("championId") or 0
        if cid <= 0:
            continue
        team = p.get("team")
        if team == my_team:
            my_ids.append(cid)
        else:
            their_ids.append(cid)
    return my_ids, their_ids


def get_my_champion_id(session: dict, my_puuid: str, my_summoner_id: str = "") -> int:
    """从对局会话取自己的英雄 ID

    客户端 gameData.players 的 puuid 字段可能为空（隐私保护），
    只返回 summonerId——用 summonerId 兜底匹配。
    """
    players = (session.get("gameData") or {}).get("players") or []
    for p in players:
        if my_puuid and p.get("puuid") == my_puuid:
            return p.get("championId") or 0
    if my_summoner_id:
        for p in players:
            if p.get("summonerId") == my_summoner_id:
                return p.get("championId") or 0
    return 0


def get_my_champion_from_champ_select(session: dict, summoner_id: str, puuid: str) -> int:
    """从选人会话识别自己英雄（myTeam 按 summonerId/puuid 匹配）

    关键：进入对局后 gameData.players 会清空，英雄只能在选人/载入阶段识别。
    """
    for cell in session.get("myTeam") or []:
        if summoner_id and cell.get("summonerId") == summoner_id:
            return cell.get("championId") or 0
        if puuid and cell.get("puuid") == puuid:
            return cell.get("championId") or 0
    return 0


def main() -> int:
    log_dir = data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
        ],
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 对话框/结果窗关闭不退出，仅悬浮窗 × 退出
    app.setApplicationName("HexMayhemHelper")
    log.info("HexMayhemHelper v%s 启动", VERSION)

    config = Config()
    # api_key.txt 优先：用户自己申请的 Key 覆盖（含内置）
    file_key = load_api_key_file()
    if file_key:
        config.set("aramgg_api_key", file_key)
        log.info("已从 api_key.txt 加载用户 Key")
    sync_detect_cfg(config)   # 启动时同步检测屏幕配置
    cache = StatsCache()
    manager = DataManager(config, cache)
    controller = AppController()

    overlay = OverlayWindow(config, manager)
    overlay.settings_requested.connect(lambda: _open_settings(app, config, manager, controller, overlay))
    overlay.close_requested.connect(app.quit)

    controller.status_changed.connect(overlay.set_status)
    controller.matchup_ready.connect(overlay.set_matchup)
    controller.augments_ready.connect(overlay.set_augments)
    controller.build_ready.connect(overlay.set_build_recommendation)
    controller.show_teams.connect(overlay.show_teams)
    controller.debug_done.connect(
        lambda results, shot_path, hit_idx: _show_debug_result(
            results, shot_path, manager, overlay, hit_idx
        )
    )
    controller.data_refreshed.connect(
        lambda msg: (overlay.set_status(msg), _notify(msg))
    )
    controller.data_failed.connect(
        lambda msg: (overlay.set_status(f"数据错误: {msg[:40]}"), _notify(f"数据错误: {msg}"))
    )

    overlay.show()

    # 启动时数据新鲜度检查
    def _on_initial_refresh(n_c, n_a):
        controller.data_refreshed.emit(f"数据就绪: 英雄{n_c} 符文{n_a}")
    QTimer.singleShot(500, lambda: manager.ensure_fresh(
        on_done=_on_initial_refresh,
        on_error=lambda e: controller.data_failed.emit(str(e)),
    ))

    # LCU 监听线程
    stop_lcu = threading.Event()

    def lcu_worker():
        client = LcuClient()
        last_phase = None
        detector: AugmentDetector | None = None
        det_applied_rev = -1
        my_puuid = None
        state = {"sig": None, "puuid": None, "summoner_id": None, "my_cid": 0}

        def make_detector() -> AugmentDetector:
            return AugmentDetector(
                ScreenCapturer(monitors=detect_cfg["monitors"]),
                resolve_name=lambda text: getattr(
                    manager.match_ocr_text(text), "augment_id", None
                ),
                callback=lambda results: _handle_augment_results(
                    results, manager, controller, state),
            )

        def load_build_for_me(phase: str) -> None:
            """进入对局（含载入画面预加载）后，拉自己英雄的装备/海克斯推荐。
            结果写入 game_state：符文识别时用它做"英雄适配"交叉。"""
            if phase not in (GamePhase.GAME_START.value, GamePhase.IN_PROGRESS.value):
                return
            if not (state["puuid"] or state["summoner_id"]):
                # 启动时可能取不到召唤师信息（如已在游戏中），这里重试一次
                try:
                    summoner = client.current_summoner() or {}
                    state["puuid"] = state["puuid"] or summoner.get("puuid")
                    state["summoner_id"] = state["summoner_id"] or summoner.get("summonerId")
                except Exception:
                    pass
            if not (state["puuid"] or state["summoner_id"]):
                log.warning("无召唤师标识（puuid/summonerId 均空），无法识别英雄")
                return
            try:
                session = client.gameflow_session()
                if not session:
                    log.warning("无对局会话，英雄识别跳过")
                    return
                cid = get_my_champion_id(session, state["puuid"] or "",
                                         state["summoner_id"] or "")
                if not cid:
                    log.warning("对局会话中未匹配到自己（players=%d, puuid=%s, sid=%s）",
                                len((session.get("gameData") or {}).get("players") or []),
                                bool(state["puuid"]), state["summoner_id"])
                    return
                log.info("识别到自己英雄 cid=%s (phase=%s)", cid, phase)
                game_state["my_cid"] = cid
                detail, loading = manager.ensure_champion_detail(cid)
                if detail:
                    game_state["detail"] = detail
                    if detail.builds:
                        controller.build_ready.emit(detail.builds[0])
                elif loading:
                    # 后台拉取中：异步等待就绪后更新 game_state（不阻塞 LCU 轮询线程）。
                    # 否则第一局的第一个弹窗会因详情未就绪而显示全局数据。
                    def waiter(cid=cid):
                        for _ in range(30):   # 最多 15 秒
                            time.sleep(0.5)
                            d = manager.get_champion_detail(cid)
                            if d:
                                game_state["detail"] = d
                                if d.builds:
                                    controller.build_ready.emit(d.builds[0])
                                log.info("英雄详情就绪: id=%d 符文%d 出装%d",
                                         cid, len(d.augments), len(d.builds))
                                break
                    threading.Thread(target=waiter, daemon=True, name="detail-wait").start()
            except Exception:
                log.exception("装备推荐加载异常")

        def load_build_from_champ_select(phase: str) -> None:
            """选人阶段识别自己英雄 + 预拉详情（关键：进游戏后 players 清空，
            英雄只能在选人/载入阶段识别；这里提前锁定，对局直接走缓存）。"""
            if phase != GamePhase.CHAMP_SELECT.value:
                return
            if not (state["puuid"] or state["summoner_id"]):
                return
            try:
                session = client.champ_select_session()
                if not session:
                    return
                cid = get_my_champion_from_champ_select(
                    session, state["summoner_id"] or "", state["puuid"] or "")
                if not cid:
                    return   # 尚未锁定英雄，下轮重试
                if game_state.get("my_cid") == cid and game_state.get("detail"):
                    return   # 已识别且详情就绪，跳过
                log.info("选人阶段识别到自己英雄 cid=%s", cid)
                game_state["my_cid"] = cid
                detail, loading = manager.ensure_champion_detail(cid)
                if detail:
                    game_state["detail"] = detail
                    if detail.builds:
                        controller.build_ready.emit(detail.builds[0])
                elif loading:
                    def waiter(cid=cid):
                        for _ in range(30):
                            time.sleep(0.5)
                            d = manager.get_champion_detail(cid)
                            if d:
                                game_state["detail"] = d
                                if d.builds:
                                    controller.build_ready.emit(d.builds[0])
                                log.info("选人阶段详情就绪: id=%d 符文%d 出装%d",
                                         cid, len(d.augments), len(d.builds))
                                break
                    threading.Thread(target=waiter, daemon=True, name="cs-detail-wait").start()
            except Exception:
                log.exception("选人阶段英雄识别异常")

        def refresh_lineup(phase: str) -> None:
            """按阶段拉阵容：选人=我方(含公共台)；载入=双方；对局=不再推送（阵容区隐藏）"""
            try:
                if phase == GamePhase.CHAMP_SELECT.value:
                    session = client.champ_select_session()
                    if not session:
                        return
                    my_ids, their_ids, bench_ids = parse_lineup_from_champ_select(session)
                    their_ids = []   # 选人阶段不显示敌方
                elif phase in (GamePhase.GAME_START.value, GamePhase.IN_PROGRESS.value):
                    session = client.gameflow_session()
                    if not session:
                        return
                    my_ids, their_ids = parse_lineup_from_game(session, state["puuid"] or "")
                    bench_ids = []
                else:
                    return
                sig = (tuple(sorted(my_ids)), tuple(sorted(their_ids)))
                if sig != state["sig"]:
                    state["sig"] = sig
                    controller.matchup_ready.emit(
                        manager.get_matchup(my_ids, their_ids, bench_ids))
                    log.info("阵容已推送: 我方%d 敌方%d 公共台%d",
                             len(my_ids), len(their_ids), len(bench_ids))
            except Exception:
                log.exception("阵容刷新异常")

        while not stop_lcu.is_set():
            if not client.connected:
                controller.status_changed.emit("未连接客户端，请先启动游戏客户端")
                if client.connect():
                    controller.status_changed.emit("已连接客户端")
                    summoner = client.current_summoner() or {}
                    state["puuid"] = summoner.get("puuid")
                    state["summoner_id"] = summoner.get("summonerId")
                    log.info("已连接: puuid=%s summonerId=%s",
                             bool(state["puuid"]), state["summoner_id"])
                else:
                    stop_lcu.wait(3)
                    continue
            phase = client.current_phase()
            if phase != last_phase:
                last_phase = phase
                log.info("阶段: %s", phase)
                controller.status_changed.emit(phase)
                if phase == GamePhase.CHAMP_SELECT.value:
                    state["sig"] = None          # 进入选人，强制刷新
                    # 清上一局残留的英雄上下文（防异常跳阶段）
                    game_state["my_cid"] = 0
                    game_state["detail"] = None
                    controller.show_teams.emit(True, False)   # 只显示我方(含公共台)
                    controller.build_ready.emit(None)
                    controller.augments_ready.emit([])
                    refresh_lineup(phase)
                    load_build_from_champ_select(phase)   # 选人阶段识别自己英雄+预拉详情
                elif phase == GamePhase.GAME_START.value:
                    state["sig"] = None          # 载入画面：显示双方
                    controller.show_teams.emit(True, True)
                    refresh_lineup(phase)
                    load_build_for_me(phase)      # 预加载装备推荐
                elif phase == GamePhase.IN_PROGRESS.value:
                    controller.show_teams.emit(False, False)  # 对局中隐藏阵容
                    load_build_for_me(phase)
                    if detector is None:
                        detector = make_detector()
                        det_applied_rev = detect_cfg["rev"]
                        detector.start()
                elif phase in (GamePhase.END_OF_GAME.value, GamePhase.NONE.value,
                               GamePhase.LOBBY.value, GamePhase.READY_CHECK.value):
                    if detector is not None:
                        detector.stop()
                        detector = None
                    controller.show_teams.emit(False, False)
                    controller.augments_ready.emit([])
                    controller.build_ready.emit(None)
                    state["my_cid"] = 0
                    game_state["my_cid"] = 0
                    game_state["detail"] = None
            else:
                # 阶段未变：选人检查重随/锁定英雄；载入画面重试阵容（gameData 可能延迟就绪）
                if phase in (GamePhase.CHAMP_SELECT.value, GamePhase.GAME_START.value):
                    refresh_lineup(phase)
                if phase == GamePhase.CHAMP_SELECT.value:
                    load_build_from_champ_select(phase)   # 等玩家锁定后识别
                if detector is not None and detect_cfg["rev"] != det_applied_rev:
                    detector.stop()
                    detector = make_detector()
                    det_applied_rev = detect_cfg["rev"]
                    detector.start()
                    log.info("检测屏幕配置已更新，检测线程重建")
            stop_lcu.wait(config.get("poll_interval") or 2.0)

    threading.Thread(target=lcu_worker, daemon=True, name="lcu-poll").start()

    try:
        return app.exec()
    finally:
        stop_lcu.set()


def _win_tier(wr: float) -> str:
    """按胜率分 T 级（胜率网站层级）：≥50 T1 / ≥45 T2 / ≥40 T3 / <40 T4"""
    if wr >= 50:
        return "T1"
    if wr >= 45:
        return "T2"
    if wr >= 40:
        return "T3"
    return "T4"


def _handle_augment_results(results, manager, controller, state):
    """符文识别结果 -> 查胜率/选取率/组合 -> UI

    数据优先级：英雄详情（识别英雄后）> 全局符文榜。
    T 级按胜率分档（T1 红 / T2 金 / T3 蓝 / T4 绿），不依赖官推 rank。
    组合提示：识别符文命中英雄 Top5 组合时，行尾显示相关组合（其他成员+胜率）。
    """
    my_cid = state.get("my_cid") or game_state.get("my_cid") or 0
    detail = game_state.get("detail")
    if not detail and my_cid:
        detail = manager.get_champion_detail(my_cid)
        if detail:
            game_state["detail"] = detail
    hero_map = {a.augment_id: a for a in (detail.augments if detail else [])} if detail else {}
    trios = (detail.augment_trios if detail else None) or []
    # 数据来源标注：英雄视角 or 全局
    src_name = "全局"
    if my_cid:
        ch = manager.get_champion(my_cid)
        if ch:
            src_name = ch.name_zh or f"#{my_cid}"

    rows = []
    for r in results:
        aug_id = r.get("augment_id")
        if aug_id:
            aug = manager.get_augment_by_id(aug_id)
            name = aug.name_zh if aug else ""
            wr = aug.win_rate if aug else 0.0
            pick = aug.pick_rate if aug else None
            hero = hero_map.get(aug_id)
            if hero:
                if hero.name_zh and not name:
                    name = hero.name_zh
                if hero.win_rate is not None:
                    wr = hero.win_rate        # 英雄视角胜率优先
                if hero.pick_rate is not None:
                    pick = hero.pick_rate      # 英雄视角选取率优先
            tier = aug.tier if aug and aug.tier else _win_tier(wr or 0.0)  # 官方 tier 优先
            # 组合提示：该符文命中的最高胜率组合（显示其他成员）
            combo_txt = ""
            best = None
            for t in trios:
                ids = t.augment_ids or []
                if aug_id in ids and (best is None or t.win_rate > best.win_rate):
                    best = t
            if best:
                others = [n for n, i in zip(best.names or [], best.augment_ids or [])
                          if i != aug_id and n]
                if others:
                    combo_txt = f" 组合:{'+'.join(others)}({best.win_rate:.1f}%)"
            rows.append((name, wr or 0.0, tier, pick, combo_txt, src_name))
        else:
            rows.append(("识别中…", 0.0, "", None, "", src_name))
    controller.augments_ready.emit(rows)


def _show_debug_result(results, shot_path, manager, parent, monitor_idx=None):
    """手动截图识别结果对话框：截图缩略图 + 识别明细（非模态，不阻塞其他窗口）"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

    from src.ui.settings_dialog import DIALOG_STYLE

    dlg = QDialog(parent)
    dlg.setWindowTitle(f"手动截图识别结果 v{VERSION}")
    dlg.setStyleSheet(DIALOG_STYLE)
    dlg.setWindowModality(Qt.WindowModality.NonModal)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.setMinimumWidth(440)
    v = QVBoxLayout(dlg)

    # 检测屏标注
    if monitor_idx:
        v.addWidget(QLabel(f"检测屏: 屏幕 {monitor_idx}"))

    # 截图缩略图（cv2 降采样，避免 QPixmap 加载大图卡 UI）
    img = cv2.imread(str(shot_path))
    if img is not None:
        ih, iw = img.shape[:2]
        scale = 420 / iw
        thumb = cv2.resize(img, (420, max(1, int(ih * scale))),
                           interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                      QImage.Format.Format_RGB888)
        img_label = QLabel()
        img_label.setPixmap(QPixmap.fromImage(qimg.copy()))
        v.addWidget(img_label)

    # 识别明细
    if results:
        # 英雄适配上下文（对局中才有）
        detail = game_state.get("detail")
        hero_line = ""
        if detail:
            hero_line = f"（英雄视角数据）"
        lines = [f"检测到 {len(results)} 张符文卡{hero_line}："]
        hero_map = {a.augment_id: a for a in (detail.augments if detail else [])} if detail else {}
        all_unmatched = True
        for r in results:
            aug_id = r.get("augment_id")
            if aug_id:
                aug = manager.get_augment_by_id(aug_id)
                name = aug.name_zh if aug else ""
                wr = aug.win_rate or 0.0
                pick = aug.pick_rate
                hero = hero_map.get(aug_id)
                if hero:
                    if hero.name_zh and not name:
                        name = hero.name_zh
                    if hero.win_rate is not None:
                        wr = hero.win_rate
                    if hero.pick_rate is not None:
                        pick = hero.pick_rate
                line = f"  · {name or aug_id} · 胜率{wr:.1f}%"
                if pick is not None:
                    line += f" · 选取率{pick:.1f}%"
                lines.append(line)
                all_unmatched = False
            else:
                ocr_txt = (r.get("ocr_text") or "").strip()
                if ocr_txt:
                    lines.append(f"  · 未匹配（OCR读到: {ocr_txt[:24]}…）")
                else:
                    lines.append("  · OCR 无输出（标题区未读到文字）")
        if all_unmatched:
            try:
                n_aug = len(manager._cache.get_augments())
            except Exception:
                n_aug = -1
            lines.append(f"  符文库: {n_aug} 条（若为 0 说明数据未加载）")
    else:
        lines = [
            "未检测到符文弹窗。",
            "使用方式：对局内出现海克斯三选一弹窗时点击，",
            "或直接截取含弹窗的画面后点击。",
        ]
    v.addWidget(QLabel("\n".join(lines)))

    hint = QLabel(f"截图已保存: {shot_path.name}（{shot_path.parent}）")
    hint.setStyleSheet("color: #8a8a8e; font-size: 11px;")
    hint.setWordWrap(True)
    v.addWidget(hint)

    btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    btn.rejected.connect(dlg.reject)
    btn.accepted.connect(dlg.accept)
    v.addWidget(btn)
    dlg.show()


# 检测屏幕共享配置（lcu_worker 线程与设置对话框共用；GIL 保证单键赋值安全）
detect_cfg = {"monitors": None, "rev": 0}

# 当前对局英雄上下文（手动截图/自动检测共用）：识别符文后查"英雄适配"
game_state = {"my_cid": 0, "detail": None}


def sync_detect_cfg(config) -> None:
    """从配置同步检测屏幕：0=全部，N=指定物理屏"""
    m = int(config.get("detect_monitor") or 0)
    detect_cfg["monitors"] = [m] if m > 0 else None
    detect_cfg["rev"] += 1


def _open_settings(app, config, manager, controller, overlay):
    from src.ui import SettingsDialog

    def on_debug_capture(monitor_sel: int = 0):
        """手动识别符文（监听模式）：后台持续截屏最多 15 秒，弹窗一出现即识别。
        防重入：监听中忽略新点击。"""
        log.info("手动识别符文点击 (monitor_sel=%s)", monitor_sel)
        if detect_cfg.get("busy"):
            controller.status_changed.emit("正在监听中，请等待完成")
            return
        detect_cfg["busy"] = True
        try:
            dlg._debug_btn.setEnabled(False)
        except Exception:
            pass
        controller.status_changed.emit("截图识别中… 监听弹窗（最长 15 秒）")

        def worker():
            try:
                monitors = [monitor_sel] if monitor_sel > 0 else detect_cfg["monitors"]
                det = AugmentDetector(
                    ScreenCapturer(monitors=monitors),
                    resolve_name=lambda text: getattr(
                        manager.match_ocr_text(text), "augment_id", None
                    ),
                )
                deadline = time.time() + 15.0
                results = []
                hit_frame = None
                hit_idx = None
                last_tip = 0.0
                while time.time() < deadline:
                    # 每 3 秒提示剩余时间，避免"点了没反应"的体感
                    now = time.time()
                    if now - last_tip >= 3.0:
                        last_tip = now
                        remain = max(1, int(deadline - now))
                        controller.status_changed.emit(f"监听弹窗中… 剩余 {remain} 秒")
                    screen_frames = det._capturer.grab_screens()
                    for idx, frame in screen_frames:
                        r = det.analyze_frame(frame)
                        if r:
                            results = r
                            hit_frame = frame
                            hit_idx = idx
                            log.info("手动截图命中屏 %d", idx)
                            break
                    if results:
                        break
                    time.sleep(0.8)
                # 保存截图供排查（命中帧；未命中保存检测屏最后一帧）
                if hit_frame is None and screen_frames:
                    hit_frame = screen_frames[0][1]
                    hit_idx = screen_frames[0][0]
                debug_dir = data_dir()
                debug_dir.mkdir(parents=True, exist_ok=True)
                shot_path = debug_dir / "debug_capture.png"
                cv2.imwrite(str(shot_path), hit_frame)
                if results:
                    _handle_augment_results(results, manager, controller, game_state)
                    controller.status_changed.emit(
                        f"手动截图: 识别到 {len(results)} 张符文卡（截图: debug_capture.png）"
                    )
                else:
                    controller.augments_ready.emit([])
                    controller.status_changed.emit(
                        "手动截图: 15 秒内未检测到符文弹窗（截图: debug_capture.png）"
                    )
                controller.debug_done.emit(results, shot_path, hit_idx)
            except Exception as e:
                log.exception("手动截图失败")
                controller.status_changed.emit(f"手动截图失败: {e}")
            finally:
                detect_cfg["busy"] = False
                try:
                    dlg._debug_btn.setEnabled(True)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True, name="debug-capture").start()

    def on_hero_identify():
        """手动识别英雄：走正常链路（LCU 读当前英雄 → 拉详情 → 写入缓存），
        之后手动识别符文即可看到该英雄的适配胜率。"""
        log.info("手动识别英雄点击")
        controller.status_changed.emit("识别英雄中…")

        def worker():
            try:
                from src.lcu.connector import LcuClient
                client = LcuClient()
                if not client.connect():
                    controller.status_changed.emit("未连接客户端")
                    return
                session = client.gameflow_session()
                if not session:
                    controller.status_changed.emit("无对局会话（需在选人/对局中）")
                    return
                summoner = client.current_summoner() or {}
                puuid = summoner.get("puuid")
                sid = summoner.get("summonerId")
                cid = get_my_champion_id(session, puuid or "", sid or "")
                if not cid:
                    controller.status_changed.emit("未识别到英雄（不在选人/对局）")
                    return
                game_state["my_cid"] = cid
                # 触发拉取（force）并等待写缓存
                manager.ensure_champion_detail(cid, force=True)
                detail = None
                for _ in range(40):   # 最多等 30 秒
                    time.sleep(0.75)
                    detail = manager.get_champion_detail(cid)
                    if detail:
                        break
                if detail:
                    game_state["detail"] = detail
                    ch = manager.get_champion(cid)
                    name = (ch.name_zh if ch else "") or f"#{cid}"
                    controller.status_changed.emit(
                        f"英雄已识别: {name} · 符文推荐 {len(detail.augments)} 条 · 出装 {len(detail.builds)} 套"
                    )
                    log.info("手动识别英雄 %s (%d): 符文%d 出装%d",
                             name, cid, len(detail.augments), len(detail.builds))
                else:
                    controller.status_changed.emit(f"英雄 {cid} 详情拉取超时")
            except Exception as e:
                log.exception("手动识别英雄失败")
                controller.status_changed.emit(f"识别英雄失败: {e}")

        threading.Thread(target=worker, daemon=True, name="hero-identify").start()

    def on_provider_changed(name):
        controller.status_changed.emit(f"切换数据源: {name}，拉取中…")
        manager.refresh_async(
            provider=name,
            on_done=lambda n_c, n_a: controller.data_refreshed.emit(
                f"数据源 {name}: 英雄{n_c} 符文{n_a}"
            ),
            on_error=lambda e: controller.data_failed.emit(str(e)),
        )

    def on_refresh():
        controller.status_changed.emit("手动刷新数据…")
        manager.refresh_async(
            on_done=lambda n_c, n_a: controller.data_refreshed.emit(
                f"刷新完成: 英雄{n_c} 符文{n_a}"
            ),
            on_error=lambda e: controller.data_failed.emit(str(e)),
        )

    def on_settings_saved():
        overlay.apply_settings()
        sync_detect_cfg(config)   # 检测屏幕变更 -> 检测线程热更新

    dlg = SettingsDialog(config, manager, overlay)
    dlg.setWindowModality(Qt.WindowModality.NonModal)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.provider_changed.connect(on_provider_changed)
    dlg.refresh_requested.connect(on_refresh)
    dlg.debug_capture_requested.connect(on_debug_capture)
    dlg.hero_identify_requested.connect(on_hero_identify)
    dlg.monitor_changed.connect(lambda sel: sync_detect_cfg(config))
    dlg.settings_applied.connect(on_settings_saved)
    # 调试反馈同时显示在设置窗口内部（悬浮窗状态栏小字容易被忽略）
    controller.status_changed.connect(dlg.set_debug_status)
    dlg.show()


def _notify(msg: str):
    log.info("notify: %s", msg)


if __name__ == "__main__":
    sys.exit(main())
