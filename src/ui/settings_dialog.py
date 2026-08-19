"""设置对话框：数据源切换（aramgg / hexdata）、透明度、刷新"""
from __future__ import annotations

import logging

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QSlider, QVBoxLayout,
)

from ..config import Config
from ..capture.detector import ScreenCapturer
from ..data import PROVIDERS
from ..data.manager import DataManager

log = logging.getLogger(__name__)

# 设置对话框黑灰白样式
DIALOG_STYLE = """
QDialog {
    background: #1c1c1e;
}
QLabel {
    color: #e8e8ea;
    font-size: 13px;
}
QRadioButton {
    color: #e8e8ea;
    font-size: 13px;
    spacing: 6px;
}
QLineEdit {
    background: #26262a;
    color: #e8e8ea;
    border: 1px solid #3c3c40;
    border-radius: 4px;
    padding: 4px 8px;
}
QComboBox {
    background: #26262a;
    color: #e8e8ea;
    border: 1px solid #3c3c40;
    border-radius: 4px;
    padding: 4px 8px;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid #b0b0b4;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background: #26262a;
    color: #e8e8ea;
    border: 1px solid #3c3c40;
    selection-background-color: #3a3a3e;
    selection-color: #ffffff;
    outline: none;
}
QPushButton {
    background: #2e2e32;
    color: #e8e8ea;
    border: 1px solid #3c3c40;
    border-radius: 4px;
    padding: 5px 14px;
}
QPushButton:hover {
    background: #3a3a3e;
}
#headClose {
    background: transparent;
    border: none;
    color: #9a9a9e;
    font-size: 14px;
    padding: 0 4px;
}
#headClose:hover {
    color: #ffffff;
    background: #3a3a3e;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #2e2e32;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #b0b0b4;
    width: 14px;
    margin: -5px 0;
    border-radius: 7px;
}
"""


