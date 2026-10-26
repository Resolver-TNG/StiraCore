"""Offline HTTP + real SDK regression tests for image input (#87)."""

import base64
import io
from unittest.mock import Mock

import pytest
from PIL import Image
from strands.models import Model

from dashboard.app import create_app
from stiracore.agent.nxp_agent import NXPAgent, prompt_text
from stiracore.core.checkpoint import FileCheckpointStore
from stiracore.observability.chat_images import MAX_CHAT_BYTES, MAX_IMAGE_BYTES


def image_block(fmt="png", raw=None, mime=None):
    if raw is None:
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, format=fmt)
        raw = output.getvalue()
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime or f"image/{fmt}",
            "data": base64.b64encode(raw).decode(),
        },
    }, raw


@pytest.fixture
def client(tmp_path):
    calls = []

    def agent(prompt):
        calls.append(prompt)
        return "I see an image"

    app = create_app(state_path=tmp_path / "state.json", chat_agent=agent)
    app.testing = True
    return app.test_client(), calls


@pytest.mark.parametrize(
    "fmt,mime",
    [
        ("png", "image/png"),
        ("jpeg", "image/jpeg"),
        ("jpeg", "image/jpg"),
        ("gif", "image/gif"),
        ("webp", "image/webp"),
    ],
)
def test_image_chat_passes_sdk_content_blocks(client, fmt, mime):
    http, calls = client
    image, raw = image_block(fmt, mime=mime)
    result = http.post("/api/chat", json={"message": " What is this? ", "images": [image]})
    assert result.status_code == 200
    assert "event: turn" in result.text
    assert calls == [
        [{"text": "What is this?"}, {"image": {"format": fmt, "source": {"bytes": raw}}}]
    ]
    history = http.get("/api/chat/history").json["turns"]
    assert history[0]["image_count"] == 1
    assert "base64" not in str(history) and image["source"]["data"] not in str(history)


def test_image_only_and_text_only(client):
    http, calls = client
    image, raw = image_block()
    assert "event: turn" in http.post("/api/chat", json={"images": [image]}).text
    assert calls[-1] == [{"image": {"format": "png", "source": {"bytes": raw}}}]
    assert "event: turn" in http.post("/api/chat", json={"message": " hello "}).text
    assert calls[-1] == "hello"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"message": " "},
        {"message": 4},
        {"message": "x" * 16001},
        {"images": {}},
        {"images": [None]},
        {"images": [{}]},
        {"images": [{"type": "image", "source": {"type": "url"}}]},
    ],
)
def test_bad_payload_never_invokes_agent(client, payload):
    http, calls = client
    assert http.post("/api/chat", json=payload).status_code == 400
    assert not calls


@pytest.mark.parametrize(
    "mime,raw",
    [
        ("image/svg+xml", b"<svg/>"),
        ("image/bmp", b"BMdata"),
        ("image/png", b"not an image"),
        ("image/png", b""),
        ("image/jpeg", b"GIF89afake"),
    ],
)
def test_invalid_images(client, mime, raw):
    http, calls = client
    image, _ = image_block(raw=raw, mime=mime)
    assert http.post("/api/chat", json={"images": [image]}).status_code == 400
    assert not calls


@pytest.mark.parametrize("data", ["%%%", "eA===", "あ", 123, None])
def test_invalid_base64(client, data):
    http, calls = client
    image, _ = image_block()
    image["source"]["data"] = data
    assert http.post("/api/chat", json={"images": [image]}).status_code == 400
    assert not calls


def test_mime_mismatch_and_count(client):
    http, calls = client
    image, _ = image_block("png", mime="image/jpeg")
    assert http.post("/api/chat", json={"images": [image]}).status_code == 400
    image, _ = image_block()
    assert http.post("/api/chat", json={"images": [image] * 6}).status_code == 400
    assert not calls


def test_size_boundary_and_aggregate_limit(client):
    http, calls = client
    _, png = image_block()
    # Valid PNG with padding exercises the decoded byte boundary, not a mocked limit.
    raw = png + b"\0" * (MAX_IMAGE_BYTES - len(png))
    image, _ = image_block(raw=raw)
    assert "event: turn" in http.post("/api/chat", json={"images": [image]}).text
    calls.clear()
    image, _ = image_block(raw=raw + b"\0")
    assert http.post("/api/chat", json={"images": [image]}).status_code == 413
    image, _ = image_block(raw=raw[: MAX_IMAGE_BYTES // 2 + 1])
    assert http.post("/api/chat", json={"images": [image, image]}).status_code == 413
    assert not calls


def test_http_body_limit_preserves_other_routes(client):
    http, calls = client
    result = http.post(
        "/api/chat", data=b" " * (MAX_CHAT_BYTES + 1), content_type="application/json"
    )
    assert result.status_code == 413 and result.is_json
    assert http.post("/api/stimulus", json={"padding": "x" * 5000}).status_code == 413
    assert not calls


def test_decompression_bomb_rejected(client, monkeypatch):
    http, calls = client
    image, _ = image_block()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    assert http.post("/api/chat", json={"images": [image]}).status_code == 400
    assert not calls


class VisionModel(Model):
    def __init__(self):
        self.seen = None

    def get_config(self):
        return {"model_id": "offline-vision"}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.seen = messages[0]["content"]
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "red image"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def test_http_through_real_nxp_and_strands_sdk(tmp_path, monkeypatch):
    model = VisionModel()
    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", lambda **kw: model)
    classifier = Mock()
    classifier.classify.return_value = None
    # The turn's checkpoint goes under tmp_path, not the default .local/stira (#514).
    agent = NXPAgent(
        "offline",
        "us-east-1",
        stimulus_classifier=classifier,
        checkpoint_store=FileCheckpointStore(tmp_path / "checkpoints"),
    )
    app = create_app(state_path=tmp_path / "state.json", chat_agent=agent)
    image, raw = image_block()
    result = app.test_client().post("/api/chat", json={"message": "color?", "images": [image]})
    assert "event: turn" in result.text
    assert model.seen == [
        {"text": "color?"},
        {"image": {"format": "png", "source": {"bytes": raw}}},
    ]
    classifier.classify.assert_called_once()
    assert classifier.classify.call_args.kwargs["user_message"] == "color?"
    assert prompt_text(model.seen) == "color?"
    assert prompt_text([model.seen[1]]) == ""
    assert len(list((tmp_path / "checkpoints").rglob("*.json"))) == 1
