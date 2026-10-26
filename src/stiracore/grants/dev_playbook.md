Working rules for this goal. They come from the operator; they do not change the goal and do not widen any permission.

Before you start
- Read the existing code and docs first; do not redo work that is done. An open task may already be done: confirm it in the code.
- For three or more steps, write a short plan first: requirements, design, tasks, expected test results.

Build
- Make small, surgical edits. Never rewrite a whole existing file. Save large work in small steps.
- Do not guess expected values: measure, then write the test.
- Run dependent steps one after another: create, check, then run.

Check
- Do not state a result before the tool has returned it. When a result looks wrong, suspect your own step first and confirm the fact with a simple check.
- Before you claim that something passes, run the relevant checks. Confirm once that a passing test fails without your change.
- Reproduce a finding before you change code for it. If it does not reproduce, leave the code and pin the behaviour with a test.

Report
- Use goal_report with its codes. Claim completion only after the checks pass; a person verifies every claim.
- When you are blocked or a decision is needed, report blocked with its code and stop.
- Anything that leaves the workspace (publish, send, merge) needs a person: report blocked with needs_decision.
