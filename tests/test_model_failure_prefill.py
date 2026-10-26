"""#256: a tool call written as plain text ends in Bedrock's "assistant message prefill" error.

Reproduced offline (no Bedrock call): a fake model answers with only text but the stop
reason ``tool_use`` (the model wrote its tool call as plain text). Strands then appends a
tool-result message with no results (``{"role": "user", "content": []}``), and Bedrock's
own message formatter (``BedrockModel._format_bedrock_messages``) drops empty messages, so
the next request ends with the assistant's text. Bedrock rejects that (models without
prefill support: ``ValidationException`` ... "must end with a user message").

The chat names it (``failure: ends_with_assistant``) instead of the generic 4xx text
("check the model name and settings"), which pointed at the wrong fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("botocore")

from botocore.exceptions import ClientError
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.models.model import Model

from stiracore.providers.failure import (
    MESSAGES,
    REASONS,
    describe_model_failure,
)

# Bedrock's wording for the refused request (what the fake raises; the text never leaves).
PREFILL = (
    "This model does not support assistant message prefill. "
    "The conversation must end with a user message."
)
TEXT_TOOL_CALL = '<function_calls><invoke name="file_read">secret-path</invoke></function_calls>'


def _client_error(message: str, code: str = "ValidationException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        "ConverseStream",
    )


class _TextToolCallModel(Model):
    """Answers with a tool call written as text and stop reason ``tool_use``; refuses, as
    Bedrock does, a request whose formatted conversation ends with the assistant."""

    def __init__(self):
        self.formatter = BedrockModel(model_id="test-model", region_name="us-east-1")
        self.sent_roles: list[list[str]] = []

    def update_config(self, **_kwargs):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *_args, **_kwargs):  # pragma: no cover - unused
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **_kwargs):
        sent = self.formatter._format_bedrock_messages(messages)
        self.sent_roles.append([message["role"] for message in sent])
        if sent[-1]["role"] != "user":
            raise _client_error(PREFILL)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": TEXT_TOOL_CALL}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def test_a_text_tool_call_makes_the_next_request_end_with_the_assistant():
    model = _TextToolCallModel()
    agent = Agent(model=model, callback_handler=None)
    with pytest.raises(Exception) as caught:
        agent("hello")
    # The first request ends with the user; the second (after the empty tool-result
    # message was dropped by the Bedrock formatter) ends with the assistant's text.
    assert model.sent_roles == [["user"], ["user", "assistant"]]
    assert (agent.messages[-1]["role"], agent.messages[-1]["content"]) == ("user", [])
    failure = describe_model_failure(caught.value)
    assert failure is not None
    assert failure["reason"] == "ends_with_assistant"
    assert failure["http_status"] == 400
    # Only the fixed text: never Bedrock's message or the conversation.
    assert failure["error"] == " / ".join(MESSAGES["ends_with_assistant"])
    assert "secret-path" not in json.dumps(failure)
    assert PREFILL not in json.dumps(failure)


def test_other_validation_errors_stay_bad_request():
    failure = describe_model_failure(_client_error("messages.0: text field is blank"))
    assert failure is not None and failure["reason"] == "bad_request"


def test_the_reason_is_listed_and_has_both_catalog_texts():
    assert "ends_with_assistant" in REASONS
    root = Path(__file__).resolve().parents[1] / "dashboard/static/i18n"
    for lang in ("ja", "en"):
        catalog = json.loads((root / f"catalog.{lang}.json").read_text(encoding="utf-8"))
        assert catalog["chat.model_failure_ends_with_assistant"]


@pytest.mark.parametrize(
    "message",
    [
        "This model does not support assistant message prefill.",
        "The conversation must end with a user message.",
    ],
    ids=["prefill-marker", "user-last-marker"],
)
def test_each_marker_alone_is_enough(message):
    failure = describe_model_failure(_client_error(message))
    assert failure is not None and failure["reason"] == "ends_with_assistant"


def test_the_same_text_under_another_code_is_not_this_reason():
    failure = describe_model_failure(_client_error(PREFILL, code="AccessDeniedException"))
    assert failure is not None and failure["reason"] != "ends_with_assistant"


def test_a_wrapped_refusal_is_still_named():
    try:
        try:
            raise _client_error(PREFILL)
        except ClientError as inner:
            raise RuntimeError("event loop failed") from inner
    except RuntimeError as outer:
        failure = describe_model_failure(outer)
    assert failure is not None and failure["reason"] == "ends_with_assistant"