class SettingsDialog(QDialog):
    provider_changed = Signal(str)   # 数据源切换（需触发重新拉取）
    refresh_requested = Signal()
    debug_capture_requested = Signal(int)   # 手动截图识别（携带当前检测屏选择）
    hero_identify_requested = Signal()      # 手动识别英雄（LCU + 写缓存）
    monitor_changed = Signal(int)    # 检测屏下拉框变化（立即生效）
    settings_applied = Signal()      # 设置已保存（透明度等需重应用）

    def __init__(self, config: Config, manager: DataManager, parent=None):
        super().__init__(parent)
        self._config = config
        self._manager = manager
        self._drag = None
        # 无边框自绘（与悬浮窗同构），不占任务栏
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setWindowTitle("设置")
        self.setMinimumWidth(380)
        self.setStyleSheet(DIALOG_STYLE)
        self._build_ui()
        self._load_state()

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setSpacing(12)

        # 自绘标题栏（可拖动）
        head = QHBoxLayout()
        head_title = QLabel("设置")
        head_title.setStyleSheet("color: #ffffff; font-size: 14px; font-weight: 600;")
        head_close = QPushButton("×")
        head_close.setObjectName("headClose")
        head_close.setFixedSize(24, 22)
        head_close.clicked.connect(self.reject)
        head.addWidget(head_title)
        head.addStretch(1)
        head.addWidget(head_close)
        v.addLayout(head)
        # 数据源
        v.addWidget(QLabel("胜率数据源"))
        self._provider_group = QButtonGroup(self)
        self._radio_aramgg = QRadioButton("aramgg.com（官方 API，腾讯国服快照 + 客户端样本）")
        self._radio_hexdata = QRadioButton("hexdata.com.cn（中文，百万级样本）")
        self._provider_group.addButton(self._radio_aramgg, 0)
        self._provider_group.addButton(self._radio_hexdata, 1)
        v.addWidget(self._radio_aramgg)
        v.addWidget(self._radio_hexdata)

        # API Key（aramgg 专用）
        key_row = QHBoxLayout()
        key_label = QLabel("aramgg API Key")
        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("留空使用内置 Key")
        key_btn = QPushButton("获取 Key")
        key_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://data.dtodo.cn/developer.html"))
        )
        key_row.addWidget(key_label)
        key_row.addWidget(self._key_edit, 1)
        key_row.addWidget(key_btn)
        v.addLayout(key_row)

        # 刷新
        refresh_row = QHBoxLayout()
        self._refresh_btn = QPushButton("立即刷新数据")
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        self._data_info = QLabel("")
        self._data_info.setStyleSheet("color: #8a8a8e; font-size: 11px;")
        refresh_row.addWidget(self._refresh_btn)
        refresh_row.addWidget(self._data_info, 1)
        v.addLayout(refresh_row)

        # 检测屏幕
        v.addWidget(QLabel("符文检测屏幕（多屏时指定游戏所在屏）"))
        self._monitor_combo = QComboBox()
        try:
            descs = ScreenCapturer().monitor_descs
        except Exception:
            descs = []
        self._monitor_combo.addItem("自动（全部屏幕）", 0)
        for idx, w, h in descs:
            self._monitor_combo.addItem(f"屏幕 {idx}（{w}×{h}）", idx)
        self._monitor_combo.setCurrentIndex(0)
        self._monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)
        monitor_hint = QLabel("手动截图与自动检测立即生效；对局中切换需等下一轮轮询（约 2 秒）")
        monitor_hint.setStyleSheet("color: #8a8a8e; font-size: 11px;")
        monitor_hint.setWordWrap(True)
        v.addWidget(self._monitor_combo)
        v.addWidget(monitor_hint)

        # 调试通道
        v.addWidget(QLabel("调试"))
        dbg_row = QHBoxLayout()
        self._hero_btn = QPushButton("手动识别英雄")
        self._hero_btn.setToolTip(
            "通过客户端识别当前英雄并拉取符文/装备数据（写入正常缓存）。\n"
            "之后手动识别符文会显示该英雄的适配胜率。"
        )
        self._hero_btn.clicked.connect(self.hero_identify_requested.emit)
        self._debug_btn = QPushButton("手动识别符文")
        self._debug_btn.setToolTip(
            "按当前选择的检测屏幕截屏识别符文弹窗（OCR→查胜率→悬浮窗）。\n"
            "截图保存至 ~/.hex-mayhem-helper/debug_capture.png"
        )
        self._debug_btn.clicked.connect(
            lambda: self.debug_capture_requested.emit(
                int(self._monitor_combo.currentData() or 0)
            )
        )
        dbg_row.addWidget(self._hero_btn)
        dbg_row.addWidget(self._debug_btn)
        v.addLayout(dbg_row)
        self._debug_status = QLabel("")
        self._debug_status.setStyleSheet("color: #6fce8a; font-size: 11px;")
        self._debug_status.setWordWrap(True)
        v.addWidget(self._debug_status)
        debug_hint = QLabel("先识别英雄→再识别符文，可验证英雄适配通路")
        debug_hint.setStyleSheet("color: #8a8a8e; font-size: 11px;")
        debug_hint.setWordWrap(True)
        v.addWidget(debug_hint)

        # 按钮
        btns = QHBoxLayout()
        ok = QPushButton("保存")
        ok.clicked.connect(self._on_save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        v.addLayout(btns)

        # 水印 + 版本
        from src.version import VERSION
        wm = QLabel(f"-From AirLibrax · v{VERSION}")
        wm.setStyleSheet("color: rgba(255,255,255,0.22); font-size: 9px;")
        wm.setAlignment(Qt.AlignmentFlag.AlignRight)
        v.addWidget(wm)

    def _on_monitor_changed(self) -> None:
        """下拉框变化立即生效：写配置 + 通知检测线程（不依赖保存）"""
        sel = int(self._monitor_combo.currentData() or 0)
        self._config.set("detect_monitor", sel)
        self.monitor_changed.emit(sel)

    def set_debug_status(self, text: str) -> None:
        """调试区状态反馈（悬浮窗状态栏同步显示）"""
        self._debug_status.setText(text)

    # ---------- 拖动（无边框窗口） ----------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e) -> None:
        self._drag = None

    def _load_state(self) -> None:
        provider = self._config.get("provider")
        if provider == "hexdata":
            self._radio_hexdata.setChecked(True)
        else:
            self._radio_aramgg.setChecked(True)
        self._key_edit.setText(self._config.get("aramgg_api_key") or "")
        sel = int(self._config.get("detect_monitor") or 0)
        for i in range(self._monitor_combo.count()):
            if self._monitor_combo.itemData(i) == sel:
                self._monitor_combo.setCurrentIndex(i)
                break
        self._update_data_info()

    def _update_data_info(self) -> None:
        from ..data.cache import StatsCache
        cache = StatsCache()
        ts = cache.updated_at("champions")
        import time
        if ts:
            self._data_info.setText(f"上次更新: {time.strftime('%m-%d %H:%M', time.localtime(ts))}")
        else:
            self._data_info.setText("尚无数据")
        cache.close()

    def _on_save(self) -> None:
        new_provider = "hexdata" if self._radio_hexdata.isChecked() else "aramgg"
        old_provider = self._config.get("provider")
        new_key = self._key_edit.text().strip()
        old_key = self._config.get("aramgg_api_key") or ""
        self._config.set("aramgg_api_key", new_key)
        self._config.set("provider", new_provider)
        self._config.set("detect_monitor", int(self._monitor_combo.currentData() or 0))
        # 数据源或 Key 变化都触发重新拉取
        if new_provider != old_provider or new_key != old_key:
            self.provider_changed.emit(new_provider)
        self.settings_applied.emit()
        self.accept()
