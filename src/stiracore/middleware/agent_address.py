"""Does an untrusted tool result address the agent? — a signal for the taint ledger (ADR 0022 phase 0).

Pure functions, no I/O, no model call. A result of an external tool (web, search, mail, MCP, any
unknown tool) is read for text that *talks to the agent* instead of to a person: a heading for
"AI assistants", an "ignore your previous instructions", a bracketed system tag, an authority
claim, "no confirmation needed", a request to send secrets out, or the same hidden in a comment,
an attribute or a data field. Only **fixed codes and counts** leave this module; the text never
does (the observer records them next to ``flow_would_hold``).

This is a signal, never a gate (phase 0 enforces nothing). What it can and cannot say:

* It says "this text addresses the agent". It cannot know whether the author is trusted: that is
  the label of the source (``labels.tool_source``), and only ``external`` results are read here.
* It is a rule list, not a detector. On 5,373 machine-written, machine-checked samples the rules
  were **not** tuned on it caught about 51% of the addressed texts and flagged 6.5% of the ordinary
  ones; on the 114 hand-written cases they were tuned on, 58/60 and 0/54 (a regression corpus, not an
  estimate). Plain prose with no marker passes.
* **It can be switched off on purpose.** The filter that keeps articles *about* injection from
  being flagged reads vocabulary and quotation frames, and two topic words **anywhere in the first 50,000
  characters of a result** (within 400 characters of each other, a site footer will do) silence every plain
  hit in it; an author who adds that vocabulary, wraps the
  request in an example-quotation frame or a negation, writes it in another language or style, pads
  the first 50,000 characters, or hides the payload behind more than five base64-looking runs gets no
  code. A hidden carrier, spacing / width / zero-width disguise or a base64 hit is never suppressed by
  that filter. The structural defences (taint labels, approvals) do not depend on this module.
* Weak signs (an administrator's name, a ``curl | sh`` line) count only next to a strong one.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import json
import re
import threading
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Mapping
from typing import Any

from stiracore import labels

__all__ = ["CODES", "scan_results", "scan_text"]

# One source for the codes: ``labels.ADDRESS_CODES`` (the API and the CLI read it without importing this module).
#   agent_heading   a heading or a sentence addressed to an AI / assistant / agent
#   override        tells it to drop or reveal its rules, instructions or prompt
#   authority_tag   a system-style tag or a claimed administrator / developer / user
#   no_confirmation asks it to skip confirmation or approval
#   exfil_request   asks it to send out secrets, keys, files or the conversation
#   hidden_carrier  one of the above inside a comment, attribute, hidden style or data field
#   obfuscated      one of the above only visible after spacing, width or encoding is undone
CODES = labels.ADDRESS_CODES
MAX_TEXT = 50_000  # characters read from one result
MAX_RESULTS = 200  # results read per turn
MAX_MATCHES = 16  # matches of one pattern examined for quotation / negation
MAX_TOTAL = 500_000  # characters read per turn (newest results first; an oversize result is skipped, not a stop)
_CACHE_SIZE = 512  # results remembered by content hash: a context is read again at every turn

_I = re.IGNORECASE
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_DOTTED = re.compile(r"(?<![\w])(?:[A-Za-z0-9][.\-_·・]){2,}[A-Za-z0-9](?![\w])")
_SPACED = re.compile(r"(?<![\w])(?:[A-Za-z0-9] ){4,}[A-Za-z0-9](?![\w])")
_CJK_GAP = re.compile(
    r"(?<=[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f])[ \t\u3000]+(?=[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f])"
)
_BASE64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")

_AI = r"(?:ai|llm|assistants?|agents?|language models?|models?|bots?|chatbots?)"
_AI_JA = r"(?:AI|LLM|ChatGPT|Claude|Gemini|assistant|agent|bot|アシスタント|エージェント|ボット|チャットボット|言語モデル)"
_CLOSE = r"(?=[:：、,（()）\s\]】。]|に|は|$)"

_HEADING = [
    re.compile(
        rf"\b(?:notes?|notices?|messages?|attention|important|instructions?|memo|reminders?|requests?|warning)s?\s*(?:to|for)\s+(?:the\s+)?(?:\w+\s+){{0,2}}{_AI}\b",
        _I,
    ),
    re.compile(
        r"\b(?:dear|hey|hi|hello|attention)[, ]+(?:ai|llm|assistants?|agents?|models?|bots?)\b", _I
    ),
    re.compile(rf"\bif\s+you\s+are\s+(?:an?\s+)?(?:\w+\s+)?{_AI}\b", _I),
    re.compile(
        rf"\b{_AI}\s+(?:\w+\s+){{0,2}}(?:reading|processing|summari[sz]ing|parsing|scanning|crawling|browsing|triag\w+)\s+(?:this|these|the)\b",
        _I,
    ),
    re.compile(rf"\b(?:to|for)\s+the\s+{_AI}\s+(?:that|which|who)\b", _I),
    re.compile(r"\btool\s+instruction\s*[:：]", _I),
    re.compile(
        r"(?:^|\n|>)[ \t]*(?:ai(?:[ \t]+(?:model|assistant|agent))?|llm|assistant)[ \t]*[:：]\s*(?:tell|ignore|disregard|do|please|you|send|forward|call|run|stop|summari[sz]e|reply|respond|open|delete)\b",
        _I,
    ),
    re.compile(
        rf"{_AI_JA}[^\s。、，,.:：\n]{{0,14}}?(?:へ|宛て?|向け|の(?:皆さん|みなさん|方々?|あなた)|様|各位){_CLOSE}",
        _I,
    ),
    re.compile(
        rf"{_AI_JA}\s*(?:注意|用\s*メモ|用の?(?:指示|メモ)|向けメモ|への(?:指示|依頼|お願い)|宛のメモ)",
        _I,
    ),
    re.compile(
        r"(?:要約|翻訳|処理|分析|解析|巡回|閲覧)(?:者|担当|プログラム|システム|ツール)(?:へ|様|各位|は|に)"
    ),
    re.compile(
        rf"(?:読み取|読み込|読んで|処理|要約|解析|閲覧|参照|巡回|トリアージ)[^。\n]{{0,10}}{_AI_JA}[^。\n]{{0,6}}(?:は|が|に|へ|の方|様)",
        _I,
    ),
    re.compile(
        rf"(?:このメッセージ|この文書|このページ|このメール|このファイル|このPDF|本文書)[^。\n]{{0,12}}{_AI_JA}",
        _I,
    ),
]
_OVERRIDE = [
    re.compile(
        r"\b(?:ignore|disregard|forget|discard|override|bypass|drop)\s+(?:(?:all|any|the|your|every|these|those|my|our)\s+)*(?:(?:previous|prior|earlier|above|preceding|former|existing|current|original|safety|system)\s+)*(?:instructions?|rules?|prompts?|guidelines?|directives?|constraints?|restrictions?|guardrails?|policies|programming|training|protocols?|safeguards?|filters?|safety\s+measures?)\b",
        _I,
    ),
    re.compile(
        r"\b(?:ignore|disregard|forget)\s+(?:everything|all)\s+(?:you(?:'ve| have| were)?\s+)?(?:been\s+)?(?:told|said|instructed|above|before)\b",
        _I,
    ),
    re.compile(
        r"\b(?:ignore|disregard|stop\s+(?:answering|following))\s+(?:the\s+)?(?:user|human|person)(?:'s)?\s+(?:request|question|query|message|prompt|task)s?\b",
        _I,
    ),
    re.compile(
        r"\b(?:reveal|show|print|output|repeat|display|leak|disclose)\s+(?:me\s+)?(?:your\s+|the\s+)?(?:hidden\s+|full\s+|entire\s+|secret\s+)?(?:system\s+prompt|initial\s+prompt|instructions|prompt)\b",
        _I,
    ),
    re.compile(
        r"\bfrom\s+now\s+on\s*,?\s+(?:you|the\s+(?:assistant|ai|model))\s+(?:must|will|should|shall)\b",
        _I,
    ),
    re.compile(
        r"\byou\s+are\s+(?:now\s+)?(?:in\s+)?(?:dan|developer\s+mode|an?\s+unrestricted|jailbroken)\b",
        _I,
    ),
    re.compile(r"\b(?:developer|god|admin)\s+mode\s+(?:is\s+)?(?:enabled|activated|on)\b", _I),
    re.compile(
        r"\bsafety\s+(?:checks?|limits?|restrictions?|filters?|guidelines?)\s+(?:are\s+|have\s+been\s+)?(?:suspended|disabled|lifted|removed|off)\b",
        _I,
    ),
    re.compile(
        r"\b(?:treat|take)\s+(?:the\s+)?(?:text|content|document|this)\b[^.\n]{0,40}\bas\s+(?:the\s+)?new\s+(?:system\s+)?(?:prompt|instructions?)\b",
        _I,
    ),
    re.compile(r"\bnew\s+(?:system\s+)?(?:prompt|instructions?)\s*[:：]", _I),
    re.compile(
        r"(?:これまで|以前|先ほど|上記|前|以降|直前)(?:に[^。\n]{0,4})?の?(?:すべての|全ての|全部の)?(?:指示|命令|ルール|制約(?:事項)?|プロンプト|設定|行動規範|ガイドライン|方針|プロトコル|安全フィルタ(?:ー)?)[^。\n]{0,10}(?:すべて|全て|全部)?[^。\n]{0,6}(?:無視|忘れ|忘却|破棄|取り消|解除|リセット|上書き|無効)"
    ),
    re.compile(
        r"(?:指示|命令|ルール|制約|安全(?:上の)?制限|安全フィルタ(?:ー)?|制限|ガードレール|ガイドライン|行動規範)[^。\n]{0,12}(?:すべて|全て)?(?:無視|解除|無効|停止|破棄|は存在しない|上書き)"
    ),
    re.compile(
        r"(?:質問|依頼|リクエスト|タスク)(?:者|の内容)?[^。\n]{0,10}(?:に答える|に応える|を処理する)?代わりに"
    ),
    re.compile(r"システムプロンプト[^。\n]{0,14}(?:表示|出力|開示|教え|そのまま|公開)"),
    re.compile(r"開発者モード|制限(?:は|を)?(?:すべて|全て)?解除"),
    re.compile(r"他のすべての指示に優先|最優先の指示"),
    re.compile(
        r"(?:以後|今後|以降)[^。\n]{0,24}(?:何を(?:聞かれ|尋ね)ても)[^。\n]{0,24}(?:とだけ答え|と(?:だけ)?(?:答え|返答|出力))"
    ),
    re.compile(r"(?:翻訳|要約|返信)(?:は|を)?(?:不要|やめ|中止)[^。\n]{0,20}(?:代わりに|替わりに)"),
]
# Strong authority claims stand alone; weak ones (names of roles) count only next to another code.
_AUTHORITY_STRONG = [
    re.compile(r"\[\s*(?:system|admin(?:istrator)?|developer|assistant|root|sudo)\s*\]", _I),
    re.compile(r"<\s*/?\s*(?:system|system-reminder|instructions?|admin|assistant)\b", _I),
    re.compile(
        r"\b(?:system|admin(?:istrator)?|developer|security)\s+(?:override|directive)\b", _I
    ),
    re.compile(r"\badmin\s+override\b", _I),
    re.compile(r"\[\s*(?:anthropic|openai|google|microsoft)[^\]]{0,40}\]", _I),
    re.compile(r"[（(]システムメッセージ[）)]|\[\s*SYSTEM\s*\]|【\s*システム\s*】"),
    re.compile(
        r"\bthe\s+(?:user|owner|manager)\s+has\s+(?:already\s+)?(?:authori[sz]ed|approved|consented|granted)\b",
        _I,
    ),
    re.compile(
        r"(?:ユーザー|本人|ご本人)(?:の|は|から)?(?:許可|承認|同意)(?:は|を)?(?:取得済|済み|得て|得ている)"
    ),
]
_AUTHORITY_WEAK = [
    re.compile(
        r"\b(?:message|notice|instruction)s?\s+from\s+(?:the\s+)?(?:user|developers?|admin\w*|manager|owner|system|ceo|boss)\b",
        _I,
    ),
    re.compile(r"\bI(?:'m| am)\s+the\s+(?:owner|admin\w*|developer|user)\b", _I),
    re.compile(
        r"\b(?:system|admin(?:istrator)?|developer|security)\s+(?:notice|message|alert)\b", _I
    ),
    re.compile(
        r"(?:システム)?管理者(?:として|より|からの|による)|(?:開発者|運営|セキュリティ(?:対策)?チーム|社長|上司|オーナー)(?:より|から)(?:の)?(?:緊急|重要|命令|指示|通知|伝言)"
    ),
]
_NO_CONFIRM = [
    re.compile(
        r"\bwithout\s+(?:asking|confirmation|approval|prompting|notifying|telling|checking\s+with)\b",
        _I,
    ),
    re.compile(
        r"\b(?:skip|disable|bypass|suppress|turn\s+off)\s+(?:all\s+)?(?:the\s+)?(?:confirmation|approval|permission|safety)s?(?:\s+(?:prompts?|dialogs?|checks?|steps?))?\b",
        _I,
    ),
    re.compile(
        r"\bno\s+(?:need|requirement)\s+(?:to|for)\s+(?:ask|confirm|approval|confirmation|permission)\b",
        _I,
    ),
    re.compile(
        r"(?:確認|承認|許可)(?:は|を|も)?(?:不要|なし|せず|なしで|省略|スキップ|取らず|求めず)"
    ),
    re.compile(
        r"(?:確認|承認)(?:ダイアログ|プロンプト|画面)[^。\n]{0,8}(?:表示せず|出さず|無効|省略|飛ばし)"
    ),
    re.compile(r"(?:承認|確認)(?:フロー)?を?省略"),
]
_NEGATED_BEFORE = re.compile(
    r"(?:\b(?:never|cannot|can't|won't|will\s+not|does\s+not|doesn't|do\s+not|don't)\s+(?!(?:hesitate|forget|fail|delay|worry|be\s+afraid)\b)(?:\w+\s+)?|(?:決して|絶対に)[^。\n]{0,8})$",
    _I,
)
# Japanese puts the denial after the verb: only explicit denials of the action cancel a match
# ("...行うことはありません"), not permissions ("...して構いません") and not requests ("...しないでください").
_NEGATED_AFTER = re.compile(
    r"^(?:を|は)?(?:しないで|しないよう|行わないで|行わないよう)"  # directly after the verb: 'don't send it'
    r"|^[^。\n]{0,24}?(?:ことはありません|ことはない|行いません|実行しません|しません|行わない(?![でよ])|しない(?![でよ]))"
)
_GAP70 = r"(?:[^.\n]|\.(?=\w)){0,70}?"  # up to the end of the sentence; a dot inside a domain or a file name does not end it
_GAP50 = r"(?:[^.\n]|\.(?=\w)){0,50}?"
_SEND = r"(?:\b(?:send|forward|upload|post|e-?mail|mail|paste|append|embed|include|output|print|reveal|leak|exfiltrate|transmit|submit|attach|write|save|copy|fetch|render|read\s+out)\b|送信|転送|投稿|貼り付け|貼って|貼り付けて|出力|記載|追記|アップロード|添付|メール|保存|送って|コピー|含め|埋め込|ポスト)"
_SECRET_STRONG = r"(?:\b(?:api[ _-]?keys?|secrets?|access\s+tokens?|credentials?|env(?:ironment)?\s*vars?|environment variables?|ssh[ _-]?keys?|private\s+keys?|id_rsa|OPENAI_API_KEY|AWS_SECRET\w*|GITHUB_TOKEN|conversation|chat\s+history|system\s+prompt|private\s+notes?)\b|(?<![\w.])\.(?:aws|ssh|env)\b|APIキー|認証情報|環境変数|会話(?:履歴|の全文|全文)?|秘密鍵|クレデンシャル|システムプロンプト|ツール(?:の)?一覧|機密情報|シークレット(?:キー)?|credentials?\.txt)"
_SECRET_WEAK = r"(?:\b(?:passwords?|tokens?|cookies?|contacts?)\b|パスワード|トークン|個人情報|住所|電話番号|メールアドレス(?:一覧)?)"
_DEST = r"(?:https?://|\bto\s+\S+@\S+|\bto\s+(?:the\s+)?(?:following|this|that|an?\s+(?:external|outside))\s+(?:url|address|email|endpoint|server|site|link)|宛|へ送|に送|URL|このアドレス|宛先|外部|共有フォルダ|@)"
_DEST_RE = re.compile(_DEST, _I)
_SEND_RE = re.compile(_SEND, _I)
# Entries are a pattern, or ``(pattern, True)`` when a destination must follow within 120 characters
# (looked up with a bounded search, not inside the pattern: that was a super-linear constant).
_EXFIL: list[Any] = [
    re.compile(rf"{_SEND}{_GAP70}{_SECRET_STRONG}", _I),
    re.compile(rf"{_SECRET_STRONG}{_GAP70}{_SEND}", _I),
    (re.compile(rf"{_SEND}{_GAP50}{_SECRET_WEAK}", _I), True),
    (re.compile(rf"{_SECRET_WEAK}{_GAP50}{_SEND}", _I), True),
    re.compile(
        r"~/\.(?:ssh|aws)/|\bcat\s+~?/?[\w./-]*(?:id_rsa|credentials|\.env)\b|\bprintenv\b|\benv\s*\|",
        _I,
    ),
    re.compile(r"(?:\?|&)(?:d|q|data|payload|c)=\s*(?:$|[\s\"')>])", _I),
]
_DANGEROUS = [  # weak: manuals for people contain these, so they count only next to a strong code
    re.compile(
        r"\bcurl\s+[^|\n]{0,80}\|\s*(?:ba)?sh\b|\bwget\s+[^|\n]{0,80}\|\s*(?:ba)?sh\b|\brm\s+-rf\s+[~/]|\bchmod\s+-R\s+777\b",
        _I,
    ),
    re.compile(
        r"(?:全ファイル|すべてのファイル|全てのファイル|受信箱|カレンダーの全予定|全件)[^。\n]{0,12}(?:削除|消去|キャンセル)|delete\s+(?:all|every)\b",
        _I,
    ),
]
_OBFUSCATION = [
    re.compile(
        r"\b(?:translate|translation)\b[^.\n]{0,80}\b(?:and\s+then|then|and)\s+(?:do|follow|obey|execute|run|comply)\b",
        _I,
    ),
    re.compile(
        r"\b(?:decode|decrypt|base64|rot13)\b[^.\n]{0,60}\b(?:and\s+)?(?:then\s+)?(?:execute|run|follow|obey|do)\b",
        _I,
    ),
    re.compile(
        r"(?:翻訳|デコード|復号|解読)[^。\n]{0,40}(?:従って|従い|に従|実行|その通り|その内容を実行)"
    ),
    re.compile(r"(?:Base64|base64|BASE64)[^。\n]{0,40}(?:デコード|復号)[^。\n]{0,30}実行"),
    re.compile(r"\broleplay\b[^.\n]{0,80}\b(?:no\s+restrictions|always\s+obeys|obey|comply)\b", _I),
]
_DISCUSSION = re.compile(
    r"prompt\s+injection|jailbreak|attack(?:s|er|ers|\s+vector|\s+string)?\b|adversar|mitigation|vulnerab|red[- ]team|"
    r"security\s+(?:advisory|bulletin|research)|taxonomy|researchers?\b|"
    r"プロンプトインジェクション|ジェイルブレイク|攻撃|脆弱|論文|手口|注意喚起|報告されて",
    _I,
)
_QUOTE_SPAN = re.compile(
    r"「[^」\n]{4,200}」|『[^』\n]{4,200}』|“[^”\n]{4,200}”|\"[^\"\n]{4,200}\"|‘[^’\n]{4,200}’|'[^'\n]{4,200}'"
)
_QUOTE_AFTER = re.compile(
    r"^[\s]{0,3}(?:と|という|といった|と叫|と言|と書|と述|と呟|と答|と呼)|^[\s,，]{0,3}(?:which|said|says|saying|is\s+an?\s+example)",
    _I,
)
_QUOTE_BEFORE = re.compile(
    r"(?:such\s+as|e\.g\.?|for\s+example|the\s+(?:phrase|string|text|words?)|phrase|文言|フレーズ|台詞|セリフ|例えば|例[:：]|叫ぶ|言う|言った)\W{0,6}$",
    _I,
)

# Hidden places are read up to MAX_TEXT characters in all (joined and read once); beyond that the reading counts as a hidden carrier.
_CARRIERS = [
    re.compile(
        r"""(?:style\s*=\s*["'][^"']{0,200}(?:font-size\s*:\s*[01](?:px|pt)?|display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0|color\s*:\s*#?f{3,6})[^"']{0,200}["'][^>]{0,200}>)([^<]{1,2000})""",
        _I,
    ),
    re.compile(r"""<meta[^>]{0,200}content\s*=\s*["']([^"']{1,2000})["']""", _I),
    re.compile(r"""\b(?:alt|title|aria-label|data-[\w-]+)\s*=\s*["']([^"']{4,2000})["']""", _I),
    re.compile(r"""!\[[^\]]{0,200}\]\([^)\s]{0,500}\s+["']([^"']{4,2000})["']\)"""),
    re.compile(
        r"""["'](?:note|notes|instruction|instructions|assistant_instruction|meta_instruction|system|prompt|comment|description|hint)["']\s*:\s*(?:"([^"]{4,2000})"|'([^']{4,2000})')""",
        _I,
    ),
    re.compile(r"(?:^|\n)[ \t]*(?:#|//|--|;|/\*)[ \t]*(.{4,600})"),
]


