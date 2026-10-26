# Stage (#341)

Pixel-art view of the agent's activity and emotion. Design, rules, the emotion table and the
live event mapping: [docs/stage.md](../../docs/stage.md).

Live (opt-in): `STIRA_STAGE=1` when starting the chat server, then open `/stage/` (served from
`dashboard/templates/stage.html` inside the dashboard shell + an allow-list of the files
here). The text window holds the composer and the log (#350; `js/talk.js`, `js/backlog.js`,
`js/typewriter.js`; sending is `static/js/chat_core.js`, shared with the chat page).
Gateway `ui.home: "stage"` opens it on `/`.

Demo (dummy events, no server):

```sh
python3 -m http.server 8765 -d dashboard   # then open http://127.0.0.1:8765/stage/index.html
```

Keys: `Tab` raw view, `D` debug, `L` ja/en; demo: `Enter` next, `1`–`9` states,
`c t y u s r g m k h x a n` gadget states (coding, tests, subagents, memory, forgetting,
scheduler, tool failure, approval seals), `0` autoplay.

Rebuild (offline tools; the outputs are committed):

```sh
swiftc -O dashboard/stage/tools/subject_mask.swift -o /tmp/subject_mask   # macOS only, once
python3 dashboard/stage/tools/build_char.py    # numpy, scipy, opencv-python, Pillow
                                               # (also cuts the hair: tools/build_hair.py,
                                               #  and the motion parts: tools/char_parts.py)
python3 dashboard/stage/tools/build_bg.py      # numpy, scipy, Pillow
python3 dashboard/stage/tools/build_gadgets.py # numpy, Pillow (props: laptop, drones, ...)
dashboard/stage/tools/build_vendor.sh          # three.js -> vendor/three.stage.min.js
```
