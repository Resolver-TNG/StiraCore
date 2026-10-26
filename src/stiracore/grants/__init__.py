"""Grant + goal store and state machine (ADR 0011 §2, §8, §11; W4 of #296).

Execution is OFF: nothing in this package runs a model, a tool or a goal iteration (the
executor is W5). Everything is behind the gateway switch ``autonomy.stage_c`` (default
off): with it off no route is registered, no file is created and no thread starts.

* :mod:`.schema` — strict validation of the grant record and the issue request;
* :mod:`.state` — the goal × grant state machine as pure functions;
* :mod:`.store` — the durable ``grants.json`` (``0600``, chained records);
* :mod:`.service` — the human control plane (issue, revoke, pause, resume, verify);
* :mod:`.routes` — the Flask routes of the control plane.

See ``docs/grants.md``.
"""