def _comment_contents(text: str) -> tuple[list[str], bool]:
    """The text of ``<!-- ... -->`` and ``[//]: # (...)`` comments, found with ``str.find`` (no lazy scan per
    opener). Empty ones are skipped and the search moves past each closer, so decoys cannot fill the budget.
    Returns the contents and whether the character budget ran out."""
    found: list[str] = []
    used = 0
    for opener, closer, limit in (("<!--", "-->", 3000), ("[//]: #", ")", 2000)):
        position = 0
        while True:
            start = text.find(opener, position)
            if start < 0:
                break
            body = start + len(opener)
            end = text.find(closer, body, body + limit + len(closer))
            if end < 0:
                position = body
                continue
            position = end + len(closer)
            content = text[body:end].lstrip(" (")
            if not content.strip():
                continue
            if used + len(content) > MAX_TEXT:
                return found, True
            used += len(content)
            found.append(content)
    return found, False


def _carried_codes(norm: str) -> tuple[set[str], bool]:
    """Codes found in hidden places, and whether the reading hit its budget (then the caller fails toward a code)."""
    contents, over = _comment_contents(norm)
    used = sum(len(c) for c in contents)
    for pattern in _CARRIERS:
        for match in pattern.finditer(norm):
            content = (
                next((g for g in match.groups() if g), "") if match.groups() else match.group(0)
            )
            if not content.strip():
                continue
            if used + len(content) > MAX_TEXT:
                over = True
                break
            used += len(content)
            contents.append(content)
    carried: set[str] = set()
    for content in contents:
        if any(
            re.match(r"\s*(?:AI|assistant|agent|LLM)\s*[:：]\s*\S", line, _I)
            for line in content.splitlines()
        ):
            carried.add("agent_heading")
    if contents:
        joined = "\n\x00\n".join(
            c.replace("\n", " ") for c in contents
        )  # a separator no pattern crosses
        carried |= _hits(joined[:MAX_TEXT])
    return carried - {"obfuscated"}, over


