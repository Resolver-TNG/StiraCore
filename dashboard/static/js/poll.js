// Polling scheduler (#369): one place for how the pages poll the server.
//
//  - A hidden tab (document.hidden) does not poll at all; on return it polls once at once.
//  - A failed run backs off exponentially (base * 2^fails), capped.
//  - A run that found nothing new widens the interval step by step up to ``max``;
//    anything new, ``hold()`` (something that needs a quick reaction: a turn is running,
//    an approval waits) or ``boost()`` (an event that says "now") puts it back to ``base``.
//  - ``poke()`` polls now, e.g. when an approval request arrives on the page's own stream.
//
// ``run()`` returns a Promise: false = nothing changed, anything else (also undefined) =
// something changed, so a caller that does not know never gets slowed down. A rejection is
// a failure. No DOM beyond the injected ``doc``; timers are injected, so it runs in Node
// tests (tests/poll_test.cjs).
//
// Loaded as a classic script (window.StiraPoll) and, in Node, as a CommonJS module.
(function (root) {
  "use strict";
  const GRACE = 3; // unchanged runs at each step before the interval doubles
  const BACKOFF_MAX = 30000; // ms; a failing endpoint is asked at most this often

  // The delay (ms) after a run. ``fails`` consecutive failures win over idleness.
  function nextDelay({ base, max = base, fails = 0, idle = 0, grace = GRACE, backoffMax = BACKOFF_MAX }) {
    if (fails > 0) return Math.min(base * 2 ** Math.min(fails, 16), Math.max(backoffMax, base));
    const steps = Math.floor(idle / grace);
    return Math.min(base * 2 ** Math.min(steps, 16), Math.max(max, base));
  }

  function create(options) {
    const {
      run, base, max = base, hold = () => false,
      doc = typeof document !== "undefined" ? document : null,
      setTimer = (fn, ms) => root.setTimeout(fn, ms),
      clearTimer = id => root.clearTimeout(id),
      grace = GRACE, backoffMax = BACKOFF_MAX,
    } = options;
    let timer = null, running = false, started = false, listening = false, again = false, fails = 0, idle = 0, due = 0, last = 0;
    // The tab's return polls at once; the listener lives only while started (a stopped poller
    // leaves nothing behind: the Stage board creates one per opening, W5b audit D2).
    const onVisibility = () => {
      if (hidden()) clear();
      else if (started) { idle = 0; void tick(); }
    };
    const now = options.now || (() => Date.now());
    const hidden = () => Boolean(doc && doc.hidden);

    function clear() {
      if (timer !== null) { clearTimer(timer); timer = null; }
    }
    function schedule() {
      clear();
      if (!started || hidden()) return;
      const delay = nextDelay({ base, max, fails, idle, grace, backoffMax });
      due = now() + delay;
      timer = setTimer(tick, delay);
    }
    async function tick() {
      timer = null;
      if (!started || hidden() || running) return;
      running = true;
      try {
        const changed = await run();
        fails = 0;
        idle = changed === false && !hold() ? idle + 1 : 0;
      } catch {
        fails += 1;
      } finally {
        running = false;
        last = now();
        // A poke() that came in while this read was in flight: that answer may predate the
        // event the poke is about, so read once more at once.
        if (again && started && !hidden()) { again = false; void tick(); } else { again = false; schedule(); }
      }
    }

    return {
      // ``wait``: the first poll comes after one interval (the caller has just read).
      start(startOptions) {
        if (started) return;
        started = true;
        if (doc && doc.addEventListener && !listening) {
          listening = true;
          doc.addEventListener("visibilitychange", onVisibility);
        }
        if (hidden()) return;
        if (startOptions && startOptions.wait) schedule();
        else void tick();
      },
      stop() {
        started = false;
        clear();
        if (listening && doc && doc.removeEventListener) {
          listening = false;
          doc.removeEventListener("visibilitychange", onVisibility);
        }
      },
      // Poll now (a hidden tab stays quiet; it polls when it returns).
      poke() {
        idle = 0;
        if (running) { again = true; return; }
        if (!started || hidden()) return;
        clear();
        void tick();
      },
      // Back to the short interval without polling now. Only ever brings the next run
      // forward: a pending run (a long idle wait, or a failure's backoff deadline) is never
      // pushed further out, so a caller that boosts often cannot starve a retry.
      boost() {
        idle = 0;
        if (!started || hidden() || running || timer === null) return;
        if (due - now() > base) {
          const wanted = now() + base;
          clear();
          due = wanted;
          timer = setTimer(tick, base);
        }
      },
      state() { return { fails, idle, delay: nextDelay({ base, max, fails, idle, grace, backoffMax }), running, scheduled: timer !== null, last }; },
    };
  }

  // The common page loop: ``read()`` returns false (nothing new), true/undefined (something
  // new) or null (could not be read: a failure, backed off). Starts at once; returns the
  // poller (poke / boost). ``hold()`` true keeps the short interval.
  function keep(read, base, max, hold, doc, startOptions, backoffMax) {
    const poller = create({
      base, max, hold, doc,
      ...(backoffMax ? { backoffMax } : {}),
      run: async () => {
        const result = await read();
        if (result === null) throw new Error("read failed");
        return result;
      },
    });
    poller.start(startOptions);
    return poller;
  }

  const api = { GRACE, BACKOFF_MAX, nextDelay, create, keep };
  root.StiraPoll = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
