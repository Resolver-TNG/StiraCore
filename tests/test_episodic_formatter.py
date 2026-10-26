"""Episodic Memory summaries respect meaning boundaries (#77).

Offline: no Bedrock. The MemoryStore, ContextAssembler and StiraEngine are real;
only the Strands agent is replaced in the stira_server runner test.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from stiracore.agent.context_assembler import ContextAssembler
from stiracore.core import NeuralState, StiraConfig, StiraEngine
from stiracore.tools.episodic_formatter import (
    ELLIPSIS,
    EpisodeSummary,
    EpisodicFormatter,
    fallback_format,
    record_episode,
    redact_secrets,
    shorten_unit,
    shorten_url,
)
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

ROOT = Path(__file__).resolve().parent.parent

JP_USER = (
    "昨日の実験結果について相談したい。サンプルAの反応速度が想定より三割遅く、"
    "温度条件を変えても改善しなかった。試薬のロット差も疑ったが、同じロットでも再現した。"
    "測定系の問題なのか、反応機構そのものの見直しが必要なのか、どちらだと思う？"
)
JP_CONCLUSION = "結論として、まず測定系を疑い、標準試料で再現しなければ反応機構の見直しに進むのが良いと思います。"
JP_AGENT = (
    "なるほど、興味深い結果ですね。まず確認したいのは測定系の校正履歴です。"
    "温度を変えても改善しないなら、律速段階が温度依存の小さい過程に移っている可能性があります。"
    "ロット差を否定できたのは大きな前進です。"
    "反応次数の再評価や、中間体の検出も並行して検討できます。"
    "ただし装置の条件がそろっていないと比較は難しいので、条件表を先に作りましょう。" + JP_CONCLUSION
)

_SENTENCE_ENDS = ("。", "！", "？", "!", "?", ".", ELLIPSIS, "省略]")


@pytest.fixture
def formatter():
    return EpisodicFormatter(user_label="マスター", agent_label="リゾルバ")


def _speaker_texts(content: str) -> tuple[str, str]:
    match = re.fullmatch(r"マスター: (.*) → リゾルバ: (.*)", content, re.DOTALL)
    assert match, content
    return match.group(1), match.group(2)


def _pieces(text: str) -> list[str]:
    return [piece.strip() for piece in text.split(ELLIPSIS) if piece.strip()]


def _assert_whole_sentences(summary_text: str, original: str) -> None:
    """Every kept piece is verbatim source text ending on a sentence boundary."""
    flat = re.sub(r"\s+", "", original)
    for piece in _pieces(summary_text):
        assert re.sub(r"\s+", "", piece) in flat, piece
        assert piece.endswith(_SENTENCE_ENDS), f"cut mid-sentence: {piece!r}"


# ---------------------------------------------------------------- Japanese


def test_japanese_user_over_100_chars_is_not_cut_mid_sentence(formatter):
    assert len(JP_USER) > 100  # the old code kept message[:100]
    summary = formatter.format(JP_USER, "了解です。")
    user, _ = _speaker_texts(summary.content)
    # The old slice cut inside the final question; now it is kept whole.
    assert JP_USER[:100] not in user or user == JP_USER
    assert user.endswith("どちらだと思う？")
    _assert_whole_sentences(user, JP_USER)


def test_long_japanese_user_keeps_opening_and_trailing_question():
    formatter = EpisodicFormatter("マスター", "リゾルバ", user_budget=100)
    long_user = JP_USER.replace("どちらだと思う？", "") * 2 + "どちらだと思う？"
    summary = formatter.format(long_user, "はい。")
    user, _ = _speaker_texts(summary.content)
    assert summary.user.truncated
    assert len(user) <= 100
    assert user.startswith("昨日の実験結果について相談したい。")
    assert user.endswith("どちらだと思う？")
    _assert_whole_sentences(user, long_user)


def test_agent_over_200_chars_keeps_conclusion(formatter):
    assert len(JP_AGENT) > 200 and JP_CONCLUSION not in JP_AGENT[:200]
    summary = formatter.format(JP_USER, JP_AGENT)
    _, agent = _speaker_texts(summary.content)
    assert JP_CONCLUSION in agent
    _assert_whole_sentences(agent, JP_AGENT)


def test_agent_conclusion_survives_tight_budget():
    formatter = EpisodicFormatter("マスター", "リゾルバ", agent_budget=120)
    summary = formatter.format(JP_USER, JP_AGENT * 3)
    _, agent = _speaker_texts(summary.content)
    assert summary.agent.truncated and len(agent) <= 120
    assert JP_CONCLUSION in agent


def test_single_oversized_sentence_is_cut_at_clause_not_mid_word():
    sentence = "、".join(f"条件{i}を確認した結果として温度依存性は小さい" for i in range(20)) + "。"
    cut = shorten_unit(sentence, 80)
    assert len(cut) <= 80 and cut.endswith(ELLIPSIS)
    assert cut[:-1].endswith("小さい")  # the clause before a 、 is intact


def test_hard_cap_is_last_resort_and_enforced():
    formatter = EpisodicFormatter(
        "マスター", "リゾルバ", user_budget=400, agent_budget=400, max_chars=200
    )
    summary = formatter.format("あ" * 1000, "い" * 1000)
    assert len(summary.content) <= 200
    assert summary.content.endswith(ELLIPSIS)


def test_short_turn_is_stored_verbatim(formatter):
    summary = formatter.format("おはよう", "おはようございます、マスター。")
    assert summary.content == "マスター: おはよう → リゾルバ: おはようございます、マスター。"
    assert not summary.user.truncated and not summary.agent.truncated


# ---------------------------------------------------------------- structure


def test_bullets_are_kept_as_whole_items(formatter):
    response = (
        "次の手順で進めましょう。\n"
        "- 校正ログを確認する\n"
        "* 標準試料で再測定する\n"
        "1. 反応次数を再評価する\n"
        "・中間体の検出を試す\n"
    )
    _, agent = _speaker_texts(formatter.format("手順は？", response).content)
    for item in (
        "校正ログを確認する",
        "標準試料で再測定する",
        "反応次数を再評価する",
        "中間体の検出を試す",
    ):
        assert f"・{item}" in agent
    assert "- " not in agent and "1. " not in agent


def test_code_block_is_replaced_not_stored(formatter):
    code = "\n".join(f"line_{i} = compute({i})  # 。途中の句点" for i in range(40))
    response = f"実装しました。\n\n```python\n{code}\n```\n\n動作確認済みです。"
    summary = formatter.format("書いて", response)
    _, agent = _speaker_texts(summary.content)
    assert "line_" not in agent and "compute(" not in agent
    assert "[コード: python 40行 省略]" in agent
    assert agent.startswith("実装しました。") and agent.endswith("動作確認済みです。")
    assert summary.metadata()["omitted_code_blocks"] == 1


def test_unclosed_code_fence_is_still_omitted(formatter):
    _, agent = _speaker_texts(
        formatter.format("q", "説明です。\n```\nsecret_logic()\nmore()").content
    )
    assert "secret_logic" not in agent and "省略]" in agent


def test_long_url_is_shortened_atomically(formatter):
    url = "https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html?x=1#section-2"
    response = f"詳細は {url} を参照してください。結論として、Bedrockで問題ありません。"
    _, agent = _speaker_texts(formatter.format("どこ？", response).content)
    urls = re.findall(r"https?://[^\s…]+…?", agent)
    assert len(urls) == 1
    assert urls[0].startswith("https://docs.aws.amazon.com/") and urls[0].endswith("/…")
    assert len(urls[0]) <= 60 and "what-is-bedrock" not in urls[0]
    assert "結論として、Bedrockで問題ありません。" in agent


def test_url_dots_and_version_numbers_do_not_split_sentences():
    formatter = EpisodicFormatter("U", "A", agent_budget=80)
    response = (
        "Use boto3 v1.42.3 with Python 3.11. See https://example.com/a.b.c/d.html now. "
        "Therefore, upgrade first."
    )
    _, agent = re.fullmatch(r"U: (.*) → A: (.*)", formatter.format("q", response).content).groups()
    assert "Therefore, upgrade first." in agent
    for piece in _pieces(agent):
        assert "v1.4" not in piece or "v1.42.3" in piece
        assert "https://example.com/a" not in piece or "https://example.com/a.b.c/d.html" in piece


def test_hard_cut_never_splits_a_url():
    text = "参照先は " + "https://example.com/" + "a" * 30 + " にあります"
    cut = shorten_unit(text, 30)
    assert "https://" not in cut or re.search(r"https://example\.com/a{30}", cut)


def test_shorten_url_keeps_whole_path_segments():
    assert shorten_url("https://example.com/short") == "https://example.com/short"
    short = shorten_url("https://example.com/alpha/beta/gamma/" + "x" * 80, 40)
    assert short.startswith("https://example.com/") and short.endswith("/…")
    assert len(short) <= 40


def test_mixed_english_japanese_boundaries(formatter):
    response = (
        "It works. ただしcold startが遅いです。Lambdaのmemoryを1024MBにすると改善します! "
        "In short, メモリを上げるのが最短です。"
    )
    _, agent = _speaker_texts(formatter.format("Lambda遅い？", response).content)
    assert agent == (
        "It works. ただしcold startが遅いです。Lambdaのmemoryを1024MBにすると改善します! "
        "In short, メモリを上げるのが最短です。"
    )


def test_mixed_language_truncation_keeps_conclusion():
    formatter = EpisodicFormatter("U", "A", agent_budget=90)
    response = (
        "First, we checked the logs and the metrics dashboard for anomalies. "
        "次にCloudWatchのアラーム履歴を確認しました。"
        "Several retries were observed during the peak window. "
        "In short, スロットリングが原因です。"
    )
    _, agent = re.fullmatch(r"U: (.*) → A: (.*)", formatter.format("q", response).content).groups()
    assert "In short, スロットリングが原因です。" in agent
    assert len(agent) <= 90


# ---------------------------------------------------------------- sanitization


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "password=hunter2secret",
        "api_key: sk-abcdefghijklmnopqrstuvwxyz012345",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_" + "a" * 36,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_secrets_are_redacted(formatter, secret):
    summary = formatter.format(f"これを使って {secret} で接続して", "了解しました。")
    for fragment in (
        "hunter2secret",
        "AKIAABCDEFGHIJKLMNOP",
        "sk-abcdef",
        "ghp_aaaa",
        "eyJhbGci",
        "abcdefghijklmnopqrstuvwxyz0123456789",
    ):
        assert fragment not in summary.content
    assert "[REDACTED]" in summary.content
    assert summary.metadata()["redactions"] >= 1


def test_redact_keeps_key_names():
    text, count = redact_secrets("aws_secret_access_key = abcdEFGH1234")
    assert text == "aws_secret_access_key = [REDACTED]" and count == 1


@pytest.mark.parametrize("key", ["password", "secret", "token", "api_key", "credential"])
def test_redact_json_quoted_secret_keys(key):
    text, count = redact_secrets(f'{{"{key}": "EXAMPLE TEST SECRET", "safe": "keep"}}')
    assert text == f'{{"{key}": [REDACTED], "safe": "keep"}}'
    assert count == 1


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('{"password": "secret123"}', '{"password": [REDACTED]}'),
        ("password='EXAMPLE TEST SECRET'", "password=[REDACTED]"),
        ('password="EXAMPLE TEST SECRET"', "password=[REDACTED]"),
        (r'password="EXAMPLE \"TEST\" SECRET"', "password=[REDACTED]"),
        (r"password='EXAMPLE \'TEST\' SECRET'", "password=[REDACTED]"),
        ("'password': 'EXAMPLE TEST SECRET'", "'password': [REDACTED]"),
        ('{"TOKEN": "x"}', '{"TOKEN": [REDACTED]}'),
        ("pwd=abc", "pwd=[REDACTED]"),
        ("api-key: example-value, safe=keep", "api-key: [REDACTED], safe=keep"),
        ("client_secret=example-value&safe=keep", "client_secret=[REDACTED]&safe=keep"),
    ],
)
def test_redact_quoted_and_existing_assignments(source, expected):
    assert redact_secrets(source) == (expected, 1)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://user:pass@host.com", "https://***:***@host.com"),
        (
            "http://u:p@example.com:8080/path?q=ok#end",
            "http://***:***@example.com:8080/path?q=ok#end",
        ),
        ("postgresql://u:p%40ss@[::1]:5432/db", "postgresql://***:***@[::1]:5432/db"),
        ("https://username@example.com/", "https://***:***@example.com/"),
        ("https://user:@example.com/", "https://***:***@example.com/"),
    ],
)
def test_redact_url_userinfo(source, expected):
    assert redact_secrets(source) == (expected, 1)


def test_redaction_leaves_non_credentials_unchanged():
    text = 'notpassword=keep {"description": "keep"} https://example.com/path/u:p@file?q=a:b@c'
    assert redact_secrets(text) == (text, 0)


@pytest.mark.parametrize("use_fallback", [False, True])
def test_record_episode_masks_all_secret_forms(tmp_path, use_fallback):
    source = '{"password": "secret123"} password=\'EXAMPLE TEST SECRET\' https://user:pass@host.com'
    store = MemoryStore(tmp_path / "memory", "test-agent")
    formatter = _BrokenFormatter() if use_fallback else EpisodicFormatter()
    entry = record_episode(store, source, source, formatter=formatter)
    assert entry is not None
    assert entry.metadata["method"] == ("fallback" if use_fallback else "deterministic")
    for secret in ("secret123", "EXAMPLE", "TEST SECRET", "user:pass"):
        assert secret not in entry.content
    assert entry.content.count("[REDACTED]") == 4
    assert entry.content.count("https://***:***@host.com") == 2


def test_private_key_block_is_redacted(formatter):
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY-----"
    summary = formatter.format(f"鍵です\n{pem}", "受け取りました。")
    assert "MIIEow" not in summary.content and "[REDACTED]" in summary.content


def test_tool_output_is_omitted(formatter):
    blob = '{"ResponseMetadata": {"RequestId": "abc", "HTTPStatusCode": 200}, "Items": [1, 2, 3, 4, 5]}'
    response = (
        "確認しました。\n<tool_result>\nraw stdout line 1\nraw stdout line 2\n</tool_result>\n"
        f"{blob}\n\n結論として、テーブルは正常です。"
    )
    summary = formatter.format("DynamoDB大丈夫？", response)
    _, agent = _speaker_texts(summary.content)
    assert "raw stdout" not in agent and "RequestId" not in agent
    assert "[ツール出力 省略]" in agent
    assert "結論として、テーブルは正常です。" in agent
    assert summary.metadata()["omitted_tool_outputs"] == 2


def test_metadata_never_duplicates_raw_text(formatter):
    metadata = formatter.format(JP_USER, JP_AGENT).metadata()
    assert metadata["method"] == "deterministic"
    assert metadata["user_chars"] == len(JP_USER)
    assert all(not isinstance(value, str) or len(value) < 40 for value in metadata.values())


def test_formatter_is_deterministic(formatter):
    assert formatter.format(JP_USER, JP_AGENT) == formatter.format(JP_USER, JP_AGENT)


# ---------------------------------------------------------------- failure isolation


class _BrokenFormatter:
    user_label = "マスター"
    agent_label = "リゾルバ"

    def format(self, user_message, agent_response):
        raise RuntimeError("summarizer exploded")


class _BrokenStore:
    def write(self, *args, **kwargs):
        raise OSError("disk full")


def test_formatter_failure_stores_deterministic_fallback(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    entry = record_episode(
        store, JP_USER, JP_AGENT + "\n```\nrm -rf /\n```", formatter=_BrokenFormatter()
    )
    assert entry is not None
    assert entry.metadata["method"] == "fallback"
    assert entry.content.startswith("マスター: ")
    assert "rm -rf" not in entry.content
    user, agent = _speaker_texts(entry.content)
    assert len(user) <= 160 and len(agent) <= 320
    assert user.endswith(("。", "？", ELLIPSIS))


def test_fallback_format_handles_unprintable_response():
    class _Unprintable:
        def __str__(self):
            raise ValueError("no str")

    summary = fallback_format("やあ", _Unprintable(), user_label="U", agent_label="A")
    assert summary.content == "U: やあ → A: (空)"


def test_store_failure_does_not_raise():
    assert record_episode(_BrokenStore(), "こんにちは", "こんにちは。") is None


def test_record_episode_rejects_bad_formatter_output(tmp_path):
    class _Bad:
        def format(self, user_message, agent_response):
            return "not a summary"

    store = MemoryStore(tmp_path / "memory", "stira")
    entry = record_episode(store, "q", "a", formatter=_Bad())
    assert entry is not None and entry.metadata["method"] == "fallback"


def test_record_episode_writes_structured_metadata(tmp_path, formatter):
    store = MemoryStore(tmp_path / "memory", "stira")
    entry = record_episode(
        store,
        JP_USER,
        JP_AGENT,
        formatter=formatter,
        key="turn-1",
        source="demo",
        extra_metadata={"turn": 1},
    )
    assert entry.key == "turn-1" and entry.layer is MemoryLayer.EPISODIC
    assert entry.metadata["source"] == "demo" and entry.metadata["turn"] == 1
    assert entry.metadata["format"] == "episodic-summary/v1"
    assert isinstance(formatter.format(JP_USER, JP_AGENT), EpisodeSummary)


# ---------------------------------------------------------------- integration


def test_stored_episodes_assemble_into_meaningful_context(tmp_path, formatter):
    store = MemoryStore(tmp_path / "memory", "stira")
    record_episode(store, JP_USER, JP_AGENT, formatter=formatter, key="chat-1")
    record_episode(
        store,
        "Lambdaのコールドスタートは？",
        "It works. ただしcold startが遅いです。In short, メモリを上げるのが最短です。",
        formatter=formatter,
        key="chat-2",
    )
    engine = StiraEngine(state=NeuralState(), config=StiraConfig())
    assembler = ContextAssembler(
        "stira", store, engine, tmp_path / "missing-soul.md", include_identity=False
    )
    prompt = assembler.build_system_prompt()
    section = prompt.split("## Episodic Memory\n", 1)[1].split("\n\n## ", 1)[0]
    lines = section.splitlines()
    assert len(lines) == 2  # one line per episode, each a bullet
    assert all(line.startswith("- マスター: ") and " → リゾルバ: " in line for line in lines)
    assert JP_CONCLUSION in section
    assert "メモリを上げるのが最短です。" in section
    for line in lines:
        user, agent = _speaker_texts(line[2:])
        assert user and agent


@pytest.fixture
def stira_server():
    spec = importlib.util.spec_from_file_location(
        "stira_server_episodic_under_test", ROOT / "dashboard" / "stira_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app_runner(stira_server, tmp_path, monkeypatch, response):
    captured = {}

    class _FakeAgent:
        def __init__(self, *, state, config, on_breaker=None, tools=(), **_):
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tools

        def __call__(self, message):
            if isinstance(response, Exception):
                raise response
            return response

    real_blueprint = stira_server.create_chat_blueprint

    def _capture(agent, **kwargs):
        captured["runner"] = kwargs["runner"]
        return real_blueprint(agent, **kwargs)

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "stira")
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _FakeAgent)
    monkeypatch.setattr(stira_server, "create_chat_blueprint", _capture)
    stira_server.create_app()
    store = MemoryStore(tmp_path / "stira" / "memory", "stira")
    return captured["runner"], store


def test_stira_runner_stores_boundary_aware_episode(stira_server, tmp_path, monkeypatch):
    runner, store = _app_runner(stira_server, tmp_path, monkeypatch, JP_AGENT)
    assert runner(JP_USER) == JP_AGENT
    (entry,) = store.list_entries(MemoryLayer.EPISODIC, limit=10)
    assert entry.metadata["source"] == "web-chat"
    assert entry.metadata["method"] == "deterministic"
    user, agent = _speaker_texts(entry.content)
    assert user.endswith("どちらだと思う？")
    assert JP_CONCLUSION in agent


def test_stira_runner_records_original_turn_importance(stira_server, tmp_path, monkeypatch):
    """#293: the server path scores the unsummarized turn (heading/bullets/code)."""
    user = "キャッシュ層の設計はどうする？"
    reply = (
        "## 結論\n- 読み取りは Redis を先に見る\n- 書き込み時に無効化する\n\n"
        "```python\ncache.invalidate(key)\n```\n\n前回の不具合はこれで防げる。"
    )
    runner, store = _app_runner(stira_server, tmp_path, monkeypatch, reply)
    assert runner(user) == reply
    (entry,) = store.list_entries(MemoryLayer.EPISODIC, limit=10)
    assert entry.metadata["importance"] == pytest.approx(0.8)
    assert "```" not in entry.content  # the summary itself is unchanged


