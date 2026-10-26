/* Approvals page (#189): pending queue + decision history, polled from /api/hitl. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  if (typeof StiraApprovals === "undefined") return;
  const queue = StiraApprovals.create({
    page: true,
    headingLevel: 3,
    list: $("approval-list"),
    live: $("approval-live"),
    history: $("approval-history"),
    empty: $("approvals-empty"),
    status: $("approvals-status"),
    onEmpty: () => $("pending-title").focus?.(),
  });
  // #369: not while the tab is hidden, slower while the queue is empty and unchanged, short
  // while a request waits; a failed read backs off (poll.js). Without poll.js: a plain timer.
  if (typeof StiraPoll !== "undefined") {
    // A failing queue is asked at least every 5 s (the idle maximum of a new card).
    StiraPoll.keep(() => queue.refresh(), 2000, 5000, undefined, document, undefined, 5000);
  } else {
    void queue.refresh();
    setInterval(() => { if (!document.hidden) void queue.refresh(); }, 2000);
  }
  setInterval(() => { if (!document.hidden) queue.tick(); }, 1000);
})();
