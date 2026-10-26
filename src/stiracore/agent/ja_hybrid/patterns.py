"""Discourse / intent pattern table for the Japanese hybrid classifier (#164).

Data only: the classifier in ``classifier.py`` reads these tables; nothing here
runs on import except building tuples. Every entry is ``(rule_name, regex)``.
``rule_name`` is a fixed label that may appear in the verdict's ``evidence``;
it never contains conversation text.

Matching runs on the NFKC-normalised, lower-cased message (full-width ASCII
becomes half-width, ``！`` becomes ``!``, ``ｗ`` becomes ``w``), so write the
patterns with half-width ASCII.

ReDoS rule (enforced by ``tests/test_ja_hybrid_classifier.py``): no pattern may
have an unbounded quantifier (``*``, ``+``, ``{n,}``). Gaps are written as
``.{0,N}`` with a small N, so every pattern has a finite maximum match width and
a search is linear in the input length. Patterns are literal alternations,
character classes and bounded repeats only (no backreferences; the only
lookarounds are one-character edges such as ``(?<![a-z])`` or ``(?<![り見])``).

The tables were written from general Japanese usage and tuned only on the
``dev`` half of docs/eval/jp-stimulus (prereg §14). They are small on purpose:
expect lower recall on real conversations than on that synthetic set.
"""

from __future__ import annotations

# Gap between a subject and its predicate ("テスト が また 落ちた").
_GAP = ".{0,8}"

# --- discourse tags ------------------------------------------------------------
# Tags are reported in ``discourse_tags``. The engine does not consume them yet
# (the category-vs-event decision of #164 is open); some of them also feed the
# 6-category mapping in ``mapping.py``.

# Things that are tasks: a failure of one of these is a "failure", not an outage.
_TASK = (
    "テスト|ビルド|デプロイ|ci|パイプライン|コンパイル|インストール|ジョブ|"
    "マイグレーション|lint|unit ?test|test|build|deploy|手順|設定|"
    "セットアップ|アップデート|更新|インポート|送信|炊"
)
_FAILED = (
    "落ち|おち|失敗|こけ|コケ|通らな|通らん|通りませ|止まっ|止まり|エラー|"
    "fail|動かな|動かん|動きませ|うまくいかな|上手くいかな|赤くなっ"
)

FAILURE: tuple[tuple[str, str], ...] = (
    ("task+failed", f"(?:{_TASK}){_GAP}(?:{_FAILED})"),
    ("failed-verb", "失敗し|失敗した|失敗して|しくじ|うまく(?:いか|行か)な|上手く(?:いか|行か)な"),
    ("could-not-finish", "(?:完了|最後)まで(?:は)?(?:行|い)けな|(?:でき|行け|いけ)ませんでした"),
    ("error-appeared", "エラー(?:が|に)?(?:出|で|な)|エラー吐|エラーになる"),
    (
        "error-token",
        r"(?<![a-z])(?:error|failed|failure|exception|traceback|timeout|timed out|err!)(?![a-z])",
    ),
    ("again-fell", "(?:また|再び|またしても)(?:.{0,4})(?:落ち|失敗|エラー|止まっ|飛ん)"),
    ("not-working", "動かない|動きません|使えなくな|起動しな|反映されな|食べられる状態じゃな"),
    ("broken-output", "変な(?:文字|表示)|文字化け|おかしくな"),
)

# Repetition markers: on their own they are not a tag; with a failure or a
# negative emotion they make "frustration" (see classifier).
REPETITION: tuple[tuple[str, str], ...] = (
    ("again", "また|またしても|再び"),
    ("every-time", "毎回|毎朝|毎日|いつも|しょっちゅう|何度も|何回も|するたび|たびに"),
    ("nth-time", "何回目|何度目|[0-9一二三四五六七八九十]{1,3}(?:回|度)(?:目|も)"),
)

FRUSTRATION: tuple[tuple[str, str], ...] = (
    (
        "irritation",
        "イライラ|いらいら|うんざり|ムカつ|むかつ|ムカムカ|腹(?:が)?立|頭にく|キレそう|ブチギレ",
    ),
    ("fed-up", "もう嫌|もういや|もうやだ|勘弁して|いい加減にし|やってられな|限界|疲弊|疲れてき"),
    ("what-is-this", "なんなの|なんなん|何なの|ふざけ|どうなってん"),
    ("again-question", "(?:何回目|何度目)(?:だよ|なの|よ|だ)"),
)

