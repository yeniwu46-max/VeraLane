import asyncio
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from starlette import formparsers

from app import db, receipt_ocr
from app.main import ReceiptBodyLimitMiddleware, app


def test_parser_separates_item_candidates_from_totals_and_ambiguous_lines():
    result = receipt_ocr.parse_receipt_lines([
        ("宫保鸡丁 38.00", 0.98),
        ("米饭 ¥3.00", 0.94),
        ("合计 41.00", 0.99),
        ("套餐 18.00 2份 36.00", 0.99),
        ("模糊字 8.00", 0.32),
        ("优惠 -5.00", 0.98),
        ("桌号 03", 0.99),
    ])
    assert [(item["description"], item["amount_yuan"]) for item in result["items"]] == [
        ("宫保鸡丁", "38.00"), ("米饭", "3.00")
    ]
    assert result["total_candidate_yuan"] == "41.00"
    assert result["total_candidate_confidence"] == 0.99
    assert len(result["lines"]) == 7
    assert len(result["warnings"]) == 3


def test_upload_uses_local_engine_and_rejects_bad_images(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "receipt.sqlite3")
    class FakeOutput:
        txts = ("红烧豆腐 16.80", "合计 16.80")
        scores = (0.98, 0.99)
    class FakeEngine:
        def __call__(self, image):
            assert image.shape == (80, 120, 3)
            return FakeOutput()
    monkeypatch.setattr(receipt_ocr, "_ENGINE", FakeEngine())
    image = Image.new("RGB", (120, 80), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/aa/ocr/receipt", files={"file": ("receipt.png", buffer.getvalue(), "image/png")})
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["amount_yuan"] == "16.80"
        assert body["total_candidate_yuan"] == "16.80"
        assert client.post("/api/aa/ocr/receipt", files={"file": ("receipt.jpg", b"not an image", "image/jpeg")}).status_code == 422
        assert client.post("/api/aa/ocr/receipt", files={"file": ("receipt.gif", b"x", "image/gif")}).status_code == 415
        assert client.post("/api/aa/ocr/receipt", files={"file": ("big.png", b"x" * (receipt_ocr.MAX_UPLOAD_BYTES + 1), "image/png")}).status_code == 413


def test_oversized_multipart_is_rejected_before_temporary_file_spooling(monkeypatch):
    spool_calls = []
    original = formparsers.SpooledTemporaryFile

    def observe_spool(*args, **kwargs):
        spool_calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", observe_spool)
    large_payload = b"x" * (receipt_ocr.MAX_MULTIPART_BODY_BYTES + 1)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/aa/ocr/receipt",
            files={"file": ("oversized.png", large_payload, "image/png")},
            headers={"Origin": "http://127.0.0.1:5173"},
        )
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert response.headers["x-frame-options"] == "DENY"
    assert spool_calls == []


def test_chunked_oversized_receipt_body_is_stopped_before_parser():
    sent = []
    messages = iter([
        {"type": "http.request", "body": b"x" * 600_000, "more_body": True},
        {"type": "http.request", "body": b"x" * 400_000, "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
    ])

    async def receive():
        return next(messages)

    async def downstream(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass

    async def send(message):
        sent.append(message)

    asyncio.run(ReceiptBodyLimitMiddleware(downstream)({
        "type": "http", "method": "POST", "path": "/api/aa/ocr/receipt", "headers": [],
    }, receive, send))
    assert sent[0]["status"] == 413
