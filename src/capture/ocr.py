"""Windows 自带 OCR（WinRT）封装：识别卡片标题文字

零依赖模型文件：Windows 10/11 系统自带 OcrEngine，支持中文。
"""
from __future__ import annotations

import asyncio
import logging

import cv2

log = logging.getLogger(__name__)

_ENGINE = None


def _get_engine():
    """懒加载 OCR 引擎（进程内复用）"""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    _ENGINE = OcrEngine.try_create_from_language(Language("zh-CN"))
    if _ENGINE is None:
        log.warning("zh-CN OCR 引擎不可用，回退用户语言")
        _ENGINE = OcrEngine.try_create_from_user_profile_languages()
    if _ENGINE is None:
        log.error("Windows OCR 引擎初始化失败（系统无 OCR 语言包？）")
    return _ENGINE


def ocr_available() -> bool:
    """OCR 引擎是否可用（供上层降级提示）"""
    return _get_engine() is not None


async def _ocr_async(img_bgr) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        return ""
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buf.tobytes())
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    sbmp = await decoder.get_software_bitmap_async()

    engine = _get_engine()
    if engine is None:
        return ""
    result = await engine.recognize_async(sbmp)
    text = "".join(line.text for line in result.lines)
    log.debug("OCR 识别结果: %r", text[:40])
    return text


def ocr_image(img_bgr) -> str:
    """同步 OCR 接口"""
    try:
        return asyncio.run(_ocr_async(img_bgr))
    except Exception:
        log.exception("OCR 失败")
        return ""