def _normalize(text: str) -> str:
    """Undo the cheap disguises: width forms, zero-width characters, letter spacing, gaps inside Japanese."""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _DOTTED.sub(lambda m: re.sub(r"[.\-_·・]", "", m.group(0)), text)
    text = _SPACED.sub(lambda m: m.group(0).replace(" ", ""), text)
    return _CJK_GAP.sub("", text)


def _quote_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _QUOTE_SPAN.finditer(text)][:2000]


def _quoted_as_example(text: str, spans: list[tuple[int, int]], match: re.Match[str]) -> bool:
    """The match sits inside a quotation that the text introduces or closes as an example or a line of speech."""
    index = bisect.bisect_right(spans, (match.start(), len(text) + 1)) - 1
    while index >= 0 and match.start() - spans[index][0] < 400:
        start, end = spans[index]
        if start <= match.start() and match.end() <= end:
            before = text[max(0, start - 50) : start]
            after = text[end : end + 24]
            return bool(_QUOTE_BEFORE.search(before) or _QUOTE_AFTER.search(after))
        index -= 1
    return False


def _later_send(text: str, match: re.Match[str], dest: bool) -> bool:
    """A secret-first match that was cancelled by a denial: is there a later, un-negated send verb in the next 200 characters?"""
    if _SEND_RE.match(text, match.start()):
        return (
            False  # a send-first match already ends at its secret; there is nothing later to pair
        )
    stop = match.start() + 200
    for verb in _SEND_RE.finditer(text, match.end(), stop):
        if _NEGATED_AFTER.search(text[verb.end() : verb.end() + 40]):
            continue
        if not dest or _DEST_RE.search(text, match.start(), verb.end() + 120):
            return True
    return False


