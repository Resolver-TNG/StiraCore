import base64
import io
from unittest.mock import Mock

import pytest
from PIL import Image
from strands import Agent

from stiracore.tools import file_tools
from stiracore.tools.file_tools import file_read, view_image


def test_text_and_tilde(tmp_path, monkeypatch):
    # Path.expanduser() reads HOME on POSIX but USERPROFILE (or HOMEDRIVE+HOMEPATH)
    # on Windows. Set all of them so ~ resolves to tmp_path on every platform.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive)
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive) :])
    # newline="" stops Windows from rewriting \n to \r\n on write, so the
    # assertion checks BOM stripping (utf-8-sig) rather than line-ending policy.
    (tmp_path / "text.txt").write_text("日本語\nhello", encoding="utf-8-sig", newline="")
    result = file_read("~/text.txt")
    assert result["status"] == "success"
    # The text first, unchanged; then the observation block (#307).
    assert result["content"][0] == {"text": "日本語\nhello"}
    assert result["content"][1]["text"].startswith(file_tools.OBSERVATION_MARKER + " ")
    assert len(result["content"]) == 2


@pytest.mark.parametrize("data", [b"\xff", b"a\x00b"])
def test_binary_rejected(tmp_path, data):
    path = tmp_path / "binary"
    path.write_bytes(data)
    assert file_read(str(path))["status"] == "error"


def test_file_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(file_tools, "MAX_TEXT_BYTES", 3)
    path = tmp_path / "text"
    path.write_bytes(b"abc")
    assert file_read(str(path))["status"] == "success"
    path.write_bytes(b"abcd")
    assert "limit" in file_read(str(path))["content"][0]["text"]


def test_missing_directory_and_permission_denied(tmp_path, monkeypatch):
    assert file_read(str(tmp_path / "missing"))["status"] == "error"
    assert file_read(str(tmp_path))["status"] == "error"
    monkeypatch.setattr(file_tools, "_read_bounded", Mock(side_effect=PermissionError("denied")))
    assert file_read("denied")["status"] == "error"
    assert view_image("denied")["status"] == "error"


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_images_return_native_blocks_and_base64(tmp_path, fmt):
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format=fmt)
    path = tmp_path / "image.wrong-extension"
    path.write_bytes(stream.getvalue())
    result = view_image(str(path))
    assert result["status"] == "success"
    assert result["mime_type"] == f"image/{fmt.lower()}"
    assert base64.b64decode(result["base64"]) == stream.getvalue()
    assert result["content"][0]["image"] == {
        "format": fmt.lower(),
        "source": {"bytes": stream.getvalue()},
    }


def test_image_invalid_format_size_and_dimensions(tmp_path, monkeypatch):
    path = tmp_path / "image"
    path.write_text("not an image")
    assert view_image(str(path))["status"] == "error"
    Image.new("RGB", (2, 2)).save(path, format="BMP")
    assert view_image(str(path))["status"] == "error"
    Image.new("RGB", (2, 2)).save(path, format="PNG")
    monkeypatch.setattr(file_tools, "MAX_IMAGE_DIMENSION", 1)
    assert view_image(str(path))["status"] == "error"
    monkeypatch.setattr(file_tools, "MAX_IMAGE_BYTES", 1)
    assert view_image(str(path))["status"] == "error"


def test_image_survives_real_sdk_execution(tmp_path):
    from strands.models.bedrock import BedrockModel

    path = tmp_path / "image.png"
    Image.new("RGB", (2, 2)).save(path)
    agent = Agent(model=Mock(), tools=[view_image, file_read], callback_handler=None)
    result = agent.tool.view_image(path=str(path))
    assert result["status"] == "success"
    assert result["content"][0]["image"]["source"]["bytes"] == path.read_bytes()
    assert result["toolUseId"]
    # Exercise the actual provider serializer without creating an AWS client.
    model = object.__new__(BedrockModel)
    model.config = {"model_id": "test-model"}
    formatted = model._format_request_message_content({"toolResult": result})
    assert formatted["toolResult"]["content"][0]["image"]["source"]["bytes"] == path.read_bytes()
