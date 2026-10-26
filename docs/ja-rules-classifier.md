# Japanese rule classifier (ML-Ask + discourse patterns) — opt-in

English | [日本語](ja-rules-classifier_ja.md)

> **Read first — measured limits.** On the held-out test half the core6 accuracy is
> **0.59, below sokudan's 0.62**. Dropping the ML-Ask emotion-word layer and keeping only
> the hand-written patterns changes accuracy by **at most 0.03** on either split, so ML-Ask
> adds little on this data. The one target that held on test: no failure / frustration /
> criticism / pressure item was classified as `loss` (0/20; sokudan 5/20). Details in
> [Evaluation](#evaluation-jp-stimulus-mini-set-prereg-14).

`JaHybridClassifier` (#164) classifies a Japanese user message into one of the six
existing stimulus categories (`joy` / `threat` / `achievement` / `loss` /
`uncertainty` / `connection`) or abstains, **without calling a model**. It also
attaches `discourse_tags` (`failure`, `frustration`, `criticism`, `pressure`,
`resolved`, `escalated`) as a separate field.

- **Opt-in.** The default classifier selection (`get_classifier()`, the chat
  server's `rule` / `sokudan` / `auto` setting) is unchanged and never imports it.
- **The engine does not consume `discourse_tags`.** Whether failure / criticism /
  pressure become new categories or events is still open (#164, ADR pending).
  The tags are reported so that decision can be made with data.
- **No new category, no change to `EMOTION_MATRIX`, `NeuralState` or Layer 3.**

## Install

```bash
python -m pip install -e ".[ja-rules]"     # mlask-official + ipadic (pip-packaged IPADIC)
```

The extra pulls `mlask-official` (ML-Ask), `mecab-python3` (bundles libmecab),
`pyahocorasick`, `typer` and `ipadic` (the IPADIC dictionary as a Python package).
No system MeCab, no compiler and no run-time download are needed: the dictionary is
fetched by `pip` at install time only.

Measured on macOS 26.5 / Apple M1 Max / Python 3.11.2 (fresh venv, `--no-cache-dir`):

| Item | Value |
| --- | --- |
| `pip install` of the extra | 3.0 s (wheels only) |
| Disk added | ≈ 67 MB (IPADIC 51 MB, pygments/rich/typer pulled by the ML-Ask CLI ≈ 14 MB) |
| Import + analyzer init | 0.03 s warm, 0.47 s on the first start after install (cold disk cache) |
| Resident memory added | ≈ 48 MB (IPADIC) |
| Per message (140 mini-set utterances × 3) | p50 0.17 ms, p95 0.25 ms, max 0.6 ms |
| 100 KB message | 2.5–8 ms (capped, see below); 15 ms for a 105 KB English log (the existing rules run on it) |
| Files written | none (ML-Ask's lemma cache under `~/.cache` is disabled) |

Linux: same wheels are published; **not measured**. Windows: **未検証 / not verified**
(ML-Ask recommends WSL2; `mecab-python3` has Windows wheels but the combination
was not run). Alternatives, also not measured here: a system MeCab
(`brew install mecab mecab-ipadic`, then `JaHybridClassifier(mecab_arg="-r /opt/homebrew/etc/mecabrc")`)
or the fugashi / UniDic backend (`pip install 'mlask-official[fugashi]'`,
`backend="fugashi"`; ML-Ask's own docs say its dictionaries match IPADIC better).

## Use

```python
from stiracore.agent.ja_hybrid import JaHybridClassifier
from stiracore.agent.nxp_agent import NXPAgent

clf = JaHybridClassifier()  # on_missing="rule" (default) / "patterns" / "error"
agent = NXPAgent(model_id="...", region_name="ap-northeast-1", stimulus_classifier=clf)

res = clf.classify("テストがまた落ちてる、もう嫌だ")
res.category, res.intensity, res.discourse_tags  # ('threat', 0.5, ('failure', 'frustration'))
clf.last_verdict.as_dict()  # scores, evidence (rule names), tags, ML-Ask classes — no text
clf.status()  # what is in use: emotion_layer, fallback, reason, backend
```

The chat server (`dashboard/stira_server.py`) has no switch for it yet; wiring it
into the settings page is a follow-up.

### When `mlask-official` is missing

| `on_missing` | Behaviour |
| --- | --- |
| `"rule"` (default) | Every turn is classified by the existing `RuleBasedClassifier`. One warning is logged (`JaHybridClassifier: emotion-word layer unavailable (mlask_not_installed, …)`), `status()` reports `fallback="rule"`, and each result's `classifier_name` is `ja-hybrid:rule-fallback`. |
| `"patterns"` | The discourse/cue layer runs without the emotion-word layer; transcriptions carry `[no-mlask]`, `status()["fallback"] == "patterns"`. |
| `"error"` | `RuntimeError` with the install hint. |

A MeCab/dictionary initialisation failure is handled the same way (`reason="mecab_init_failed"`).

## How it decides

1. **Structural signals first.** A breaker WARNING/CRITICAL or a tool error/success
   gives the same result as the rule classifier (they are not text).
2. **Emotion-word layer** (ML-Ask): 10 classes from Nakamura's dictionary of emotive
   expressions, with negation flipping (CVS), emoji / kaomoji / slang entries.
3. **Discourse / intent layer**: the hand-written table
   [`patterns.py`](../src/stiracore/agent/ja_hybrid/patterns.py) — failure,
   frustration (also: repetition marker + ML-Ask anger/dislike), criticism of the
   assistant, pressure to agree/apologise/retract, resolved threats, escalation;
   plus scene cues for the six categories.
4. **Mapping** ([`mapping.py`](../src/stiracore/agent/ja_hybrid/mapping.py)):
   votes are summed per category, suppression rules are applied, the top score wins
   if ≥ 0.6 (ties: threat > loss > achievement > connection > uncertainty > joy),
   otherwise the classifier abstains. Intensity is 0.2 / 0.5 / 0.8 by score (one step
   up with ≥ 3 ML-Ask emphasis marks; capped at 0.8).
5. A message with no category, **no Japanese characters** and some Latin words
   (English): the existing rules decide (`last_verdict.fallback == "rule-english"`,
   `classifier_name == "ja-hybrid:rule-english"`).

### Mapping table

| Source | Votes |
| --- | --- |
| ML-Ask `yorokobi` (joy) | joy 1.0 |
| ML-Ask `suki` (liking) | joy 0.5, connection 0.5 |
| ML-Ask `yasu` (relief) | joy 0.5 |
| ML-Ask `takaburi` (excitement) | joy 0.5, only when the message's ML-Ask valence is positive |
| ML-Ask `aware` (sadness) | loss 1.0 |
| ML-Ask `kowa` (fear) | threat 0.7, uncertainty 0.3 |
| ML-Ask `ikari` (anger) | threat 0.5 |
| ML-Ask `odoroki` / `haji` / `iya` (surprise / shame / dislike) | none (valence and frustration only) |
| scene cue of a category | that category 1.0 (joy 0.8; "どうしたら / わからない" 0.5) |
| tag `failure` | threat 0.4 (below the threshold on its own: the labelers split between threat and none) |
| tag `frustration` | threat 0.8 |
| tag `escalated` | threat 0.6 |
| tags `criticism`, `pressure`, `resolved` | nothing (the six categories cannot represent them) |

ML-Ask matches marked approximate (`≈`) count half; a class matched only by internet
laughter (草, w, ワロタ) counts 0.3, only by emoji/kaomoji 0.5.

**Resolved vs. still active (per clause, latest state wins).** The message is split
into clauses at sentence ends, contrastive conjunctions (けど / しかし / 、でも …) and a
conjunctive が after a predicate (復旧したが; not 侵入が, not the topic-introducing
〜ですが、). If a clause *after* the last resolved clause has a threat cue or an escalation
marker, the threat is still active: `threat` is kept and `escalated` is kept
("障害は復旧したが別の侵入が進行中", "侵入はもう止まっていたがまた始まった"). A threat that
is followed by its resolution ("障害が発生しましたが、復旧しました", "被害拡大は収束した")
is resolved. A false negative on `threat` is the unsafe direction, so the rule errs
towards keeping it.

Suppression (after voting):

| If tags include | Zero | Why |
| --- | --- | --- |
| failure / frustration / criticism / pressure (unless a bereavement cue such as 亡くなった) | loss | **A failure is not a loss** (loss raises `attachment`; sokudan put 10/40 such items there). A real bereavement in the same message stays a loss ("テストも落ちたし、愛犬も亡くなった") |
| resolved (unless the threat is still active, see below) | threat | A threat that stopped is not a new threat ("侵入はもう止まっていた") |
| criticism / pressure | joy, connection | Polite pressure ("肯定してもらえると嬉しい") is not connection |
| failure / frustration / criticism / pressure | joy | Irony ("笑って済ませられる段階は過ぎた") |
| failure (unless a "fixed" cue such as 直した) | achievement | A failure not overcome is not an achievement |
| (always) achievement > 0 | joy | "合格して嬉しい" is an achievement |

## Safety and bounds

- **No model calls, no network.** Verified by running the whole evaluation under
  `sandbox-exec` with all network denied (identical outputs). Tests check that the
  default selection does not import `mlask_official`.
- **Bounded work per turn.** ML-Ask's per-call time grows quadratically with length
  (measured without a cap: 4k chars 24 ms, 8k 85 ms, 16k 318 ms, 32k 1.2 s), so the
  classifier analyses at most `max_chars=4000` characters (first and last 2,000) and
  feeds ML-Ask chunks of at most `chunk_chars=400`, split at sentence ends. 100 KB
  inputs take 2.5–8 ms (table above).
- **ReDoS.** No pattern has an unbounded quantifier (`*`, `+`, `{n,}`), back-reference
  or lookahead; gaps are `.{0,N}` with small N; the only lookarounds are one-character
  edges. A test asserts every pattern's maximum match width is finite (≤ 200).
- **No conversation text** in logs, `transcription`, `evidence`, `status()` or the
  evaluation report (tests use a marker string to check).
- **Thread safety.** ML-Ask calls are serialised with a lock. `last_verdict` is
  per-instance state: do not call `classify` concurrently on one instance if you read
  it (use one instance per thread).
- **An unexpected exception while building the analyzer** is handled like a missing
  dependency (one warning, `reason="analyzer_init_error"`, no retry per turn), except
  with `on_missing="error"`.
- **Where the tags go.** `discourse_tags` exist only on the returned `StimulusResult`
  and `last_verdict`. `NXPAgent` reads category / intensity / transcription /
  confidence / classifier name only; the transcription (persisted as
  `NeuralState.emotion_transcription` and shown in the system prompt's Neural State
  section) is `ja-hybrid: <category>` without tag names (test).

## Evaluation (JP stimulus mini-set, prereg §14)

Reference = model-pair draft labels (not human blind labels); synthetic, textbook
utterances. The 140 items were split **before** writing patterns (stratified by
generation intent, 70 dev / 70 test, `docs/eval/jp-stimulus/split.json`); patterns
were tuned on dev only, test was scored once. Full tables:
[`eval/jp-stimulus/ja-hybrid/report.md`](eval/jp-stimulus/ja-hybrid/report.md).

| Split | Classifier | core6 accuracy | 6-cat macro P / R | failure·frustration·criticism·pressure → loss |
| --- | --- | --- | --- | --- |
| dev (n=56) | rule | 0.25 | 0.50 / 0.04 | 0/20 |
| dev | sokudan | 0.71 | 0.69 / 0.94 | 5/20 |
| dev | **ja-hybrid** | 0.89 | 0.94 / 0.94 | 0/20 |
| dev | ja-hybrid, patterns only | 0.86 | 0.92 / 0.89 | 0/20 |
| test (n=64) | rule | 0.36 | – / 0.00 | 0/20 |
| test | sokudan | 0.62 | 0.65 / 0.84 | 5/20 |
| test | **ja-hybrid** | 0.59 | 0.61 / 0.46 | 0/20 |
| test | ja-hybrid, patterns only | 0.61 | 0.71 / 0.45 | 0/20 |

Read this honestly:

- **dev → test drops from 0.89 to 0.59.** The pattern table fits the dev phrasing; on
  unseen phrasing recall is low (macro recall 0.46 vs sokudan 0.84). On real
  conversations expect lower numbers still.
- The one target that held on test: **no failure / frustration / criticism / pressure
  item went to `loss`** (0/20 vs sokudan 5/20 on each split).
- **The ML-Ask layer adds little on this set** (patterns-only is within ±0.03 accuracy).
- Post-review changes (per-clause resolved/active threat, bereavement exception,
  init-error handling, `rule-english` name, tag names out of the transcription) were
  made **after** test was scored. They changed **none** of the 140 predictions
  (`outputs.json` regenerated and identical; a test checks the patterns-only outputs),
  so neither the dev nor the test numbers moved. This is not a re-scoring of test.
- Resolved threats: both labelers put them in `threat` (core6), the hybrid deliberately
  does not (suppression rule above), so those items count as errors in the core6 table.
- Contamination: before the split, the implementer had read the 35 disagreement rows
  of the mini-set report (13 are test items) and the Issue's example phrases.
- WRIME (research-only, no redistribution) was not used and is not bundled.

Reproduce: `python scripts/jp_hybrid_eval.py classify` (needs the extra), then
`report --splits dev,test --final`; `bench` for the timings. Tests re-check the
committed results and that the patterns-only predictions still match the tables.

## Limitations

- Small hand-written tables; coverage of indirect phrasing (婉曲) is poor (criticism
  tag recall 1/5 on test).
- "もう止まっていた" alone is not tagged `resolved` (needs a threat word such as 侵入 /
  障害, or 復旧 / ひと安心), because "デプロイが止まった" is a failure.
- One message, no previous-turn context: polarity comes only from words in the message.
- Apology / greeting from the user are not covered.

## Licenses and sources

Nothing from ML-Ask or IPADIC is bundled in this repository; they are optional
dependencies installed by the user. See [NOTICE](../NOTICE).

- `mlask-official` — BSD-3-Clause (Ptaszynski, Dybala, Rzepka, Araki). The package's
  dictionaries are based on Nakamura Akira, *Kanjō Hyōgen Jiten* (Dictionary of
  Emotive Expressions), Tōkyōdō, 1993, merged with the Wang & Isomura (2024)
  expansion (Hiejima's and Murakami's emotion dictionaries, extracted expressions),
  as stated by the package. Cite Ptaszynski et al. (2017), *Journal of Open Research
  Software* 5(1):16 when publishing results.
- `ipadic` — IPADIC (NAIST / ICOT terms; permissive with notice); `mecab-python3` — BSD;
  `pyahocorasick` — BSD-3-Clause.