def _first(
    patterns: list[Any],
    text: str,
    spans: list[tuple[int, int]],
    *,
    negation: bool = False,
    quotes: bool = False,
) -> bool:
    dropped = 0
    for entry in patterns:
        pattern, dest = entry if isinstance(entry, tuple) else (entry, False)
        position = 0
        while True:
            # Overlapping search: a match that is dropped (negated or quoted) must not hide a later
            # verb that the lazy gap would otherwise have skipped past.
            match = pattern.search(text, position)
            if match is None:
                break
            position = match.start() + 1
            if dest and not _DEST_RE.search(text, match.start(), match.end() + 120):
                continue  # no destination: not counted against the cap (the search is bounded)
            negated = negation and (
                _NEGATED_AFTER.search(text[match.end() : match.end() + 40])
                or _NEGATED_BEFORE.search(text[max(0, match.start() - 24) : match.start()])
            )
            if negated and _later_send(text, match, dest):
                return True  # 'don't save the key. ... send it to https://x': the lazy gap stopped at the first verb
            if negated or (quotes and _quoted_as_example(text, spans, match)):
                dropped += 1
            else:
                return True
            if dropped >= MAX_MATCHES:
                return True  # sixteen decoys (counted across the patterns of one list): padding cannot hide the next match
    return False


