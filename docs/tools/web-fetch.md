# web_fetch failure codes / web_fetch の失敗コード (#584)

`web_fetch(url)` never follows a redirect and never echoes an exception's text (it can
carry a URL with a query or credentials). A failure is one of the fixed codes below
(`stiracore.tools.web_tools.FETCH_CODES`). The tool result has `status: "error"`,
`code`, a one-line text `Web fetch failed [<code>]: <fixed sentence>` and, where listed,
a detail field. A success result is unchanged (`status`, `content`, `url`, `truncated`; a PDF adds `pages`).

`web_fetch(url)` はリダイレクトを追跡せず、例外の本文（クエリや資格情報入りの URL を含みうる）
も返しません。失敗は下の固定コードのどれか 1 つです。ツール結果は `status: "error"`、`code`、
1 行の文 `Web fetch failed [<code>]: <固定文>`、表にある詳細フィールドだけを持ちます。
成功時の形は変わりません（`status`・`content`・`url`・`truncated`。PDF は `pages` を足す）。

| Code / コード | When / いつ | Detail / 詳細 |
|---|---|---|
| `invalid_url` | Not an absolute http(s) URL with a host, or it has `user:pass@` / 絶対 http(s) URL でない、ホストが無い、資格情報つき | — |
| `redirect` | 3xx; **not followed** / 3xx。**追跡しない** | `http_status`, `location` (when it can be shown safely / 安全に出せるときだけ) |
| `http_error` | 4xx / 5xx | `http_status` |
| `network_timeout` | Connect or read timeout (5 s / 20 s), also mid-body (urllib3 `ReadTimeoutError` wrapped in a `ConnectionError`), or the whole body not read within 60 s (a slow drip; nothing of it is returned, #601) / 接続・読み取りのタイムアウト（本文の途中を含む）、または本文全体を60秒以内に読み終えない（少しずつ送る相手。途中までの本文は返さない） | — |
| `network_connect` | DNS, refused connection, TLS / 名前解決・接続拒否・TLS | — |
| `network_error` | Another transport error while reading, or urllib3 older than 2.3 (the body deadline cannot stop the read; refused before reading) / 読み取り中のその他の通信エラー、または urllib3 が 2.3 未満（本文の期限で読み取りを止められないため、読む前に拒否） | — |
| `too_large` | Body over 2 MiB (a PDF: 10 MiB), a PDF over 100 pages, or a PDF that needs more than 1 GiB to read / 本文が 2 MiB（PDF は 10 MiB）超、PDF が 100 ページ超、読むのに 1 GiB を超えるメモリが要る PDF | `limit: "pages"` / `"memory"` |
| `unsupported_type` | Not a text Content-Type and not a PDF (e.g. `image/png`) / テキストでも PDF でもない | `content_type` (`type/subtype`, or `unknown`) |
| `decode_error` | Unknown charset / 不明な文字コード | — |
| `pdf_unavailable` | The optional extra is not installed (`pip install 'stiracore[pdf]'`) / 任意の extra が未導入 | — |
| `pdf_broken` | A PDF Content-Type without the PDF header, or a PDF the parser cannot read / PDF の見出しが無い・読めない | — |
| `pdf_encrypted` | Password-protected / パスワード付き | — |
| `pdf_no_text` | No extractable text (scanned, image-only; no OCR) / テキストが取れない（画像だけ。OCR はしない） | — |
| `pdf_timeout` | The extraction ran over its time (20 s) or CPU limit / 抽出が時間・CPU 上限を超えた | — |
| `pdf_limits_unavailable` | No memory limit can be guaranteed on this system (no enforced `RLIMIT_AS` and no `ps` for the watchdog, e.g. Windows): not parsed / メモリ上限を保証できない環境（解析しない） | — |
| `summary_unavailable` | `summarize=True` where no summarizer is set up / 要約の仕組みが無い環境 | — |
| `summary_limit` | More than 3 summaries in one turn (refused before fetching) / 1 ターン 3 回を超えた（取得前に拒否） | — |
| `summary_failed` | The summary call (including getting or building its model) failed; nothing else is tried / 要約の呼び出し（モデルの取得・生成を含む）が失敗（他は試さない） | `reason` (a code) |

## PDF / PDF (#585)

A response is a PDF when its Content-Type is `application/pdf` (or a generic
`application/octet-stream`) **and** it starts with the PDF header `%PDF-` (never by the
extension). The text is extracted by `pypdf` (the optional extra `stiracore[pdf]`) in a
**separate process** (`python -I`, empty environment, 20 s wall-clock, a CPU-time limit and a
1 GiB memory limit), at most 100 pages and 10 MiB; the child's own messages are never read
back. The memory limit is the child's `RLIMIT_AS` on Linux and, wherever `ps` exists (the only
bound on macOS, which does not enforce `RLIMIT_AS`), a watchdog in the server that reads the
child's resident size every 50 ms and kills it above the limit. The watchdog runs one `ps`
(an absolute path in `/bin` or `/usr/bin`), must read the child's size once **before** the
PDF is handed over, and stops the child after 3 failed readings in a row. Where no limit can
be guaranteed (or the readings fail), the PDF is not parsed (`pdf_limits_unavailable`). **This bounds resources; it is
not an OS sandbox**: the child runs as the server's user and could read what that user can
read. Every page is read so that the full text length is known (`source_chars` of a
summary); the text kept is at most 400,000 characters. The result is the usual success shape
plus `pages`.

