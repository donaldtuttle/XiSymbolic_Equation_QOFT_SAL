# COWORK BUILD — NLA TELEMETRY BRIDGE (Qwen2.5:7b)

**Status:** pending — to be executed by the Cowork agent on the Windows
build box (`C:\Users\donal\OneDrive\Desktop\CoWork\qoft_local_observatory\`).
This file is the canonical text of the Cowork prompt, preserved here so
both sides of the handoff are working from byte-identical instructions.

**Run order context:** Cowork starts this build only AFTER:
1. `ollama pull qwen2.5:7b` completes and `ollama list` confirms the tag.
2. The NLA checkpoint for the Qwen2.5 family is pulled and registered in
   `docs/inference.md`.

**P2 and P3 below are hard go/no-go gates.** Cowork must stop and report
mismatch rather than substitute.

---

## CONTEXT

The Xi-agent runs a canonical QOFT tick loop:
```
Xi(psi) = psi_r (+) Gamma(psi)
```

Lambda_psi is a non-smooth collapse event. We want to capture what
Qwen is internally representing at the moment of collapse — not just
QOFT field telemetry, but the activation-level state.

NLA (Natural Language Autoencoders) translates residual stream
activation vectors into human-readable text. We use it as a passive
observer layer only.

Source files required:
- `nla_inference.py`       (NLA inference — self-contained)
- `docs/inference.md`      (Qwen-specific recipe and gotchas)
- `qoft_xi_agent` (existing Xi-agent build)

---

## PREFLIGHT — HARD REQUIREMENTS

**P1.** Confirm `nla_inference.py` is present and readable.

**P2.** Confirm model is `qwen2.5:7b`.
    Run: `ollama list`
    If model tag is not `qwen2.5:7b` — **STOP. Report mismatch.**
    Do not substitute silently.

**P3.** Confirm NLA checkpoint for Qwen2.5 family exists.
    Check `inference.md` checkpoint table.
    If no Qwen2.5 checkpoint — **STOP. Report missing checkpoint.**

**P4.** Confirm the Xi-agent has an identifiable Lambda_psi firing point
    in the tick loop. If not — STOP and report.

**P5.** Confirm GPU/memory headroom for AV + AR + Xi-agent.
    If uncertain — flag before proceeding.

---

## STAGE 1 — NLA INFERENCE WRAPPER

**Function:**
```
verbalize_activation(activation_vector, layer_index) -> str
```

**Behavior:**
- Takes residual stream activation vector at specified layer
- Passes through Activation Verbalizer (AV) for qwen2.5:7b
- Returns natural language verbalization string
- On failure: raise `RuntimeError` with specific message
- No silent fallback. Hard fail only.
- Read-only. No side effects on model state.

---

## STAGE 2 — COLLAPSE HOOK

At Lambda_psi firing point (before field state updates):

1. Capture QOFT telemetry:
   `tick, phi, rho, chi_xi, phase_state="Lambda_psi", field_vector`

2. Capture activation vector from `qwen2.5:7b` at layer specified
   in `inference.md` for Qwen2.5 checkpoint.

3. Call `verbalize_activation(activation_vector, layer_index)`

4. Log combined record (Stage 3 format)

5. Resume tick normally.

Also capture on non-collapse ticks (1-in-5 sampling):
- Same procedure, `phase_state="tick"`
- This is the control group for Layer 6b analysis.

**HARD RULE:** Hook is passive. Observes and logs only.
Must not alter Xi-step logic, field values, collapse thresholds,
or operator firing in any way.
If instrumentation requires modifying engine logic — STOP.

---

## STAGE 3 — LOG FORMAT

**File:** `nla_collapse_log.jsonl` (append-only)

**Schema per record:**
```json
{
  "tick": int,
  "event_type": "collapse" | "sample",
  "qoft": {
    "phi": float,
    "rho": float,
    "chi_xi": float,
    "phase_state": str,
    "field_norm": float
  },
  "nla": {
    "model": "qwen2.5:7b",
    "layer": int,
    "verbalization": str,
    "round_trip_score": float | null
  },
  "timestamp": "ISO8601"
}
```

On NLA failure: `verbalization = "NLA_ERROR: [reason]"`. Do not drop the
record. Log the error inline.

---

## STAGE 4 — VERIFICATION

Run 50 ticks.

**V1.** Confirm at least one Lambda_psi event produced a log record.
    Zero collapses in 50 ticks = report, not success.

**V2.** Open one collapse record. Confirm:
- QOFT fields non-null and plausible
- `nla.verbalization` is non-empty natural language
- `nla.model = "qwen2.5:7b"`
- `nla.layer` matches Qwen2.5 checkpoint spec

**V3.** Confirm log is append-only across multiple runs.

**V4.** Confirm Xi-agent field evolution is identical to a baseline run
    without NLA hook active. Zero change in field dynamics is required.

---

## HARD CONSTRAINTS

- No silent fallbacks.
- No modifications to Xi-step or any canonical operator.
- NLA observes only. It does not act.
- Wrong model tag = stop, not substitute.
- Missing checkpoint = stop, not substitute.
- Apply same hard-preflight discipline as `llm_backend.py`.

---

## CONTRACT WITH LAYER 6b STUB

The Layer 6b consumer in this tree (`layer6b_nla_telemetry.py`) expects
to import:

```python
from nla_inference import verbalize_activation
```

with the signature documented in Stage 1 above. If Cowork's actual build
ships a different module name or signature, update the L6b stub to match
before unblocking the L6/L6b runs.