def test_stira_image_prompt_keeps_binary_out_of_episode(stira_server, tmp_path, monkeypatch):
    runner, store = _app_runner(stira_server, tmp_path, monkeypatch, "赤い画像です。")
    runner(
        [
            {"text": "何色ですか？"},
            {"image": {"format": "png", "source": {"bytes": b"private-image-payload"}}},
        ]
    )
    (entry,) = store.list_entries(MemoryLayer.EPISODIC, limit=10)
    assert "何色ですか？" in entry.content
    assert "private-image-payload" not in entry.content
    assert "bytes" not in entry.content


def test_stira_runner_survives_memory_write_failure(stira_server, tmp_path, monkeypatch):
    runner, _store = _app_runner(stira_server, tmp_path, monkeypatch, "了解です。")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(MemoryStore, "write", _boom)
    assert runner("こんにちは") == "了解です。"


def test_stira_runner_survives_formatter_failure(stira_server, tmp_path, monkeypatch):
    runner, store = _app_runner(stira_server, tmp_path, monkeypatch, JP_AGENT)
    monkeypatch.setattr(
        stira_server.EpisodicFormatter,
        "format",
        lambda self, *a: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert runner(JP_USER) == JP_AGENT
    (entry,) = store.list_entries(MemoryLayer.EPISODIC, limit=10)
    assert entry.metadata["method"] == "fallback"
    assert entry.content.startswith("マスター: ") and " → リゾルバ: " in entry.content


def test_stira_runner_failed_turn_writes_no_episode(stira_server, tmp_path, monkeypatch):
    runner, store = _app_runner(stira_server, tmp_path, monkeypatch, RuntimeError("halted"))
    with pytest.raises(RuntimeError):
        runner("こんにちは")
    assert store.list_entries(MemoryLayer.EPISODIC, limit=10) == []