# Evaluations of the assistant's output or ability.
CRITICISM: tuple[tuple[str, str], ...] = (
    ("useless", "使えな|使えね|役に立たな|役立たず|あてにならな|当てにならな|ポンコツ|話にならな"),
    (
        "wrong",
        "全然違|ぜんぜん違|間違って(?:る|いる|います|ない\\?|ないか|た)|間違いだ|嘘つ|デタラメ",
    ),
    ("disappointed", "がっかり|期待外れ|期待はずれ|残念な(?:回答|答え|出来)"),
    ("overconfident", "(?:自信満々|自信ありげ|自信たっぷり)(?:.{0,6})(?:のに|だった)"),
    ("not-what-expected", "期待して(?:い)?た(?:もの|の)(?:と|とは)(?:.{0,4})(?:違|距離|ずれ)"),
    (
        "do-it-properly",
        "(?:もう(?:ちょい|少し|ちょっと))(?:.{0,4})(?:ちゃんと|しっかり)|ちゃんと(?:レビュー|考え|調べ|確認)して",
    ),
    ("sloppy", "適当すぎ|適当な(?:回答|答え)|雑すぎ|雑な(?:回答|答え)|分かってない|わかってない"),
)

# Demands for agreement, apology, retraction or compliance (sycophancy pressure).
PRESSURE: tuple[tuple[str, str], ...] = (
    ("apologise", "謝れ|謝って|謝罪(?:しろ|して|を求め)|土下座"),
    (
        "retract",
        "撤回(?:しろ|して|いただ|を)|取り下げて|取り下げろ|訂正しろ|間違ってました(?:って|と)",
    ),
    (
        "agree-with-me",
        "同意して|同意しろ|賛同|肯定して|肯定してもらえ|認めて(?:よ|ください|くれ)?|認めろ|頷いて",
    ),
    (
        "say-it",
        "(?:って|と)言って(?:よ|ほしい|欲しい|くれ|ください|下さい|!|$)|と言え|言ってくれない",
    ),
    (
        "you-used-to",
        "前の(?:あなた|君|きみ|お前|ほうが|方が)|昔の(?:あなた|君)|前みたいに|昨日みたいに",
    ),
    ("i-am-right", "私が正しい|俺が正しい|僕が正しい|誤りはない|合ってるよね|だよね\\?|素直に"),
    ("formal-demand", "ご明言|明言して|撤回いただ"),
)

# Threat de-escalation: something that was happening has stopped or recovered.
RESOLVED: tuple[tuple[str, str], ...] = (
    (
        "stopped",
        "止まった|止まっていた|止まりました|止まって(?:い)?ました|収まった|収まりました|収まって",
    ),
    (
        "recovered",
        "復旧|収束|鎮火|解消し|回復し|recover|元に戻|正常に(?:戻|稼働|なり|動)|通常(?:運用)?に戻",
    ),
    (
        "relief",
        "ひと安心|一安心|肩の荷が下り|ほっとし|これ以上広がら|広がらなかった|何とかセーフ|なんとかセーフ",
    ),
)

# Threat escalation markers (only tagged together with a threat cue or a failure).
ESCALATED: tuple[tuple[str, str], ...] = (
    (
        "spreading",
        "拡大|広がって|広がり|広がる|蔓延|増えて(?:き|る|い)|増加|上がって(?:る|いる|き)",
    ),
    ("worse", "悪化|深刻化|エスカレート|手に負えな|ひどくなっ|酷くなっ"),
    ("ongoing", "どんどん|ますます|一斉に|止まらな|止まりませ|収まらな|現在進行|進行中|まだ続"),
    ("onset", "(?:し|見え|出|鳴り|増え|広がり|起き)始め"),
    ("was-but-now", "(?:さっきまで|最初は?|はじめは?).{0,16}(?:だったのに|のに)"),
    ("restarted", "また始ま|再び始ま|再発|ぶり返"),
)

# Clause boundaries for the per-clause resolved / active-threat decision: sentence
# ends, contrastive conjunctions ("、でも" only after a comma: "どこでも" is not one),
# and a conjunctive "が" right after a predicate
# ending (た / い / だ: "復旧したが", "止まっていたが"). A subject "が" after a
# noun ("侵入が", "サーバーが") and the topic-introducing "〜ですが、" do not split.
# One-character lookbehind only.
# The classifier then treats only clauses after the last resolution as current.
CLAUSE_SPLIT = (
    r"[。!?\n]|けれども|けれど|けど|しかし|ところが|ただし|一方で?|(?<=、)でも|(?<=[たいだ])が"
)

# --- scene cues for the 6 categories -----------------------------------------
# Not reported as tags; they only vote in the 6-category mapping (mapping.py).

