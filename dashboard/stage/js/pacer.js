// Frame pacer for the Stage's loop (#341): requestAnimationFrame fires at the display's rate
// (60, 120 on ProMotion ...); the scene update and the WebGL render run only at the target rate
// (30 fps by default: pixel art reads better at 30, and the GPU does a quarter of the work of
// 120). Pure: no DOM, no clock of its own — the caller passes the rAF timestamp (ms).
//
// Everything that moves in the Stage is driven by real time (dt seconds, or the accumulated
// `now`), never by the number of frames, so a lower rate changes how often the picture
// refreshes and nothing else (tests/stage_pacer_test.cjs).

export const DEFAULT_FPS = 30;
export const MIN_FPS = 15;
export const MAX_FPS = 120;
export const MAX_DT = 0.1; // seconds: a long gap (tab in the background, a stall) is not replayed
export const FPS_KEY = 'stira.stage.fps'; // localStorage; the only stored setting
export const BOARD_FPS = 60; // while the whiteboard moves (270 ms): smooth, see main.js

// An integer from MIN_FPS to MAX_FPS written in plain digits, otherwise null ("30.5", "1e1",
// "0x1e", "", "abc", 14 and 121 are all refused).
export function parseFps(value) {
  if (typeof value === 'number') return Number.isInteger(value) && value >= MIN_FPS && value <= MAX_FPS ? value : null;
  if (typeof value !== 'string') return null;
  const s = value.trim();
  if (!/^\d{1,3}$/.test(s)) return null;
  const n = Number(s);
  return n >= MIN_FPS && n <= MAX_FPS ? n : null;
}

// The target rate. ?fps=N wins when the parameter is present (a bad value means the default,
// not the stored one, so a link always behaves the same); otherwise the stored value; otherwise
// the default. readStored() may throw (storage blocked): that is the default too.
export function pickFps(search, readStored) {
  const params = new URLSearchParams(search || '');
  if (params.has('fps')) return parseFps(params.get('fps')) ?? DEFAULT_FPS;
  let stored = null;
  try {
    stored = readStored ? readStored() : null;
  } catch {
    stored = null;
  }
  return parseFps(stored) ?? DEFAULT_FPS;
}

// Accumulator pacer. frame(t, { minFps }) -> { run, dt }
//   run: draw this rAF (update + render); dt: seconds since the previous drawn frame, capped
//   at MAX_DT. The first frame runs at once with dt 0. A frame is due once the accumulated
//   time reaches the interval, less a small tolerance so that a 60 Hz display's 16.7 ms
//   ticks give exactly 30 fps and rAF jitter does not skip a frame; the remainder carries over
//   so the average rate is exact on any display (144 Hz included). A backlog is dropped, never
//   caught up. reset() (tab hidden) makes the next frame the "first frame" again.
export function createPacer({ fps = DEFAULT_FPS, maxDt = MAX_DT } = {}) {
  const st = { fps: parseFps(fps) ?? DEFAULT_FPS, acc: 0, tick: null, run: null };
  return {
    get fps() {
      return st.fps;
    },
    setFps(v) {
      st.fps = parseFps(v) ?? DEFAULT_FPS;
    },
    reset() {
      st.tick = null;
      st.run = null;
      st.acc = 0;
    },
    frame(t, { minFps = 0 } = {}) {
      if (!Number.isFinite(t)) return { run: false, dt: 0 };
      if (st.tick === null) {
        st.tick = t;
        st.run = t;
        st.acc = 0;
        return { run: true, dt: 0 };
      }
      const rate = Math.min(MAX_FPS, Math.max(st.fps, minFps));
      const interval = 1000 / rate;
      const tol = Math.min(2, interval * 0.1);
      const gap = Math.max(0, t - st.tick);
      st.tick = t;
      st.acc += Math.min(gap, maxDt * 1000);
      if (st.acc < interval - tol) return { run: false, dt: 0 };
      const dt = Math.min(maxDt, Math.max(0, (t - st.run) / 1000));
      st.run = t;
      st.acc -= interval;
      if (st.acc >= interval - tol) st.acc = 0; // a backlog is dropped, not replayed
      if (st.acc < -tol) st.acc = 0;
      return { run: true, dt };
    },
  };
}