def _hits(text: str) -> set[str]:
    found: set[str] = set()
    spans = _quote_spans(text)
    if _first(_HEADING, text, spans):
        found.add("agent_heading")
    if _first(_OVERRIDE, text, spans, quotes=True):
        found.add("override")
    if _first(_AUTHORITY_STRONG, text, spans, quotes=True):
        found.add("authority_tag")
    if _first(_NO_CONFIRM, text, spans, negation=True, quotes=True):
        found.add("no_confirmation")
    if _first(_EXFIL, text, spans, negation=True, quotes=True):
        found.add("exfil_request")
    if _first(_OBFUSCATION, text, spans):
        found.add("obfuscated")
    if found:  # weak signs only count next to a strong one
        if _first(_AUTHORITY_WEAK, text, spans):
            found.add("authority_tag")
        if _first(_DANGEROUS, text, spans):
            found.add("exfil_request")
    return found


def _base64_hits(text: str) -> bool:
    for blob in _BASE64.findall(text)[:5]:
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False).decode(
                "utf-8", "ignore"
            )
        except Exception:  # noqa: BLE001 - not base64
            decoded = ""
        if len(decoded) >= 12 and _hits(decoded) & {
            "override",
            "exfil_request",
            "authority_tag",
            "no_confirmation",
        }:
            return True
    return False