Content-Type が `application/pdf`（または汎用の `application/octet-stream`）**かつ**先頭が
`%PDF-` のとき PDF として扱います（拡張子は見ない）。テキストは任意の extra `stiracore[pdf]`
の `pypdf` で、**別プロセス**（`python -I`・空の環境変数・20 秒・CPU 時間の上限、Linux ではメモリ上限）
で取り出し、100 ページ・10 MiB・メモリ 1 GiB まで。子プロセスのメッセージは読み戻しません。メモリ上限は
Linux では子の `RLIMIT_AS`、`ps` がある環境では（`RLIMIT_AS` を効かせない macOS ではこれだけが上限）
サーバー側の監視が 50 ms ごとに子の常駐サイズを読み、超えたら止めます。監視は `/bin` か `/usr/bin` の
1 つの `ps`（絶対パス）を使い、PDF を渡す**前に**子のサイズを一度読めることを確かめ、3 回続けて読めなければ
子を止めます。上限を保証できない（または読めない）ときは解析しません（`pdf_limits_unavailable`）。**これは資源の上限であって OS のサンドボックスではありません**:
子はサーバーと同じ OS ユーザーで動き、そのユーザーが読めるものは読めます。全文の長さ（要約の
`source_chars`）を知るため全ページを読み、保持する本文は 400,000 文字までです。結果は通常の成功の形に
`pages` を足したものです。

## Summary on request / 要約の指定 (#585)

`web_fetch(url, summarize=False, focus=None)`. The default is the text, unchanged.
`summarize=True` sends the text (at most 50,000 characters) to one call of the task
`tool_summary` ([task-models.md](../task-models.md); the chat model unless routed) **without
tools**: the page and `focus` are framed as data in the request, and a tool call the summary
model writes is dropped, never run. The result is Stira's JSON first (`source_url` without
credentials or secret-looking query values — a URL over 300 characters is cut, ends with `…`
and has `source_url_shortened: true`, it is never dropped; `fetched_at` UTC; `source_chars`,
the full length; `truncated`; `pages` for a PDF), then a fixed note and the summary inside
`<summary-of-untrusted-content>` (tag edges defanged, secrets masked). **A summary is
untrusted data and no evidence that the page is safe.** At most 3 per turn, counted per turn
id (turns that interleave keep their own count; a finished turn — by the server's turn store
— is refused; a full table refuses new turns instead of forgetting a count; a call outside a
turn is `summary_unavailable`); each
call writes
one `audit.task_model.call` row and one `task_model.call` turn event (task, model, input /
output tokens, status — never text). The web_fetch Ask rule is unchanged: the summary is
an argument of the same call.

