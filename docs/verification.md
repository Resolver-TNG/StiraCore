# Checking a page without looking at it — `page_check` `explore` and `design_lint`

[日本語](verification_ja.md)

#747 (a continuation of #449, [ADR 0015](adr/0015-verification-browser-tool.md)). Two more
actions of the `page_check` tool, for a model that cannot see its own page. Both are **off
unless the call asks for them**; a call without them returns what it returned before. The
rule of the tool is unchanged (**Ask**, no Always, a card in every YOLO window), and so are
the closed network, the workspace boundary and the launch guard: see
[tool-execution-paths.md](tool-execution-paths.md#page_check-verification-browser-adr-0015).

Both reports hold **counts, ratios, booleans and Stira's own ids only**. No text of the page
is in them; the page's text still comes only in the `<page-content>` block (data, not
instructions). No model is called.

## `explore` — a bounded, seeded walk

```json
{"target": "game.html",
 "actions": [{"explore": {"max_steps": 40, "seed": 0, "reach_markers": ["The End"]}}]}
```

| Key | Meaning |
|---|---|
| `max_steps` | 1–200, default 40. One step is one click or one Enter. |
| `seed` | a non-negative integer, default 0. The same page and seed give the same walk. |
| `reach_markers` | at most 10 strings (≤ 200 characters each) to look for in the page's visible text. |

At each step Stira lists the clickables (`a[href]`, `button`, `input`, `select`, `summary`,
`[onclick]`, `[role=button]`, `[role=link]`, `[tabindex]`, and elements with a pointer
cursor) that are visible, inside the 1280×800 viewport and not covered by another element
(at most 50, in document order), and the seed picks one of them or the Enter key. It never
types, never scrolls and never clicks something a person could not click.

The report (`explore` in the result):

| Field | Meaning |
|---|---|
| `steps`, `clicks`, `keys` | what was done (`steps` = `clicks` + `keys`) |
| `distinct_screens` | how many different visible texts were seen (a hash computed in the page) |
| `unique_lines` | how many different text lines were seen (hashes) |
| `errors` | page + console errors during the walk |
| `reached` | one boolean per `reach_markers` entry, in order |
| `reached_any` | any of them |

The walk stops early near the call's 30 s limit (the step's `code` is `time_limit`; the
report is still returned). `explore` counts as one of the call's 20 actions and may be used
once per call. A page whose state depends on the clock or on `Math.random()` may give a
different walk with the same seed: the seed fixes Stira's choices, not the page's.

## `design_lint` — measurements, no model

```json
{"target": "index.html", "actions": [{"design_lint": true}]}
```

Measured in the page at the moment the action runs (put it after the actions that bring the
screen you want to check):

| Field | Meaning | Flag (in `flags`) |
|---|---|---|
| `empty_area_ratio.bottom` / `.left` / `.right` | share of the viewport with no text or media below / beside the content (0–1) | `empty_bottom` at ≥ 0.25; `empty_sides` when left + right ≥ 0.5 |
| `min_font_px`, `small_text` | the smallest font size of an element with its own text; how many are under 10 px | `small_text` |
| `contrast_failures` | texts under WCAG AA contrast (4.5 : 1, large text 3 : 1) against a solid background; a background image is not judged | `low_contrast` |
| `overflow_clipped` | elements whose content is cut by `overflow: hidden` / `clip` | `overflow_clipped` |
| `overlap_pairs` | pairs of text / media boxes, neither inside the other, that overlap by a quarter of the smaller one | `overlap` |
| `dev_strings.lines`, `dev_strings.patterns` | visible text lines that look like developer output, and a count per pattern id | `dev_strings` |
| `elements_capped` | more than 5,000 elements: only the first 5,000 were measured | — |
| `ok` | no flag | — |

The pattern ids (a fixed list in Stira; the report never contains the matched text):

| id | Matches |
|---|---|
| `dev_label` | a line starting with `debug`, `scene`, `expr`, `guest`, `fps` or `window` |
| `dimensions` | `1280x800`-like sizes |
| `breakpoint` | `@900w`-like marks |
| `todo` | `TODO` |
| `undefined` | `undefined` |
| `nan` | `NaN` |
| `object` | `[object Object]` |

