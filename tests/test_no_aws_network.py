"""The default test run never reaches AWS (#209)."""

import asyncio

import pytest


def test_botocore_requests_are_blocked(monkeypatch):
    from strands.models.bedrock import BedrockModel

    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    model = BedrockModel(model_id="offline", region_name="us-east-1")

    async def first_event():
        async for event in model.stream([{"role": "user", "content": [{"text": "hi"}]}]):
            return event

    with pytest.raises(Exception, match="Tests must not call AWS"):
        asyncio.run(first_event())