既定は本文のまま（変更なし）。`summarize=True` は本文（50,000 文字まで）を処理 `tool_summary`
（[task-models_ja.md](../task-models_ja.md)。指定が無ければ会話のモデル）の 1 回の呼び出しに
**ツールなしで**渡します。ページと `focus` は要求の中でデータとして枠に入れ、要約モデルが書いた
ツール呼び出しは捨てます（実行しない）。結果は先に Stira の JSON（資格情報や秘密らしいクエリ値を
除いた `source_url`。300 文字を超える URL は切り詰めて末尾に `…` を付け `source_url_shortened: true`
とし、落とさない。UTC の `fetched_at`、全文の長さ `source_chars`、`truncated`、PDF なら `pages`）、続けて
固定の注意書きと `<summary-of-untrusted-content>` の中の要約（タグの縁は無害化、秘密はマスク）。
**要約は未信頼のデータであり、ページが安全だという根拠にはなりません。** 1 ターン 3 回まで
（ターン ID ごとに数える。交互に進むターンもそれぞれの回数を保ち、終わったターン（サーバーのターン記録で判定）
からの呼び出しは拒否。表が満杯なら回数を忘れず新しいターンを拒否。ターンの外の呼び出しは `summary_unavailable`）。
呼び出しごとに `audit.task_model.call` 1 行と `task_model.call` ターンイベント（処理名・モデル・
入出力トークン・状態。本文は無し）を残します。web_fetch の Ask の規則は変わりません（要約は同じ
呼び出しの引数）。

**The redirect destination (`location`) / 移動先.** The `Location` header resolved against
the requested URL; shown only when it is http(s) with a host and at most 500 characters.
Credentials (`user:pass@`) and the fragment are dropped; a query value whose name or value
looks like a secret (`token`, `api_key`, `sig`, `X-Amz-*`, high-entropy strings, …) becomes
`***`, and a query *name* that looks like a secret (`?AKIA…`) hides its whole pair; the
host is IDNA (punycode) and checked again after the conversion (an IP literal or DNS labels
of letters, digits and hyphens — IDNA turns U+3000 / U+00A0 into an ASCII space, which is
refused; an IPv6 scope id such as `fe80::1%eth0` is refused too); the path and query are percent-encoded and the URL goes through the shared secret
masking (a key or a Slack hook in the path becomes `***`), so no spaces, control
characters or raw non-ASCII text reach the model (the finished URL is checked once more:
printable ASCII, no space). The redirect's body is not read: requests' own redirect
preparation (`Response.next`, which drains the body even without following) is switched
off, so a 3xx whose body never ends is still `redirect`, not a timeout. Otherwise `location` is
absent and the text says the destination cannot be shown. The model fetches the
destination only with a new `web_fetch` call (its own approval, as any call).

`Location` を要求 URL 基準で解決し、ホストつきの http(s) で 500 文字以内のときだけ出します。
資格情報とフラグメントは捨て、名前か値が秘密らしいクエリの値は `***` に、名前そのものが秘密らしい
クエリ（`?AKIA…`）は組ごと `***` にします。ホストは IDNA（punycode）に変換した**後**に再検証し
（IP リテラルか、英数字とハイフンの DNS ラベルだけ。IDNA が U+3000 / U+00A0 を ASCII 空白に
変えるため。`fe80::1%eth0` のような IPv6 のスコープ ID も拒否）、パスとクエリはパーセントエンコードし、最後に共通の秘密マスク（パス中のキーや
Slack の Webhook も `***`）を通すため、空白・制御文字・生の非 ASCII
文字列はモデルに届きません（完成した URL も、表示可能な ASCII で空白なしかを最後に確認）。
リダイレクトの本文は読みません（requests が `Response.next` の準備で本文を読み切る処理を無効化。
本文が止まる 3xx も `redirect` になる）。出せないときは `location` を付けず、出せない旨の文だけを返します。
移動先を読むには、モデルが改めて `web_fetch` を呼びます（通常の呼び出しと同じ承認）。

**Audit and observation / 監査と観測.** Each failure writes one `audit.web.failed` row
(`AuditChain.record_web_failure`): `tool_name`, `reason_code` (a code above, else
`unknown`) and `http_status` — never the URL, a host, the redirect target or exception
text. The `tool.completed` event and the chat's `tool_call.updated` carry `result_code`
(the tool card's "Exit status" line shows it). #585 reuses this table.

失敗ごとに `audit.web.failed` を 1 行書きます（ツール名・コード・HTTP ステータスのみ。URL・
ホスト・移動先・例外の本文は書かない）。`tool.completed` とチャットの `tool_call.updated` は
`result_code` を持ち、ツールカードの「終了状態」欄に出ます。#585 はこの表を使います。