The list is not configurable from the call: a pattern runs inside the page, so it is Stira's
code, not an argument. A flag is a hint to look, not a verdict: a title screen may be
sparse on purpose, and "window" may be a word of the page. `design_lint` may be used once
per call.

## Module scripts in a workspace file page (#766)

Chrome refuses `<script type="module">` and `import` from a `file://` page, so a page made
of `index.html` + `src/*.js` used to stay on its first screen with **no error reported**.
`page_check` already pauses every `file://` request to enforce the workspace rule; it now
answers the allowed ones itself (ADR 0015 addendum rev 6, slice S9):

| Request | What happens |
|---|---|
| a file inside the workspace that `file_read` may read (symlinks followed, a regular file, ≤ 16 MiB, a plain `GET`) | answered with the file's bytes, a `Content-Type` from a fixed extension table (anything else `application/octet-stream`) and `Access-Control-Allow-Origin: *` — module scripts and `fetch()` of a workspace JSON file load |
| outside the workspace, through a symlink that leaves it, a directory, or on the read floor — also when the path changed into one of these after the first check | refused and counted (`blocked.outside_workspace`), as before |
| a missing file (a plain `GET`) | failed and listed in `missing_files` — also a missing module or `fetch()` target |
| the page itself and its frames, a `Range` request, a larger file, anything that is not a regular file | as before (Chrome reads the checked file) |
| after 64 MiB were answered in one call | nothing more is answered (as before: Chrome reads the checked file); the call's `reason` is `fulfil_limit` |

The file is opened the way `file_read` opens one and checked again after the open; the bytes
come from that open file. No listener and no port are opened, the network stays closed, the page's origin stays
opaque (cookies and storage are untouched), and no rule or action changed. A workspace script
whose load failed — a missing `import`, for example — is now counted in
`page_errors` as the code `script_load_failed` (never the URL or the file name), so a page
that never started is visible. Not counted: a module file that loaded but that Chrome refuses
for its type (`.ts`, `.txt`, no extension); the page text shows that it did not start.

An approved `page_check` can show the page what `file_read`'s read floor allows, whatever
`file_read`'s own rule (Ask / Never) is: the rule is per tool.

## Logs

The input is logged as a count per action kind (never the markers). The result keeps the
`explore` counts and `reached_any`, and for `design_lint` the flags, the counts and the
pattern ids.

## Not in this change

Running `page_check` with no card and a "verify after each round" hook (#747 proposals 1,
5) are separate decisions. A vision-model reviewer (proposal 4) came later as `page_review`
(below; off unless set). Judging taste stays with a person.

