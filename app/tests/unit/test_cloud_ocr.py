import base64
import json

import pytest

from app.infrastructure.ai.ocr.cloud import CloudOCR


class Pixmap:
    def tobytes(self, kind):
        assert kind == "png"
        return b"png-bytes"


class Page:
    def get_pixmap(self, **kwargs):
        assert kwargs == {"dpi": 300, "alpha": False}
        return Pixmap()


class Response:
    def __init__(self, body): self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *args): return False


def test_cloud_ocr_sends_one_png_page_and_parses_text(monkeypatch):
    captured = {}
    def open_request(request, timeout):
        captured["headers"], captured["timeout"], captured["payload"] = request.headers, timeout, json.loads(request.data)
        return Response(b'{"text":"Recognised text"}')
    monkeypatch.setattr("app.infrastructure.ai.ocr.cloud.urlopen", open_request)
    result = CloudOCR("https://ocr.example/v1/page", "secret", 12).ocr_page(Page())
    assert result.text == "Recognised text"
    assert base64.b64decode(captured["payload"]["image_base64"]) == b"png-bytes"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 12


def test_cloud_ocr_rejects_bad_provider_response(monkeypatch):
    monkeypatch.setattr("app.infrastructure.ai.ocr.cloud.urlopen", lambda *_args, **_kwargs: Response(b'{}'))
    with pytest.raises(RuntimeError, match="invalid response"):
        CloudOCR("https://ocr.example", "secret", 1).ocr_page(Page())