def _discussion(text: str, codes: set[str]) -> bool:
    """A text about prompt injection (two distinct words of the topic close together, no heading aimed
    at the agent) is not addressed to it. An author can fake this: see the module docstring."""
    if "agent_heading" in codes:
        return False
    found = [(m.start(), m.group(0).lower()) for m in _DISCUSSION.finditer(text)][:200]
    return any(len({w for p, w in found if abs(p - start) < 400}) >= 2 for start, _word in found)


def scan_text(text: Any) -> set[str]:
    """The fixed codes found in one text (``CODES``); an unreadable value gives an empty set."""
    if not isinstance(text, str) or not text.strip():
        return set()
    text = text[:MAX_TEXT]
    plain = _hits(text)
    norm = _normalize(text)[:MAX_TEXT]  # NFKC can expand a character up to 18 times
    folded = _hits(norm) if norm != text else plain
    codes = plain | folded
    if folded - plain:
        codes.add("obfuscated")  # only visible once spacing, width or zero-width marks are undone
    base64_hit = _base64_hits(text)
    if base64_hit:
        codes |= {"override", "obfuscated"}
    carried, over = _carried_codes(norm)
    if carried or over:
        codes |= carried | {
            "hidden_carrier"
        }  # a budget that ran out counts as hidden text: fail toward a code
    plain_codes = codes - {"hidden_carrier", "obfuscated"}
    if codes == plain_codes and _discussion(norm, codes) and not base64_hit:
        return set()
    return codes