ADR 0015 Addendum rev 6, slice S1 (#765) added a measurement only: at start Stira opens its
own fixed page and checks that nothing reaches a loopback canary (`verified` / `unverified` /
`unmeasured`; Linux is `unmeasured`), and an axis check reads ten invariants. `stira doctor`
shows both, and the settings page's Diagnostics section has a read-only row with the chat
server's current values ([diagnostics.md](diagnostics.md)). No rule reads them: `page_check`
is still Ask or Never, and a card in every YOLO window.

## `page_look` (ADR 0015 Addendum rev 6, slice S3)

A second page tool, `page_look`, is `page_check` for **workspace files only**: the same
actions (`explore` and `design_lint` included), limits, closed network and screenshots, but
a web URL of any form is refused `localhost_not_allowed` before a browser starts, and it never
gets a localhost port. It has `target`, `actions` and `wait_ms`; other inputs are ignored. It
is offered with `page_check` (same browser discovery). Its rule is **Never / Ask / conditional
Always** (slice S4): a saved Always runs the call without a card only while all four hold:
(1) the axis check's cached result is a fresh `pass`; (2) the turn is a chat turn or a YOLO
window's continue; (3) `file_read` is Always; (4) the rule has answered fewer than 12 calls
in this turn and fewer than 3 calls of the turn failed (any reason, a success between them or
not; a call still running counts as a failure). In a window's automatic continues (4) is
counted per window too. Otherwise it is a card (`rule_always_capped` with the reason code).
The chat server measures these conditions at every call, so a saved Always runs without a
card there while they hold. After the gate, the tool reads the conditions and both saved
rules (`page_look`, `file_read`) again from the file just before a browser starts and refuses
when they changed. A call that neither a person's card nor the gate's rule answer stands
behind (for example a child agent without the approval gate) is refused `unanswered_call`. A
`trust_all` rules file reads Ask for `page_look` unless it names the tool. It is a card in
every YOLO window while capped, never given to a `delegate` child or a job, and not nameable
in a grant. A
call that no person's card approved writes one
`audit.page.page_look.<status>` row of codes, counts and hashes (never page text, selectors,
typed text, paths or URLs). Stira also keeps, in memory only, the SHA-256 and size of every
screenshot either tool wrote (64 runs, 30 minutes); `page_review` (below) reads it and sends
only a file that still matches. Details: [tool-execution-paths.md](tool-execution-paths.md#page_look-file-targets-only-adr-0015-addendum-rev-6-s3).

## `page_review` (ADR 0015 Addendum rev 6, slice S6, #774)

`page_review(run)` sends the screenshots of one `page_check` / `page_look` run of this
session to a second model, the **reviewer**, and returns its remarks on how the page looks.
It is **off unless a person sets** `task_models.visual_review` (or
`STIRA_TASK_MODEL_VISUAL_REVIEW`) to a vision model and a budget
`visual_review.daily_tokens` ([task-models.md](task-models.md),
[configuration.md](configuration.md)): unset, the tool is not registered, nothing is sent,
and the agent's prompt does not mention it.

* **The card:** what you approve is written by Stira, not by the model: the run id, that
  the call sends screenshots, the reviewer's provider and model id, and how many images.
  Anything else the model put into the call is dropped before the card is shown. With
  `inherit` the card names the chat model as it is at that moment. If you switch the chat
  model on the settings page while the server runs, the review follows it only when the
  new model is of the same provider and has vision; otherwise nothing is sent
  (`reviewer_changed`) until you switch back or restart. A review also goes only to the
  reviewer the approved card named.
* **What is sent:** only the PNGs Stira itself wrote for that run (at most 4), each read
  again without following a link and compared with the size and SHA-256 Stira recorded
  (`shot_changed` / `run_unknown` otherwise; `shot_missing` when the run's folder is no
  longer on disk — only the newest 5 runs are kept, so call `page_look` again), and one
  fixed rubric. No tools, no
  conversation, no goal, no page text, no file name, no path. The pixels go to the
  reviewer's provider; a remote reviewer sees whatever the page drew.
* **What comes back:** Stira's own JSON `review: {status, issues, by_severity,
  rubric_version}` and one `<visual-review>` block of at most 8 remarks (`severity`, `shot`,
  `region`, `category` from fixed lists, and a `note` of at most 200 characters, defanged and
  masked). An answer outside the schema is `review_unparsable`; an entry outside it is
  dropped. The remarks are data, not instructions, and advice: nothing in Stira passes or
  fails anything on them.
* **Rule: Ask or Never.** Every call is a person's card, in a YOLO window too; a call no card
  approved is refused `unanswered_call`. Never given to a `delegate` child or a job, not
  nameable in a grant. Hidden at WARNING and above.
* **Limits:** 60 s (Stira stops waiting then, whatever the provider does, and never reads
  a late answer; while 2 such calls still run, a new one is refused `review_busy` — a
  reviewer that never answers keeps it so until you restart the server, and a provider
  whose call is still running can delay the server's exit until that call ends), 2
  reviews per turn (`review_limit`; outside a chat turn: `no_turn`), the daily token budget
  (`review_budget`; the count is in memory and starts again at a restart; a call without
  reported usage is charged an estimate). A reviewer whose provider or model id is not a
  plain name gets nothing (`reviewer_unnamed`). A reviewer that
  fails gives `review: {status: "error", code}` (`review_failed`, `review_timeout`,
  `review_unparsable`); the chat model is never asked instead.
* **Record:** every call that reaches the tool — one you approved on its card, or one
  the tool refused — writes one
  `audit.page.page_review.<status>` row: codes, counts, the screenshots' SHA-256, the
  reviewer's provider and model id, tokens, issue counts by severity. Never pixels, a note, a
  path or a URL. A card you reject runs nothing and writes no such row (the rejection is
  in the approval history). A turn you stop while the screenshots are out still gets its
  row.

Not built yet: a conditional Always for a local reviewer and the reviewer's
`loopback` / `remote` class in the record (slice S7), and `page_review` in a grant.
