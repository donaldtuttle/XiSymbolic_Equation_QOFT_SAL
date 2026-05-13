"""
layer6b_nla_telemetry.py — Layer 6b STUB. NOT runnable yet.

==============================================================================
GATE STATUS — read before running
==============================================================================
Layer 6b runs in parallel with Layer 6 and depends on the NLA telemetry
bridge that Cowork builds on the Windows box (see cowork_nla_build.md).
This file is a stub that pins the consumer contract — it will fail loudly
if invoked before that dependency is delivered.

Required upstream artifacts (Cowork P1-P5 must pass first):
  - nla_inference.py   exposing verbalize_activation(activation, layer) -> str
  - NLA checkpoint for the Qwen2.5 family registered per docs/inference.md
  - qwen2.5:7b pulled and confirmed via `ollama list` (Cowork P2)
  - the Xi-agent collapse hook wired per Stage 2 of cowork_nla_build.md

If any of those is missing, Layer 6b cannot run. Do not weaken this file
into a "best-effort" version that silently no-ops the NLA call — that
would defeat the entire L6b measurement.

Layer 6b mandate
----------------
While Layer 6 runs the label-semantics ablation (synonym substitution /
numeric threshold / one-shot example) on qwen2.5:7b, Layer 6b captures
NLA verbalizations on the SAME ticks of the SAME engine seeds. The
direct comparison at the end:

  Does the NLA verbalization mention oscillation, alternation, or
  back-and-forth patterns on ticks where the model's output label
  is wrong?

If YES: the activation pattern contains the oscillation signal but the
output pathway drops it. Representation-level encoding present;
labelling pathway is the bottleneck.
If NO: the pattern is not represented internally either. The
oscillating concept is genuinely absent from Qwen's residual stream on
this task.

Either outcome is informative. A "yes" reading is the load-bearing
positive result for the QOFT/NLA bridge — it would be the first
correlation between QOFT field telemetry and internal activation
language.

Sampling design (per Stage 2 of cowork_nla_build.md)
----------------------------------------------------
- Run Layer 4 format B (the augmented schema) at 80 trials on
  qwen2.5:7b. NOTE: per the run plan, Layer 4 is SKIPPED in the Qwen
  re-run tree; layer6b_nla_telemetry must therefore be re-targeted at
  Layer 6's runtime (label-semantics ablation) instead of Layer 4. The
  Cowork prompt's "Run Layer 4 format B" wording reflects the original
  pre-skip plan; substitute Layer 6's predict_ticks here.
- On every Lambda_psi event: capture NLA verbalization, log alongside
  QOFT telemetry, event_type="collapse".
- On every non-collapse tick: capture NLA verbalization with 1-in-5
  sampling, event_type="sample".
- Log file is append-only JSONL per Stage 3 schema.

Analysis (downstream, not in this file)
---------------------------------------
Compare verbalization content: collapse vs non-collapse ticks. Cross
the verbalization stream with the Layer 6 prediction CSV by (seed, tick).
For rows where Layer 6's output label was wrong on the oscillating GT,
inspect the corresponding NLA verbalization for keywords:
"oscillat*", "swing*", "alternat*", "back and forth", "bounce", etc.
A keyword hit rate significantly above the collapse-row baseline is
the NLA-QOFT bridge result.

Consumer contract (do not edit without coordinating with Cowork)
----------------------------------------------------------------
This module imports:

    from nla_inference import verbalize_activation

with signature:

    verbalize_activation(activation_vector, layer_index) -> str

Cowork's Stage 1 builds this. If the actual module name or signature
ships differently, update the import block AND the call sites below;
do not paper over the difference with try/except — that would mask a
silent contract drift.

Run
---
This file currently raises ImportError if invoked, because
nla_inference is not on the import path. That is intentional. To
unblock Layer 6b:

  1. Confirm Cowork has finished Stages 1-4 of cowork_nla_build.md.
  2. Ensure nla_inference.py is in sys.path (typically the repo root
     of the Cowork build, copied into this tree's PYTHONPATH for the
     Layer 6 / 6b run).
  3. Confirm verbalize_activation signature matches the import below.
  4. Remove the explicit RuntimeError gate at the bottom of main().
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ─────────────────────────────────────────────────────────────────────────────
# Consumer contract — must match Cowork Stage 1 build exactly.
# ─────────────────────────────────────────────────────────────────────────────
#
# The import below will raise ImportError until Cowork's nla_inference.py is
# on the path. That is the gate. Do not catch the exception silently.

try:
    from nla_inference import verbalize_activation  # type: ignore
    _NLA_AVAILABLE = True
    _NLA_IMPORT_ERROR: Optional[str] = None
except ImportError as e:
    verbalize_activation = None  # type: ignore[assignment]
    _NLA_AVAILABLE = False
    _NLA_IMPORT_ERROR = str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

MODEL_NAME = "qwen2.5:7b"

# Layer where the NLA AV operates. Set by the Qwen2.5 NLA checkpoint
# registered in docs/inference.md on the Cowork build box. Cowork will
# overwrite this when wiring the actual checkpoint.
NLA_LAYER_INDEX: Optional[int] = None  # MUST be set before runs.

# Non-collapse sampling rate (Stage 2 of cowork_nla_build.md).
SAMPLE_EVERY_N_TICKS = 5

# Output log per Stage 3.
NLA_LOG_PATH = HERE / "results" / "layer6b" / "nla_collapse_log.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# Log record emit (Stage 3 schema)
# ─────────────────────────────────────────────────────────────────────────────

def emit_nla_record(
    tick: int,
    event_type: str,                       # "collapse" | "sample"
    qoft_fields: Dict[str, Any],
    activation_vector: Any,                # framework-specific; opaque here
    layer_index: int,
    log_path: Path = NLA_LOG_PATH,
) -> Dict[str, Any]:
    """
    Run the NLA call and append one Stage 3 JSONL record. Returns the
    record dict for caller introspection. On NLA call failure, the record
    is still appended with verbalization = "NLA_ERROR: <reason>" (per
    Stage 3: do not drop the record).
    """
    if not _NLA_AVAILABLE:
        raise RuntimeError(
            "layer6b: nla_inference not importable. "
            "Cowork build incomplete. See cowork_nla_build.md."
        )
    if layer_index is None:
        raise RuntimeError(
            "layer6b: NLA_LAYER_INDEX not set. "
            "Wire the Qwen2.5 NLA checkpoint layer index before runs."
        )

    import datetime
    import json

    try:
        verbalization = verbalize_activation(activation_vector, layer_index)
        if not isinstance(verbalization, str) or not verbalization.strip():
            verbalization = "NLA_ERROR: empty verbalization"
    except Exception as e:
        verbalization = f"NLA_ERROR: {type(e).__name__}: {e}"

    record = {
        "tick": tick,
        "event_type": event_type,
        "qoft": qoft_fields,
        "nla": {
            "model": MODEL_NAME,
            "layer": layer_index,
            "verbalization": verbalization,
            "round_trip_score": None,    # populated by analysis tooling
        },
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")

    return record


# ─────────────────────────────────────────────────────────────────────────────
# Entry point gate
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    print("[layer6b] Layer 6b NLA telemetry — STUB.")
    print(f"[layer6b] nla_inference available: {_NLA_AVAILABLE}")
    if not _NLA_AVAILABLE:
        print(
            f"[layer6b] import error: {_NLA_IMPORT_ERROR}",
            file=sys.stderr,
        )
        print(
            "[layer6b] Layer 6b is GATED until Cowork delivers the NLA "
            "telemetry bridge. See cowork_nla_build.md (P1-P5 must pass) "
            "and the consumer contract at the top of this file.",
            file=sys.stderr,
        )
        return 2

    if NLA_LAYER_INDEX is None:
        print(
            "[layer6b] NLA_LAYER_INDEX is None. Wire the Qwen2.5 NLA "
            "checkpoint layer index from docs/inference.md before running.",
            file=sys.stderr,
        )
        return 2

    # When fully wired, this is where the L6/L6b co-run orchestrator lives.
    # It must:
    #  1. Reuse Layer 6's predict_ticks and engine seeds.
    #  2. On every Lambda_psi event in the engine run, call
    #     emit_nla_record(..., event_type="collapse", ...).
    #  3. On every Nth non-collapse tick, call
    #     emit_nla_record(..., event_type="sample", ...).
    #  4. Verify (Stage 4 V1-V4): at least one collapse logged; record
    #     schema valid; log append-only; engine field evolution unchanged
    #     vs a baseline run without the hook.
    raise NotImplementedError(
        "Layer 6b orchestrator not yet implemented. Awaiting Cowork "
        "delivery of nla_inference and the Xi-agent collapse hook, plus "
        "Layer 6's runtime spec (label-semantics ablation, post-P2/P3)."
    )


if __name__ == "__main__":
    sys.exit(main())