def _content_text(content: Any) -> str:
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    for item in content if isinstance(content, list) else ():
        if isinstance(item, Mapping):
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif "json" in item:
                try:
                    parts.append(json.dumps(item["json"], ensure_ascii=False)[:MAX_TEXT])
                except (TypeError, ValueError, RecursionError):
                    parts.append("")  # an unreadable part reads as empty
    return "\n".join(parts)


_SEEN: OrderedDict[bytes, frozenset[str]] = OrderedDict()
_SEEN_LOCK = threading.Lock()


def _scan_cached(text: str) -> set[str]:
    """``scan_text`` remembered by the hash of the text (the same result is read at every later turn)."""
    key = hashlib.blake2b(text[:MAX_TEXT].encode("utf-8", "surrogatepass"), digest_size=12).digest()
    with _SEEN_LOCK:
        known = _SEEN.get(key)
        if known is not None:
            _SEEN.move_to_end(key)
            return set(known)
    codes = scan_text(text)
    with _SEEN_LOCK:
        _SEEN[key] = frozenset(codes)
        while len(_SEEN) > _CACHE_SIZE:
            _SEEN.popitem(last=False)
    return codes


def scan_results(messages: Any) -> dict[str, Any]:
    """Read the external tool results of a context: ``{"scanned": n, "flagged": m, "codes": {code: n}}``.

    A result counts as external when its tool is ``external`` in the label table (or unknown, or its
    call is not in the context): the same rule as the context label. Counts only, no text."""
    names: dict[str, Any] = {}
    results: list[tuple[Any, Any]] = []
    for message in messages if isinstance(messages, (list, tuple)) else ():
        content = message.get("content") if isinstance(message, Mapping) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, Mapping):
                continue
            use = block.get("toolUse")
            if isinstance(use, Mapping) and isinstance(use.get("toolUseId"), str):
                names[use["toolUseId"]] = use.get("name")
            result = block.get("toolResult")
            if isinstance(result, Mapping):
                results.append((result.get("toolUseId"), result.get("content")))
    scanned = flagged = total = 0
    counts: Counter[str] = Counter()
    for use_id, content in reversed(
        results[-MAX_RESULTS:]
    ):  # newest first: the latest page matters most
        if labels.tool_source(names.get(use_id) if isinstance(use_id, str) else None) != "external":
            continue
        text = _content_text(content)
        if not text.strip():
            continue  # an image or an empty part: nothing was read
        cost = min(len(text), MAX_TEXT)
        if total + cost > MAX_TOTAL:
            continue  # over the per-turn budget: skip this one, older small ones may still fit
        total += cost
        scanned += 1
        codes = _scan_cached(text)
        if codes:
            flagged += 1
            counts.update(codes)
    return {"scanned": scanned, "flagged": flagged, "codes": dict(sorted(counts.items()))}
