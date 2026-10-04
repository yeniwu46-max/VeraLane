"""Local-only OCR draft extraction for user supplied receipts.

Images are decoded in memory, never persisted, and never sent to a remote service.
OCR output is untrusted input: it can only seed an editable AA itemization draft.
"""

from __future__ import annotations

import asyncio
import re
import threading
from decimal import Decimal, InvalidOperation
from io import BytesIO

import cv2
import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError


router = APIRouter(prefix="/api/aa", tags=["Local receipt OCR"])
MAX_UPLOAD_BYTES = 900 * 1024
# Keep total multipart bodies below Starlette's 1 MiB spooling threshold.
MAX_MULTIPART_BODY_BYTES = MAX_UPLOAD_BYTES + 16 * 1024
MAX_IMAGE_PIXELS = 20_000_000
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
SUMMARY_WORDS = re.compile(r"小计|合计|总计|应付|应收|实付|实收|支付金额|找零|优惠|折扣|抹零|服务费|打包费")
NON_ITEM_WORDS = re.compile(r"桌号|台号|人数|电话号码|联系电话|订单号|单号|日期|时间|地址|会员号|流水号|收银员|税号|发票号")
AMOUNT_TOKEN = re.compile(r"(?<!\d)(?:[¥￥]\s*)?((?:\d{1,6})(?:\.\d{1,2})?)(?:\s*元)?(?!\d)")
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def parse_receipt_lines(lines: list[tuple[str, float]]) -> dict:
    """Return conservative item and total candidates; do not assign participants."""
    items = []
    total_candidates: list[tuple[str, float]] = []
    raw_lines = []
    for index, (raw_text, score) in enumerate(lines):
        text = re.sub(r"\s+", " ", str(raw_text)).strip()
        if not text:
            continue
        confidence = max(0.0, min(1.0, float(score)))
        matches = list(AMOUNT_TOKEN.finditer(text))
        is_summary = bool(SUMMARY_WORDS.search(text))
        raw_lines.append({"text": text[:200], "confidence": round(confidence, 3)})
        if is_summary:
            if len(matches) == 1 and re.search(r"小计|合计|总计|应付|实付|支付金额", text):
                candidate = _yuan(matches[0].group(1))
                if candidate is not None:
                    total_candidates.append((candidate, confidence))
            continue
        if NON_ITEM_WORDS.search(text):
            continue
        if confidence < 0.55 or len(matches) != 1:
            continue
        description = (text[:matches[0].start()] + " " + text[matches[0].end():]).strip(" \t:：=—-¥￥")
        amount = _yuan(matches[0].group(1))
        if description and amount is not None and Decimal(amount) > 0:
            items.append({"key": index, "description": description[:80], "amount_yuan": amount,
                          "confidence": round(confidence, 3)})
    return {
        "items": items[:100],
        "total_candidate_yuan": total_candidates[-1][0] if total_candidates else None,
        "total_candidate_confidence": round(total_candidates[-1][1], 3) if total_candidates else None,
        "lines": raw_lines[:200],
        "warnings": [
            "OCR 仅生成草稿；请逐行核对菜名、金额和参与人。",
            "含多个金额、优惠或套餐的行不会自动拆解；请手动补录或修改。",
            "小票总额不会自动改写垫付金额，也不会直接创建收款单。",
        ],
    }


def _yuan(value: str) -> str | None:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount > 100_000:
            return None
        return f"{amount:.2f}"
    except (InvalidOperation, ValueError):
        return None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def recognize_image(image_bytes: bytes) -> dict:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(415, "只支持 JPEG、PNG 或 WebP 小票图片")
            if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
                raise HTTPException(413, "图片尺寸过大，请使用不超过 2000 万像素的图片")
            image.verify()
        decoded = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise HTTPException(422, "图片无法解码，请换一张清晰的小票照片")
    except HTTPException:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise HTTPException(422, "文件不是有效的 JPEG、PNG 或 WebP 图片") from None

    try:
        with _ENGINE_LOCK:
            result = _engine()(decoded)
        texts = list(result.txts or [])
        scores = list(result.scores or [])
    except Exception as exc:
        raise HTTPException(503, "本机 OCR 引擎暂不可用，请检查部署依赖后重试") from exc
    if not texts:
        raise HTTPException(422, "没有识别到清晰文字；可尝试裁切、旋转或重新拍摄")
    return parse_receipt_lines([(text, scores[i] if i < len(scores) else 0.0)
                                for i, text in enumerate(texts)])


@router.post("/ocr/receipt")
async def ocr_receipt(file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(415, "只支持 JPEG、PNG 或 WebP 小票图片")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "压缩后的小票图片不能超过 900 KB")
    if not content:
        raise HTTPException(422, "请选择一张小票图片")
    # CPU-bound recognition must not block the event loop. The OCR engine lock
    # serializes model access and bounds memory during simultaneous uploads.
    return await asyncio.to_thread(recognize_image, content)
