"""悬浮窗：无边框、置顶、半透明、可拖动

内容区：
- 状态行：当前阶段 / 数据源 / 版本
- 阵容区：我方 5 英雄胜率 vs 敌方 5 英雄胜率
- 符文区：当前弹窗识别的 3 个符文胜率对比
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from ..config import Config
from ..data.manager import DataManager
from ..data.models import ChampionStat, MatchupView

log = logging.getLogger(__name__)

RESIZE_EDGE = 7      # 边缘可拖拽宽度（px）
RESIZE_CORNER = 24   # 右下角热点（覆盖三角视觉区域）

STYLE = """
QWidget { background: transparent; }
#panel {
    background: rgba(24, 24, 26, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 10px;
}
QLabel { color: #e8e8ea; font-size: 13px; }
#statusLabel { color: #8a8a8e; font-size: 11px; }
#titleLabel { color: #ffffff; font-size: 14px; font-weight: 600; }
#btnSettings, #btnClose {
    color: #9a9a9e; background: transparent; border: none;
    font-size: 12px; padding: 0 4px;
}
#btnSettings:hover, #btnClose:hover { color: #ffffff; }
#winHigh { color: #ffffff; font-weight: 600; }
#winMid { color: #e8e8ea; }
#winLow { color: #8a8a8e; }
#watermark { color: rgba(255, 255, 255, 0.15); font-size: 8px; }
#tri { color: rgba(255, 255, 255, 0.22); font-size: 9px; }
.teamHeader { color: #9a9a9e; font-size: 11px; font-weight: 600; }
.augRow { border: 1px solid rgba(255,255,255,0.10); border-radius: 6px; padding: 2px; }
"""

# 评级颜色（T 级）：T1 红 / T2 金 / T3 蓝 / T4 绿 / T5 白
TIER_COLORS = {
    "T1": "#e05a4e", "T2": "#e8b64c", "T3": "#6cb2ff", "T4": "#6fce8a", "T5": "#ffffff",
    "S": "#e05a4e", "A": "#e8b64c", "B": "#6cb2ff", "C": "#6fce8a", "D": "#ffffff",
}


def tier_color(tier: str) -> Optional[str]:
    """解析评级文本 -> 颜色。支持 'T1'~'T5'、'S/A/B/C/D'、'官推#1'；无评级返回 None"""
    if not tier:
        return None
    import re
    t = tier.strip().upper()
    m = re.match(r"T(\d)", t)
    if m:
        return TIER_COLORS.get(f"T{m.group(1)}")
    m = re.search(r"#(\d)", tier)
    if m:
        return TIER_COLORS.get(f"T{m.group(1)}")
    if t in TIER_COLORS:
        return TIER_COLORS[t]
    return None


class OverlayWindow(QWidget):
    """主悬浮窗"""
    settings_requested = Signal()
    close_requested = Signal()

    def __init__(self, config: Config, manager: DataManager):
        super().__init__(None)
        self._config = config
        self._manager = manager
        self._matchup: Optional[MatchupView] = None
        self._augments: list[tuple[str, float, str]] = []  # (名, 胜率, 档位)
        self._phase_text = "等待客户端..."

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._apply_geometry()
        self._drag_pos = None
        self._resize_dir = None
        self._drag = None
        self._start_geo = None

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QFrame(self)
        panel.setObjectName("panel")
        outer.addWidget(panel)

        v = QVBoxLayout(panel)
        v.setContentsMargins(12, 8, 12, 10)
        v.setSpacing(6)

        # 标题行
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self._title = QLabel("海克斯大乱斗助手")
        self._title.setObjectName("titleLabel")
        self._status = QLabel("未连接")
        self._status.setObjectName("statusLabel")
        btn_opacity = QPushButton("◐")
        btn_opacity.setObjectName("btnSettings")
        btn_opacity.setFixedSize(24, 22)
        btn_opacity.setToolTip("透明度（实时预览）")
        btn_opacity.clicked.connect(self._toggle_opacity)
        btn_settings = QPushButton("⚙")
        btn_settings.setObjectName("btnSettings")
        btn_settings.setFixedSize(24, 22)
        btn_settings.clicked.connect(self.settings_requested.emit)
        btn_close = QPushButton("×")
        btn_close.setObjectName("btnClose")
        btn_close.setFixedSize(24, 22)
        btn_close.clicked.connect(self.close_requested.emit)
        title_row.addWidget(self._title)
        title_row.addStretch(1)
        title_row.addWidget(self._status)
        title_row.addWidget(btn_opacity)
        title_row.addWidget(btn_settings)
        title_row.addWidget(btn_close)
        v.addLayout(title_row)

        # 透明度行（点 ◐ 展开，拖动实时预览）
        self._opacity_row = QWidget()
        oh = QHBoxLayout(self._opacity_row)
        oh.setContentsMargins(0, 0, 0, 0)
        oh.setSpacing(8)
        oh.addWidget(QLabel("透明度"))
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(30, 100)
        self._opacity_slider.setValue(int((self._config.get("overlay_opacity") or 0.92) * 100))
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        oh.addWidget(self._opacity_slider, 1)
        self._opacity_row.hide()
        v.addWidget(self._opacity_row)

        # 阵容区
        self._teams_widget = QWidget()
        tv = QVBoxLayout(self._teams_widget)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(2)
        self._team_labels: dict[str, list[QLabel]] = {"my": [], "their": []}
        self._team_headers: dict[str, QLabel] = {}
        for key, label_text in (("my", "我方"), ("their", "敌方")):
            header = QLabel(label_text)
            header.setObjectName("teamHeader")
            tv.addWidget(header)
            self._team_headers[key] = header
            for _ in range(5):
                lab = QLabel("-")
                tv.addWidget(lab)
                self._team_labels[key].append(lab)
        self._teams_widget.hide()  # 无数据/非选人阶段不显示空阵容
        v.addWidget(self._teams_widget)

        # 公共台（未被选择的英雄）区：选人阶段显示
        self._bench_widget = QWidget()
        bv = QVBoxLayout(self._bench_widget)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(2)
        self._bench_header = QLabel("未被选择的英雄")
        self._bench_header.setObjectName("teamHeader")
        bv.addWidget(self._bench_header)
        self._bench_labels: list[QLabel] = []
        for _ in range(5):
            lab = QLabel("-")
            bv.addWidget(lab)
            self._bench_labels.append(lab)
        self._bench_widget.hide()
        v.addWidget(self._bench_widget)

        # 符文区
        self._aug_widget = QWidget()
        av = QVBoxLayout(self._aug_widget)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(3)
        self._aug_title = QLabel("符文推荐")
        self._aug_title.setObjectName("teamHeader")
        av.addWidget(self._aug_title)
        self._aug_labels: list[QLabel] = []
        for _ in range(3):
            lab = QLabel("-")
            lab.setWordWrap(True)
            av.addWidget(lab)
            self._aug_labels.append(lab)
        self._aug_widget.hide()
        v.addWidget(self._aug_widget)

        # 装备推荐区（对局中显示）
        self._build_widget = QWidget()
        bv = QVBoxLayout(self._build_widget)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(3)
        self._build_title = QLabel("装备推荐")
        self._build_title.setObjectName("teamHeader")
        bv.addWidget(self._build_title)
        self._build_tags = QLabel("")
        self._build_tags.setObjectName("statusLabel")
        self._build_tags.setWordWrap(True)
        bv.addWidget(self._build_tags)
        self._build_core = QLabel("")
        self._build_core.setWordWrap(True)
        bv.addWidget(self._build_core)
        self._build_start = QLabel("")
        self._build_start.setObjectName("statusLabel")
        self._build_start.setWordWrap(True)
        bv.addWidget(self._build_start)
        self._build_widget.hide()
        v.addWidget(self._build_widget)

        # 底部行：版本号（左下角）+ 水印 + 右下角三角
        v.addStretch(1)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 2, 0, 0)
        bottom.setSpacing(2)
        from src.version import VERSION
        ver = QLabel(f"v{VERSION}")
        ver.setObjectName("watermark")
        wm = QLabel("-From AirLibrax")
        wm.setObjectName("watermark")
        tri = QLabel("◢")
        tri.setObjectName("tri")
        bottom.addWidget(ver)
        bottom.addStretch(1)
        bottom.addWidget(wm)
        bottom.addWidget(tri)
        v.addLayout(bottom)

        self.setMinimumSize(260, 180)
        # 递归开启鼠标跟踪：鼠标在任意子控件（面板/水印/三角）上移动时
        # move 事件都会冒泡到本窗口，边缘光标反馈才能生效
        for w in self.findChildren(QWidget):
            w.setMouseTracking(True)
        self.setMouseTracking(True)

    def _apply_geometry(self) -> None:
        w = self._config.get("overlay_width") or 360
        h = self._config.get("overlay_height") or 300
        x = self._config.get("overlay_x") or 100
        y = self._config.get("overlay_y") or 100
        self.setGeometry(x, y, w, h)
        opacity = self._config.get("overlay_opacity") or 0.92
        self.setWindowOpacity(opacity)

    def apply_settings(self) -> None:
        """设置保存后重应用：透明度。不动位置（拖动已实时保存）。"""
        opacity = self._config.get("overlay_opacity") or 0.92
        self.setWindowOpacity(opacity)

    # ---------- 透明度（实时预览） ----------
    def _toggle_opacity(self) -> None:
        self._opacity_row.setVisible(not self._opacity_row.isVisible())

    def _on_opacity_changed(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._config.set("overlay_opacity", value / 100.0)

    # ---------- 数据更新 ----------
    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_phase(self, phase: str) -> None:
        self._phase_text = phase
        self._status.setText(phase)

    def set_matchup(self, m: MatchupView) -> None:
        self._matchup = m
        self._render_teams()
        self._title.setText(
            f"海克斯大乱斗 · {self._manager.provider_name()}"
            if self._manager.provider_name() else "海克斯大乱斗助手"
        )

    def set_build_recommendation(self, build) -> None:
        """对局中显示当前英雄的推荐出装（BuildRec）"""
        if not build:
            self._build_widget.hide()
            return
        wr = build.win_rate or 0.0
        # 装备流派 T 级（与符文同一套分档/颜色：≥50 T1 / ≥45 T2 / ≥40 T3 / 其余 T4）
        if wr >= 50:
            tier_tag = "T1"
        elif wr >= 45:
            tier_tag = "T2"
        elif wr >= 40:
            tier_tag = "T3"
        else:
            tier_tag = "T4"
        head_style = f"color: {TIER_COLORS.get(tier_tag, '#e8e8ea')};"
        self._build_tags.setText(
            f"[{tier_tag}] {build.tags or '主流'} · 胜率 {wr:.1f}% · 样本 {build.games}"
        )
        self._build_tags.setStyleSheet(head_style)
        core = " → ".join(i.name for i in (build.core_items or []) if i.name)
        start = " + ".join(i.name for i in (build.starting_items or []) if i.name)
        spells = "/".join(build.summoner_spells or [])
        self._build_core.setText(f"核心: {core}")
        self._build_core.setStyleSheet("color: #ffffff; font-weight: 600;")
        self._build_start.setText(f"出门: {start}" + (f" · 召唤师: {spells}" if spells else ""))
        self._build_start.setStyleSheet("color: #8a8a8e;")
        self._build_widget.show()

    def clear_build(self) -> None:
        self._build_widget.hide()

    def set_augments(self, rows: list[tuple]) -> None:
        """rows: [(符文名, 胜率, T级, 选取率, 组合提示, 数据来源)]"""
        self._augments = rows
        self._render_augments()
        self._aug_widget.setVisible(bool(rows))

    def show_teams(self, show_my: bool = True, show_their: bool = True) -> None:
        """按阶段控制阵容区显隐：选人只我方(+公共台)，载入双方，对局隐藏"""
        self._team_headers["my"].setVisible(show_my)
        for lab in self._team_labels["my"]:
            lab.setVisible(show_my)
        self._team_headers["their"].setVisible(show_their)
        for lab in self._team_labels["their"]:
            lab.setVisible(show_their)
        self._teams_widget.setVisible(show_my or show_their)
        # 公共台跟随我方显示（仅选人阶段）
        self._bench_widget.setVisible(show_my and bool(self._matchup and self._matchup.bench_team))

    def _render_teams(self) -> None:
        if not self._matchup:
            for lab in self._team_labels["my"] + self._team_labels["their"]:
                lab.setText("-")
                lab.setStyleSheet("")
            self._bench_widget.hide()
            return
        teams = {"my": self._matchup.my_team, "their": self._matchup.their_team}
        avgs = {"my": self._matchup.my_avg, "their": self._matchup.their_avg}
        for key, stats in teams.items():
            labels = self._team_labels[key]
            header = self._team_headers[key]
            avg = avgs[key]
            side = "我方" if key == "my" else "敌方"
            if stats:
                header.setText(f"{side}队伍平均胜率：{avg:.1f}%" if avg else f"{side}队伍平均胜率：-")
            else:
                header.setText(f"{side}队伍平均胜率：-")
            for i in range(5):
                if i < len(stats):
                    c = stats[i]
                    # 格式：[T几] 英雄名 胜率:X% 选取率:X%（T 级用 aramgg 分层色，统一字重）
                    tier_txt = f"[{c.tier}] " if c.tier else ""
                    pick_txt = f" 选取率:{c.pick_rate:.1f}%" if c.pick_rate is not None else ""
                    text = f"{tier_txt}{self._manager.champion_display_name(c)} 胜率:{c.win_rate:.1f}%{pick_txt}"
                    labels[i].setText(text)
                    labels[i].setStyleSheet(self._tier_style(c.tier) or "")
                else:
                    labels[i].setText("-")
                    labels[i].setStyleSheet("")
        # 公共台（未被选择的英雄）
        self._render_bench()

    def _render_bench(self) -> None:
        bench = (self._matchup.bench_team if self._matchup else None) or []
        for i in range(5):
            lab = self._bench_labels[i]
            if i < len(bench):
                c = bench[i]
                tier_txt = f"[{c.tier}] " if c.tier else ""
                pick_txt = f" 选取率:{c.pick_rate:.1f}%" if c.pick_rate is not None else ""
                text = f"{tier_txt}{self._manager.champion_display_name(c)} 胜率:{c.win_rate:.1f}%{pick_txt}"
                lab.setText(text)
                lab.setStyleSheet(self._tier_style(c.tier) or "")
            else:
                lab.setText("-")
                lab.setStyleSheet("")
        self._bench_widget.setVisible(bool(bench))

    def _render_augments(self) -> None:
        for i in range(3):
            lab = self._aug_labels[i]
            if i < len(self._augments):
                name, wr, tier, pick, combo_txt, src = self._augments[i]
                # 格式：[T几] 名字 · 胜率X% · 选取率X% · 组合提示 · [数据来源]
                tier_txt = f"[{tier}] " if tier else ""
                pick_txt = f" · 选取率{pick:.1f}%" if pick is not None else ""
                text = f"{tier_txt}{name} · 胜率{wr:.1f}%{pick_txt}"
                if combo_txt:
                    text += f" · {combo_txt}"
                if src:
                    text += f" · [{src}]"
                lab.setText(text)
                style = self._tier_style(tier)          # 评级色优先
                if style is None:
                    style = self._win_style(wr)          # 无评级用胜率黑白灰
                lab.setStyleSheet(style)
            else:
                lab.setText("-")
                lab.setStyleSheet("")

    @staticmethod
    def _win_style(wr: float) -> str:
        """胜率样式：黑灰白（高=白加粗，低=灰）"""
        if wr >= 55:
            return "color: #ffffff; font-weight: 600;"
        if wr <= 47:
            return "color: #8a8a8e;"
        return "color: #e8e8ea;"

    @staticmethod
    def _tier_style(tier: str) -> Optional[str]:
        """T 级颜色（金紫蓝绿灰），不加粗：三行符文统一字号字重，只靠颜色区分"""
        c = tier_color(tier)
        if c:
            return f"color: {c};"
        return None

    # ---------- 拖动 / 边缘 resize ----------
    def _edge_dir(self, pos) -> Optional[str]:
        """检测位置是否在可拖拽区域：右下角热点 / 右缘 / 下缘。返回 'br'/'r'/'b'/None"""
        w, h = self.width(), self.height()
        if pos.x() >= w - RESIZE_CORNER and pos.y() >= h - RESIZE_CORNER:
            return "br"
        if pos.x() >= w - RESIZE_EDGE:
            return "r"
        if pos.y() >= h - RESIZE_EDGE:
            return "b"
        return None

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._resize_dir = self._edge_dir(e.position())
            if self._resize_dir:
                self._drag = e.globalPosition().toPoint()
                self._start_geo = self.geometry()
            else:
                self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        e.accept()

    def mouseMoveEvent(self, e) -> None:
        if self._resize_dir:
            delta = e.globalPosition().toPoint() - self._drag
            g = self._start_geo
            nw, nh = g.width(), g.height()
            if "r" in self._resize_dir:
                nw = max(self.minimumWidth(), g.width() + delta.x())
            if "b" in self._resize_dir:
                nh = max(self.minimumHeight(), g.height() + delta.y())
            self.setGeometry(g.x(), g.y(), nw, nh)
        elif self._drag_pos is not None:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
        else:
            # 边缘光标反馈（未按下时）
            d = self._edge_dir(e.position())
            if d == "br":
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif d == "r":
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif d == "b":
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.unsetCursor()
        e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._drag_pos = None
        self._resize_dir = None
        # 拖动/缩放结束才写一次配置（拖动中高频写盘是卡顿根源）
        self._save_geometry()
        e.accept()

    def _save_geometry(self) -> None:
        self._config.set("overlay_x", self.x())
        self._config.set("overlay_y", self.y())
        self._config.set("overlay_width", self.width())
        self._config.set("overlay_height", self.height())

    def resizeEvent(self, e) -> None:
        # 尺寸在拖拽结束时统一保存（_save_geometry），此处不写盘避免卡顿
        super().resizeEvent(e)
