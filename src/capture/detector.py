"""海克斯符文弹窗检测与识别

流程：
1. mss 截取全屏（LOL 无边框窗口化，桌面截屏即可拿到画面）
2. 弹窗检测：符文选择弹窗固定在屏幕中央，三张卡片并排
   -> 截中央区域，转灰度，用列梯度统计找三块等宽高对比区域
3. 图标识别：若有图标模板库（assets/augment_icons/{id}.png），对每块区域做
   OpenCV 模板匹配，命中即得 augment_id
4. 名称兜底：模板未命中时返回 None，由上层提示

注意：弹窗出现时机（每局 4 次，约 4/8/12/16 级）和对局中 UI 细节以实测为准，
本模块所有阈值集中定义，便于联调校准。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import mss
import numpy as np

log = logging.getLogger(__name__)

# ---------- 可调参数（联调校准用） ----------
DETECT_INTERVAL = 1.5          # 截帧间隔（秒）
CENTER_CROP_RATIO = 0.5        # 中央裁剪高度比例（全屏高的 50%）
CARD_MIN_WIDTH_RATIO = 0.08    # 卡片最小宽度（相对屏宽）
CARD_MAX_WIDTH_RATIO = 0.20    # 卡片最大宽度
CARD_MIN_GAP = 20              # 相邻卡片最小间距（px）
EXPECTED_CARDS = 3             # 三张符文卡片
MIN_EDGE_RUN = 70              # 边框竖线最长连续边缘段阈值（px）
CLUSTER_GAP = 25               # 相近边缘簇合并间距（px）
BORDER_MAX_W = 60              # 单条边框线合并后最大宽度（px）


class ScreenCapturer:
    def __init__(self, monitor: int = 0, monitors: Optional[list[int]] = None):
        self._monitor = monitor
        self._monitors = monitors  # 检测用物理屏列表（1-based）；None=全部
        self._sct = mss.mss()

    @property
    def monitor_descs(self) -> list[tuple[int, int, int]]:
        """物理屏列表 [(序号, 宽, 高), ...]，供 UI 展示"""
        return [(i, self._sct.monitors[i]["width"], self._sct.monitors[i]["height"])
                for i in range(1, len(self._sct.monitors))]

    def grab(self, monitor: Optional[int] = None) -> np.ndarray:
        """截指定 monitor（0=全屏拼接，1=第一物理屏...），返回 BGR ndarray"""
        idx = monitor if monitor is not None else self._monitor
        shot = self._sct.grab(self._sct.monitors[idx])
        return np.array(shot, dtype=np.uint8)[:, :, :3]

    def grab_screens(self) -> list[tuple[int, np.ndarray]]:
        """按配置的屏列表截取，返回 [(屏序号, BGR帧), ...]"""
        if self._monitors:
            targets = self._monitors
        else:
            targets = list(range(1, len(self._sct.monitors)))
        return [(idx, self.grab(idx)) for idx in targets]

    @property
    def screen_count(self) -> int:
        if self._monitors:
            return len(self._monitors)
        return len(self._sct.monitors) - 1


class AugmentDetector:
    """轮询截屏，检测符文弹窗并识别三张卡片"""

    def __init__(self, capturer: ScreenCapturer,
                 callback: Optional[Callable[[list[dict]], None]] = None,
                 resolve_name: Optional[Callable[[str], Optional[str]]] = None,
                 interval: float = DETECT_INTERVAL):
        """resolve_name: OCR 文本 -> augment_id 的回调（由上层用名称库匹配）"""
        self._capturer = capturer
        self._callback = callback
        self._resolve_name = resolve_name
        self._interval = interval
        self._stop = threading.Event()

    # ---------- 弹窗检测 ----------
    def detect_augment_cards(self, frame: np.ndarray) -> Optional[list[np.ndarray]]:
        """返回三个卡片区域的 BGR 图像列表；未检测到弹窗返回 None

        策略 v2（实测校准）：LOL 海克斯弹窗卡片是深色玻璃底 + 亮色边框竖线，
        不是亮色卡片。改用边缘检测：卡片左右边框是贯穿卡片高度的连续竖线
        （最长连续边缘段 > 100px），视频噪点/字幕/装饰线都是短段。
        对每列计算最长连续边缘段长度，高簇即边框线，相邻簇成对得到卡片。
        """
        h, w = frame.shape[:2]
        # 中央区域
        top = int(h * (1 - CENTER_CROP_RATIO) / 2)
        bottom = int(h * (1 + CENTER_CROP_RATIO) / 2)
        center = frame[top:bottom, :]
        gray = cv2.cvtColor(center, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 60, 160)
        # 垂直闭运算接续边框断点（视频压缩/画质损耗）
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 1), np.uint8))

        # 每列最长连续边缘段
        col_run = np.zeros(w, np.int32)
        for x in range(w):
            ys = np.where(edges[:, x] > 0)[0]
            if len(ys) == 0:
                continue
            diff = np.diff(ys)
            breaks = np.where(diff != 1)[0]
            runs = np.diff(np.concatenate(([-1], breaks, [len(ys) - 1])))
            col_run[x] = int(runs.max())
        col_run = cv2.blur(col_run.reshape(1, -1), (1, 3)).reshape(-1).astype(np.float32)

        border = col_run >= MIN_EDGE_RUN
        regions: list[tuple[int, int]] = []
        start = None
        for i, flag in enumerate(border):
            if flag and start is None:
                start = i
            elif not flag and start is not None:
                regions.append((start, i))
                start = None
        if start is not None:
            regions.append((start, len(border)))
        if len(regions) < 2:
            return None

        # 相近簇合并为一条边框线（双线边框结构）
        lines: list[tuple[int, int]] = []
        for a, b in regions:
            if lines and a - lines[-1][1] <= CLUSTER_GAP:
                lines[-1] = (lines[-1][0], b)
            else:
                lines.append((a, b))
        lines = [(a, b) for a, b in lines if (b - a) <= BORDER_MAX_W]
        if len(lines) < 2:
            return None

        # 相邻边框线成对 -> 卡片（宽度过滤 + 间距过滤）
        min_w, max_w = int(w * CARD_MIN_WIDTH_RATIO), int(w * CARD_MAX_WIDTH_RATIO)
        candidates: list[tuple[int, int, int]] = []  # (左缘, 右缘, 距中央距离)
        for i in range(len(lines) - 1):
            left = lines[i][0]
            right = lines[i + 1][1]
            width = right - left
            if not (min_w <= width <= max_w):
                continue
            gap_prev_ok = not candidates or (left - candidates[-1][1]) >= CARD_MIN_GAP
            if not gap_prev_ok:
                continue
            dist = abs((left + right) / 2 - w / 2)
            candidates.append((left, right, dist))
        if len(candidates) < EXPECTED_CARDS:
            return None
        # 取最靠近屏幕中央的三张
        candidates = sorted(candidates, key=lambda c: c[2])[:EXPECTED_CARDS]
        candidates = sorted(candidates, key=lambda c: c[0])
        return [center[:, a:b] for a, b, _ in candidates]

    # ---------- （模板匹配已移除，识别走 OCR） ----------
    # ---------- 主循环 ----------
    def analyze_frame(self, frame: np.ndarray) -> list[dict]:
        """分析一帧：检测弹窗 -> 识别图标。返回识别结果列表（空=无弹窗）。
        供主循环与手动 debug 通道复用。"""
        cards = self.detect_augment_cards(frame)
        if not cards:
            return []
        return self._identify(cards)

    def analyze_screens(self) -> tuple[list[dict], Optional[int]]:
        """逐屏分析：返回 (识别结果, 命中屏序号)；无命中返回 ([], None)。
        同一时刻只有一个符文弹窗，取第一个命中的屏。"""
        for idx, frame in self._capturer.grab_screens():
            results = self.analyze_frame(frame)
            if results:
                return results, idx
        return [], None

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="augment-detect").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        """状态机：弹窗出现即识别；重 roll（内容变化）立即重识别；
        无时间冷却，避免攒两次选择机会连弹时漏掉第二次。"""
        log.info("符文检测线程启动（%d 个屏幕）", self._capturer.screen_count)
        prev_visible = False
        prev_sig: Optional[tuple] = None
        err_count = 0
        while not self._stop.is_set():
            try:
                results, _ = self.analyze_screens()
                err_count = 0
                if results:
                    sig = tuple(sorted(r.get("augment_id") for r in results))
                    # 弹窗新出现，或内容变化（重 roll）才推送
                    if not prev_visible or sig != prev_sig:
                        prev_visible = True
                        prev_sig = sig
                        if self._callback:
                            self._callback(results)
                else:
                    # 弹窗消失：重置状态，下次出现立即识别
                    prev_visible = False
                    prev_sig = None
            except Exception:
                err_count += 1
                log.exception("符文检测异常（连续 %d 次）", err_count)
                if err_count >= 10:
                    # 熔断：连续异常说明环境有问题，暂停 30 秒再恢复
                    log.error("检测连续异常 %d 次，暂停 30 秒", err_count)
                    self._stop.wait(30)
                    err_count = 0
            self._stop.wait(self._interval)
        log.info("符文检测线程退出")

    def _identify(self, cards: list[np.ndarray]) -> list[dict]:
        """OCR 读卡片文字 -> 上层名称解析。返回 [{"augment_id", "ocr_text", "matched"}]

        整卡 OCR（不猜标题位置）：不同画面弹窗卡片位置会变，固定比例裁剪
        会裁到描述文字。整卡识别后由匹配逻辑取"最早出现的库名"（标题在
        OCR 输出最前），天然命中标题。
        """
        from .ocr import ocr_image
        results = []
        for card in cards:
            h, w = card.shape[:2]
            # 卡片主体：去掉上下边框残留（10%~90%）
            body = card[int(h * 0.10):int(h * 0.90), :]
            if body.size == 0:
                results.append({"augment_id": None, "ocr_text": "", "matched": False})
                continue
            body = cv2.resize(body, None, fx=1.8, fy=1.8,
                              interpolation=cv2.INTER_CUBIC)
            text = ocr_image(body)
            aug_id = None
            if text and self._resolve_name:
                try:
                    aug_id = self._resolve_name(text)
                except Exception:
                    log.exception("名称解析失败: %r", text)
            results.append({
                "augment_id": aug_id,
                "ocr_text": text,
                "matched": bool(aug_id),
            })
        return results
