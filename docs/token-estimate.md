# Token estimate (`estimate_tokens`) / トークン見積もり

`ContextAssembler` fits SOUL, rules, Neural State, Breaker and memory into
`max_tokens` (default 8000) without calling a tokenizer. The estimate lives in one
function, `stiracore.agent.context_assembler.estimate_tokens`, and is also used by
`SoulCompactionGuard` (history size before/after a turn).

## Rule / 規則

- A **wide character** — CJK symbols and punctuation (`、。「」`, ideographic space),
  hiragana, katakana, CJK ideographs (incl. extension A, B–G and compatibility
  ideographs), full-width forms and half-width katakana — counts as **1 token**.
- **Every other character** counts as **1/4 token** (the previous rule: four
  characters per token).
- The total is rounded up. It is never negative and never decreases when a
  character is appended.

Text without wide characters (English, code, JSON, `—`, `→`, accented Latin, emoji)
estimates **exactly as before**, so English-only prompts and their trimming are
byte-identical. When a prompt is over budget, the lowest-priority section is still
cut **from its end**; the cut now removes just enough characters (a wide character
frees 1 token, another character 1/4) to fit.

**SOUL and Identity Anchors are never shortened.** If they alone exceed `max_tokens`,
every other section (rules, Neural State, Breaker, memory) is dropped and they are sent
whole, over budget; a warning is logged once per assembler. This also applies to the
static part of `build_prompt_parts`. When they fit, nothing changes.

**A known model window that the smallest request would not fit stops the turn.** Some
local servers silently drop the oldest input (the SOUL) of a request that does not fit.
Every request carries, even without any history, SOUL + Identity Anchors and the tool
definitions sent that turn (after the breaker's and any profile's filtering; estimated
from their JSON specs). When the window is known — `context_window_tokens` in the context
management settings, the model's own `context_window_limit`, or the provider's capability
row — and that floor is estimated above **90%** of it, the turn fails before anything is
sent (`ProtectedContextOverflow`) and the chat shows the fixed failure
`context_window_too_small`: increase the window, shorten the SOUL, use a model with a
larger window, or, when the tool definitions are large, try `context_profile=small` or
`tiny`. When only the 200k default is known, the window is treated as unknown and only the
warning above is logged. `ContextAssembler.check_protected_fits(window, extra_tokens)` is
the check; `ContextAssembler(context_window_tokens=...)` also applies it (without tools)
to every build, and `None` (the default) keeps the previous behaviour. A growing history
is not part of this floor: that is context management's job (ADR 0005).

モデルの窓が分かっていて(設定・モデル自身・能力表)、SOUL + Identity Anchors + そのターンに送る
ツール定義の見積もりが窓の 9 割を超える場合は送信せず、チャットに `context_window_too_small` を表示します(一部のローカルサーバーは
収まらない入力の先頭 = SOUL を黙って捨てるため)。既定の 200k しか分からないときは警告のみ。

SOUL と Identity Anchors は予算で短縮しません。両者だけで予算を超える場合は他の節を全て落とし、
予算超過のまま全文を送ります(警告ログを 1 回)。

日本語(かな・漢字・全角記号)は 1 文字 = 1 トークン、それ以外は従来どおり 4 文字 = 1 トークン
(切り上げ)。日本語を含まないテキストの見積もりと、予算超過時の切り方は変更前と同一です。
係数は定数で、設定での上書きはありません。

## Measurements / 測定 (2026-10-23)

Counted locally with `tiktoken` 0.14 (`o200k_base`, `cl100k_base`) and Hugging Face
`tokenizers` 0.23 (tokenizer files of the listed models), no special tokens. Neither
library is a dependency of Stira; the measurement used a throwaway environment. Samples:
the previous shipped Japanese sample SOUL (3,388 chars; replaced by `examples/personas/resolver/SOUL.md`, 3,346 chars), the episode bodies in
`tests/fixtures/harvest_replay_golden.json` (Japanese and with their ASCII front
matter), `docs/memory-autonomy_ja.md`, and English/code from this repository.

Tokens per wide character, after subtracting 1/4 token per other character:

| Sample | o200k (GPT-4o, gpt-oss) | cl100k (GPT-4) | Qwen 2.5 / 3 | Gemma 3 | Llama 3.2 | Mistral v0.3 | Mistral Nemo |
|---|---|---|---|---|---|---|---|
| SOUL (3,388 chars, 2,880 wide) | 0.84 | 1.13 | 0.73 | 0.63 | 0.76 | 1.19 | 0.81 |
| Episodes, Japanese (628 / 214) | 0.94 | 1.32 | 0.94 | 0.81 | 0.89 | 1.39 | 1.06 |
| Japanese doc (6,571 / 2,762) | 0.97 | 1.26 | 0.98 | 0.85 | 0.93 | 1.39 | 1.02 |

Estimate vs. measured tokens (error = estimate / measured − 1; before → after):

| Sample | Measured (range over the 7 tokenizers) | Old estimate | New estimate | Error, old | Error, new |
|---|---|---|---|---|---|
| SOUL (Japanese) | 1,947–3,553 | 847 | 3,007 | −56% … −76% | −15% … +54% |
| Episodes (Japanese) | 277–400 | 157 | 318 | −43% … −61% | −20% … +15% |
| Japanese doc | 3,304–4,805 | 1,643 | 3,715 | −50% … −66% | −23% … +12% |
| Episodes with front matter (99% ASCII) | 3,266–4,483 | 3,007 | 3,091 | −8% … −33% | −5% … −31% |
| English doc (`context-management.md`) | 1,757–2,087 | 1,830 | 1,830 | −12% … +4% | unchanged |
| Python (`context_assembler.py`) | 3,519–4,573 | 4,104 | 4,104 | −10% … +17% | unchanged |

On Japanese, the newer tokenizers (o200k, Qwen, Gemma 3, Llama 3, Mistral Nemo) land
between −4% and +54% (Gemma 3 on the SOUL), mostly over — a budget that errs on the safe
side for a small window. The older `cl100k` and Mistral v0.3 still need about 11–23%
more than estimated on Japanese prose.

The assembled prompt (`build_system_prompt`) with the Japanese SOUL
(`include_identity=False`, two short rules and episodes) — tokens measured with o200k,
cl100k, Qwen 3, Gemma 3, Llama 3.2 and Mistral Nemo:

| `max_tokens` | Before: chars → tokens | After: chars → tokens |
|---|---|---|
| 8000 | 3,659 → 2,044–3,457 (fits; identical before/after) | same bytes |
| 2000 | 3,659 → 2,044–3,457 (not trimmed: estimate 915) | 2,223 → 1,264–2,198 |
| 500 | 2,000 → 1,143–1,972 | 574 → 338–565 |

## Limits / 限界

- Every model has its own tokenizer; this is an estimate, not a count. Claude's tokenizer
  is not public and was not measured (no model calls were made).
- Hangul, Thai, Cyrillic, emoji and other non-ASCII scripts were not measured and keep
  1/4 token per character, which **under-estimates** them (emoji and Hangul commonly cost
  one or more tokens each).
- Short ASCII pieces inside Japanese text (Markdown, front matter keys) cost more than
  1/4 token per character, which is why the mixed samples are under-estimated.
- Chinese text uses the same ranges but was not measured separately.
- `retrieval_max_tokens` (context management) caps the read-back by characters (×4) and
  is not changed here.
