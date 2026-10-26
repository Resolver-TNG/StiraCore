"""Emotion-module ablation harness (#143). Measurements only — see docs/eval/.

    python -m stiracore.eval validate
    python -m stiracore.eval run --runner scripted --seeds 0,1 --out eval_out
    python -m stiracore.eval compare eval_out/history/A.json eval_out/latest.json
    python -m stiracore.eval labeling-sheet --out labeling/
    python -m stiracore.eval audit-pairs eval_out/latest.json --a real --b neutral --out audit/

``--runner nxp`` calls a real model and is disabled unless ``--allow-llm``,
``--model``, ``--max-turns``, ``--max-cost-usd``, ``--price-in`` and
``--price-out`` are all given.
"""

from __future__ import annotations