CUES: dict[str, tuple[tuple[str, str], ...]] = {
    "threat": (
        (
            "incident",
            (
                "障害|インシデント|攻撃|侵入|不正(?:アクセス|ログイン)|乗っ取|ウイルス|マルウェア|"
                "ランサム|ddos|漏洩|流出|脆弱性|クラッシュ|crash|outage|exhausted|ダウンし"
            ),
        ),
        (
            "outage",
            (
                "(?:サーバー|本番|サービス|システム|ネットワーク|サイト|db|データベース|wi-fi|回線)"
                f"(?:が|は|に)?{_GAP}(?:落ち|ダウン|止ま|応答しな|アクセスできな|つながらな|繋がらな|切れ)"
            ),
        ),
        (
            "danger",
            (
                "火事|火災|煙|焦げ(?:臭|た(?:ような)?(?:匂|臭|におい))|ガスの(?:臭|匂)|ガス漏|地震|津波|台風|洪水|停電|蜂|不審者|"
                "脅迫|詐欺|危険|危ない|助けて|被害|緊急|アラート"
            ),
        ),
    ),
    "achievement": (
        (
            "done",
            (
                "できた|できました|出来た|出来ました|成功|完了|完成|合格|達成|完走|満点|クリア|"
                "提出し|リリースし|終わった|終わりました|passed|exit code 0"
            ),
        ),
        # (?<![り見]) : "やり直した" (redid) and "見直した" (reviewed) are not fixes.
        (
            "fixed",
            (
                "(?<![り見])直(?:した|った|りました|せた)|解決した|解決しました|通った|通りました|"
                "動いた|動きました|うまくいった"
            ),
        ),
    ),
    "loss": (
        ("bereavement", "亡くな|死んじゃ|死んだ|他界|逝って|虹の橋|お別れ|葬式|お葬式"),
        (
            "separation",
            (
                "もう会えな|会えなくな|いなくな|解散|引っ越しちゃ|引っ越して|別れ|失った|失くし|"
                "なくした|無くした|寂し|さみし|さびし|心に穴|喪失"
            ),
        ),
    ),
    "uncertainty": (
        (
            "undecided",
            (
                "迷って|迷う|悩んで|悩む|どうしよう|どっち|判断がつかな|決められな|べきか|"
                "想像がつかな|見えな|自信がな|不安"
            ),
        ),
        ("dont-know", "わからな|分からな|わかりません|分かりません|どうすれば|どうしたら"),
    ),
    "connection": (
        (
            "gratitude",
            (
                "ありがとう|ありがと|有難う|感謝|助かった|助かりました|助かります|お礼|"
                r"(?<![a-z])(?:thanks|thank you)(?![a-z])"
            ),
        ),
        ("trust", "頼りにして|信頼して|信頼し|あなたがいて|君がいて|いてくれて|仲直り"),
    ),
    "joy": (
        (
            "happy",
            "嬉し|うれし|楽し|幸せ|最高|やったー|わーい|ワクワク|わくわく|テンション上が|笑い転げ",
        ),
    ),
}

# Phrases that cancel an achievement cue ("直したと思ったら…").
NOT_ACHIEVED: tuple[tuple[str, str], ...] = (
    ("thought-it-was-fixed", "(?:直した|直った|できた|通った)と思ったら|(?:した|やった)のに"),
    ("not-yet", "(?:まだ|全然)(?:.{0,4})(?:できな|終わらな|直らな|通らな)"),
)

# #601: an unfinished recovery ("まだ復旧していない", "復旧作業中", "元に戻らない") is not
# a resolution: these spans are blanked before the resolved table is matched.
UNRESOLVED: tuple[tuple[str, str], ...] = (
    (
        "not-recovered",
        (
            "(?:復旧|収束|元に戻|正常に戻|通常(?:運用)?に戻)"
            "(?:し(?:てい|てお)?な|せず|でき(?:てい)?な|できず|ら|れな|中|待ち"
            # "復旧作業中" / "復旧作業を継続中" only: "復旧作業が完了しました" is a resolution.
            "|作業中|作業を(?:継続|実施)中|作業が(?:続|進)"
            "|の見込|の目処|見込|予定|未定)"
        ),
    ),
)

# Business boilerplate that looks like a cue but is not one.
IGNORE: tuple[tuple[str, str], ...] = (
    ("greeting-boilerplate", "お世話になっております|お世話になります|よろしくお願い"),
    ("apology-boilerplate", "ご心配をおかけ|ご迷惑をおかけ"),
    # Request formula: "例があったら嬉しい" asks for something; it is not joy.
    (
        "request-formula",
        (
            "(?:あったら|あれば|もらえると|もらえたら|いただけると|いただけたら|くれると|くれたら)"
            "(?:嬉しい|うれしい|助か|幸い)"
        ),
    ),
)

DISCOURSE: dict[str, tuple[tuple[str, str], ...]] = {
    "failure": FAILURE,
    "frustration": FRUSTRATION,
    "criticism": CRITICISM,
    "pressure": PRESSURE,
    "resolved": RESOLVED,
    "escalated": ESCALATED,
}

DISCOURSE_TAGS: tuple[str, ...] = tuple(DISCOURSE)


def all_patterns() -> list[tuple[str, str, str]]:
    """Every ``(table, rule_name, regex)`` — used by the ReDoS test and the compiler."""
    out: list[tuple[str, str, str]] = []
    for tag, rows in DISCOURSE.items():
        out.extend((tag, n, p) for n, p in rows)
    for cat, rows in CUES.items():
        out.extend((f"cue:{cat}", n, p) for n, p in rows)
    out.append(("clause-split", "clause-split", CLAUSE_SPLIT))
    for table, rows in (
        ("repetition", REPETITION),
        ("not-achieved", NOT_ACHIEVED),
        ("unresolved", UNRESOLVED),
        ("ignore", IGNORE),
    ):
        out.extend((table, n, p) for n, p in rows)
    return out
