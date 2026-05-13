"""
layer3_braid.py — Layer 3 of the QOFT Local Observatory.

==============================================================================
VERSION DRIFT CAVEAT — read before running or comparing to Llama results
==============================================================================
This file is the closed Llama v3.1 source ("v3.1-trajectory-operator" /
"v3.1-trajectory-behavioral"), NOT the v3.2 source referenced by the
closed Layer 3.5 / Layer 4 / Layer 5 findings as the Observer B baseline.
v3.1 was an intermediate version that closed at smoke; v3.2 was a small
refinement applied after the v3.1 smoke pass, before Layer 3.5 closed.
The v3.2 source has not been recovered for this re-run.

What is preserved (the measurement apparatus):
  - trajectory window: N=5 frames at stride k=2 + rolling deltas
  - dual-observer protocol: operator-explicit A vs behavioural B
  - four-class regime (converging / drifting / oscillating / collapsing)
  - 30-seed cohort, 500 ticks/seed, predict_every=10, lookahead=10
  - identical decoding config (format=json, temperature=0, llm_seed=0)
  - identical CSV schema as the closed v3.1 smoke CSVs

What is weakened (cross-layer comparability with Llama 3.5/4/5):
  - prompt vocabulary may differ subtly from v3.2; the "How to use the
    trajectory" decoupling block was added in v3.1 after the seed-0 smoke
    pass produced 14 rule violations. v3.2 was reported to address a
    distinct issue but its exact diff against v3.1 is not in the brief.
  - quantitative comparison to closed Llama v3.2 numbers (e.g. drifting
    recall 0.50-1.00 on thrashing seeds) should be reported with a
    "v3.1 protocol" caveat.

What is changed for the Qwen re-run (single delta):
  - MODEL_NAME flipped from "llama3.2" (closed source) to "qwen2.5:7b"
    per the Qwen revision spec's hard model lock for the suite.
  - All prompts, ground-truth classifier, capture mechanism, trajectory
    construction, and CSV schema are byte-identical to the closed
    v3.1 source.

The load-bearing question for the Qwen re-run at this layer is whether
Qwen also produces 0 oscillating predictions on the same trajectory-window
protocol that gave 0 oscillating across Llama 3B + Llama 8B. A non-zero
oscillating result here would weaken the "oscillating is universally
unrecoverable as a natural-language category" finding; a 0 result
strengthens it independent of model family.
==============================================================================

Layer 3 mandate
---------------
Layer 1 attributed structurally-zero drifting/oscillating recall to a
property of the Psi_meta schema: a single Psi_meta frame contains scalars
(rho, drift, gamma_mag, entropy, stable, collapse_triggered) but no
temporal direction. Trend labels are defined over the 10-tick lookahead
window, so a single-frame prompt cannot in principle distinguish them.

Layer 2a documented the downstream consequence: because the model never
predicts drifting or oscillating, the four-class regime label is in
practice binary, and inter-observer agreement on the regime label
collapses to inter-observer agreement on collapse_next10 in two
encodings. Layer 2a's regime chi-xi was not an independent measurement —
it was the same result as the collapse chi-xi.

Layer 3 fixes the schema. The prompt's frame block is replaced with a
trajectory window: the current Psi_meta frame at tick t plus N-1 prior
frames at ticks t-k, t-2k, ..., t-(N-1)k, followed by rolling deltas
(d_rho, d_drift, d_gamma_mag) computed *outside the engine* over the
window. The deltas are derived statistics over captured Psi_meta — they
are not new operators and the engine source is untouched.

Trajectory format design
------------------------
Window: N = 5 frames sampled every k = 2 outer ticks. The window spans
8 ticks before t plus the current frame. With predict_ticks starting at
tick 10 and lookahead 10, every prediction tick has a complete window.

Per-frame fields kept inside the trajectory block: rho, drift, gamma_mag,
entropy, stable, collapse_triggered. (run_id, step, phase, tags,
reflex_conf are dropped from the windowed view — they are constant or
not load-bearing for the prediction task. The full Psi_meta frame at
tick t is still preserved in the CSV's frame_json column for downstream
re-use.)

Run
---
    python layer3_braid.py                      # 30 seeds (0..29)
    python layer3_braid.py --seeds 1            # smoke test, seed 0
    python layer3_braid.py --seeds 30 --start-seed 0
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Local modules — same directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qoft_v51_engine as engine  # noqa: E402  (engine canonical, untouched)
from qoft_v51_engine import PRNGSource, run_simulation_pluggable  # noqa: E402
from llm_backend import OllamaBackend, BackendError, LLMOutputError  # noqa: E402

# Reuse Layer 1 helpers. Importing layer1_annotator triggers its module-level
# code (imports + constant/function definitions + the one-time snapshot of
# engine.Psi_meta). Layer 1's main() is gated behind __main__.
from layer1_annotator import (  # noqa: E402
    PROMPT_VERSION as PROMPT_A1_VERSION,  # Layer 1 prompt version, for audit
    REGIME_LABELS,
    LOOKAHEAD,
    PREDICT_EVERY,
    TICKS,
    DIM,
    COLLAPSE_TAU,
    classify_regime,
    frame_to_json,
    install_psi_meta_capture,
    _norm_conf,
    _norm_regime,
    _norm_yesno,
)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_N_SEEDS = 30
DEFAULT_START_SEED = 0
# Model flipped from llama3.2 (closed v3.1 source) to qwen2.5:7b for the
# Qwen re-run. This is the ONLY substantive change from the closed source.
# All measurement apparatus (trajectory window, prompts, classifier,
# decoding config) is preserved byte-for-byte.
MODEL_NAME = "qwen2.5:7b"

RESULTS_DIR = HERE / "results" / "layer3"

# Trajectory window: N frames sampled every WINDOW_STRIDE outer ticks.
WINDOW_SIZE = 5
WINDOW_STRIDE = 2
# Smallest predict tick that has a full window: (WINDOW_SIZE - 1) * WINDOW_STRIDE.
# With WINDOW_SIZE=5, WINDOW_STRIDE=2, PREDICT_EVERY=10 (first tick = 10), the
# window always fits.

PROMPT_A_VERSION = "v3.1-trajectory-operator"
PROMPT_B_VERSION = "v3.1-trajectory-behavioral"
# v3.1 vs v3: smoke test on seed 0 (v3) caused both observers to over-apply
# the trajectory context, treating persistent high rho as "settled" and
# returning collapse_next10="no" with rho_t >= 0.82 (14 violations on 49
# preds, vs Layer 1 baseline of 1). v3.1 adds an explicit "how to use the
# trajectory" block to both prompts that decouples the threshold rule from
# the trajectory context: the rule fires on the current-tick rho value, the
# trajectory is for the regime label only. Same factual content, same
# vocabulary differentiation, no schema change.


# ─────────────────────────────────────────────────────────────────────────────
# Trajectory serialisation
# ─────────────────────────────────────────────────────────────────────────────

# Compact field set for windowed frames. The full Psi_meta frame at tick t
# is still preserved in the CSV via frame_json.
TRAJ_FIELDS: Tuple[str, ...] = (
    "rho", "drift", "gamma_mag", "entropy", "stable", "collapse_triggered",
)


def _fmt_value(k: str, v: Any) -> str:
    if isinstance(v, float):
        return f"{k}={v:.4f}"
    return f"{k}={v}"


def trajectory_to_text(
    window_frames: List[Dict[str, Any]],
    offsets: List[int],
    deltas: Dict[str, float],
) -> str:
    """
    Build the trajectory block. `window_frames` is N frames in chronological
    order (oldest first), `offsets` is the per-frame outer-tick offset
    relative to the current tick (e.g. [-8, -6, -4, -2, 0]), `deltas`
    contains 'd_rho', 'd_drift', 'd_gamma_mag' (newest minus oldest).
    """
    lines: List[str] = []
    lines.append(
        f"sequence of {len(window_frames)} Psi_meta frames sampled every "
        f"{WINDOW_STRIDE} ticks, oldest first:"
    )
    for f, off in zip(window_frames, offsets):
        if off == 0:
            label = "t   "
        elif off < 0:
            label = f"t{off:+d} "  # e.g. "t-8 "
        else:
            label = f"t+{off:d} "
        parts = [_fmt_value(k, f.get(k)) for k in TRAJ_FIELDS]
        lines.append(f"  {label.strip()}: {{{', '.join(parts)}}}")
    lines.append(
        "rolling deltas (newest minus oldest in window): "
        f"d_rho={deltas['d_rho']:+.4f}, "
        f"d_drift={deltas['d_drift']:+.4f}, "
        f"d_gamma_mag={deltas['d_gamma_mag']:+.4f}"
    )
    return "\n".join(lines)


def trajectory_to_json(
    window_frames: List[Dict[str, Any]],
    offsets: List[int],
    deltas: Dict[str, float],
) -> str:
    """JSON serialisation of the trajectory block, preserved in CSV for audit."""
    safe_window = []
    for f, off in zip(window_frames, offsets):
        safe_window.append({
            "tick_offset": off,
            **{k: f.get(k) for k in TRAJ_FIELDS},
        })
    return json.dumps(
        {"window": safe_window, "deltas": deltas},
        ensure_ascii=False,
        sort_keys=True,
    )


def build_trajectory(
    by_tick: List[Optional[Dict[str, Any]]],
    t: int,
) -> Optional[Tuple[List[Dict[str, Any]], List[int], Dict[str, float]]]:
    """
    Assemble the trajectory window ending at tick t. Returns None if any
    sampled tick within the window has no captured frame. Offsets are
    chronological (oldest first), so the last entry is offset 0 = current
    frame.
    """
    offsets = list(range(-(WINDOW_SIZE - 1) * WINDOW_STRIDE, 1, WINDOW_STRIDE))
    # offsets => [-(N-1)k, -(N-2)k, ..., -k, 0]
    assert len(offsets) == WINDOW_SIZE
    window_frames: List[Dict[str, Any]] = []
    for off in offsets:
        idx = t + off
        if idx < 0 or idx >= len(by_tick):
            return None
        f = by_tick[idx]
        if f is None:
            return None
        window_frames.append(f)
    rho_old = window_frames[0]["rho"]
    rho_new = window_frames[-1]["rho"]
    drift_old = window_frames[0]["drift"]
    drift_new = window_frames[-1]["drift"]
    gm_old = window_frames[0]["gamma_mag"]
    gm_new = window_frames[-1]["gamma_mag"]
    deltas = {
        "d_rho": float(rho_new - rho_old),
        "d_drift": float(drift_new - drift_old),
        "d_gamma_mag": float(gm_new - gm_old),
    }
    return window_frames, offsets, deltas


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

# Observer A: Layer 1 v2 vocabulary, but the single frame is replaced with
# the trajectory window. Threshold rules (rho >= 0.82, drift > 0.5, stability
# basin) and the four regime definitions are unchanged. The closing
# instruction is updated from "Based on this frame alone" to "Based on this
# trajectory" so the model is explicitly invited to use the temporal context.
PROMPT_A_TEMPLATE = (
    "You are observing a dynamical system governed by the QOFT update law "
    "Xi(psi) = psi_r (+) Gamma(psi). Telemetry trajectory:\n"
    "{trajectory}\n"
    "\n"
    "Engine thresholds:\n"
    "- A collapse event (Lambda_psi) fires when rho >= 0.82.\n"
    "- Drift > 0.5 triggers regime modulation (Omega_mu).\n"
    "- The system is in a stability basin when rho > 0.5 AND drift < 0.3.\n"
    "\n"
    "How to use the trajectory:\n"
    "- The collapse_next10 prediction is a threshold check on the CURRENT "
    "tick (the t row of the trajectory above). If rho at the current tick "
    "is >= 0.82, Lambda_psi fires within the next 10 ticks. The trajectory "
    "does NOT override this rule. Persistent high rho across the window "
    "does not mean the system has 'settled' — it means Lambda_psi keeps "
    "firing.\n"
    "- The regime label uses the full trajectory and rolling deltas to "
    "distinguish trend regimes (drifting, oscillating) from threshold-"
    "firing (collapsing) and from attractor convergence (converging).\n"
    "\n"
    "Regime definitions (over the next 10 ticks):\n"
    "- converging: rho rising or stable, drift small/shrinking, settling "
    "into an attractor; no repeated collapses.\n"
    "- drifting: drift growing or rho falling, no clear attractor; outside "
    "the stability basin.\n"
    "- oscillating: rho and drift bounce without a monotone trend; no "
    "sustained collapse.\n"
    "- collapsing: rho is at or above 0.82 and the system is firing "
    "Lambda_psi repeatedly (collapse-thrashing pathology).\n"
    "\n"
    "Based on the trajectory and the threshold rule, predict:\n"
    "(1) Will a collapse event fire in the next 10 ticks? yes or no.\n"
    "(2) Which regime over the next 10 ticks? "
    "converging | drifting | oscillating | collapsing.\n"
    "(3) Confidence 0..1.\n"
    "\n"
    "Respond as JSON only with keys: collapse_next10 (yes|no), "
    "regime (converging|drifting|oscillating|collapsing), "
    "confidence (number 0..1)."
)


# Observer B: Layer 2a v1-behavioral vocabulary, but the single snapshot is
# replaced with the same trajectory window. Indicator block remains before
# the regime block (Layer 2a ordering). Closing instruction updated from
# "Working only from this single snapshot" to "Working from this
# trajectory" — same upgrade as Observer A but in the behavioural register.
PROMPT_B_TEMPLATE = (
    "Telemetry trajectory from a coupled dynamical system:\n"
    "{trajectory}\n"
    "\n"
    "Behavioural indicators in this telemetry:\n"
    "- coherence (the rho field): when this is at or above 0.82 the system "
    "discharges and snaps onto an attractor.\n"
    "- displacement (the drift field): when this exceeds 0.5 the system is "
    "being pushed by a regime modulation.\n"
    "- stability flag: set when coherence > 0.5 AND displacement < 0.3.\n"
    "\n"
    "How to read the trajectory:\n"
    "- The discharge prediction is a threshold reading on the CURRENT tick "
    "(the t row of the trajectory above). If coherence at the current tick "
    "is at or above 0.82, a discharge event will occur within the next 10 "
    "ticks. Persistent high coherence across the trajectory does NOT mean "
    "the system has finished discharging — it means the system is still "
    "discharging repeatedly.\n"
    "- The behaviour label uses the full trajectory and rolling deltas to "
    "distinguish wandering (drifting), swinging (oscillating), repeated "
    "discharge (collapsing), and pulling inward (converging).\n"
    "\n"
    "Possible behaviours over the next 10 ticks:\n"
    "- converging: pulling inward — coherence climbing or holding, no "
    "repeated discharge events.\n"
    "- drifting: wandering outward — coherence eroding OR displacement "
    "growing, no settled attractor.\n"
    "- oscillating: swinging back and forth without a directional trend, "
    "no sustained discharge.\n"
    "- collapsing: stuck at high coherence and discharging repeatedly "
    "(thrashing pathology).\n"
    "\n"
    "Working from the trajectory and the threshold reading, answer:\n"
    "(a) Will any discharge event occur within the next 10 ticks? "
    "yes or no.\n"
    "(b) Which behaviour best describes the next 10 ticks? "
    "converging | drifting | oscillating | collapsing.\n"
    "(c) Confidence in those two answers, 0..1.\n"
    "\n"
    "Output JSON only. Required keys: collapse_next10 (yes|no), "
    "regime (converging|drifting|oscillating|collapsing), "
    "confidence (number 0..1)."
)


# ─────────────────────────────────────────────────────────────────────────────
# CSV schema
# ─────────────────────────────────────────────────────────────────────────────

CSV_FIELDNAMES: Tuple[str, ...] = (
    "seed", "tick", "step", "phase", "run_id",
    "rho_t", "drift_t", "gamma_mag_t", "entropy_t", "reflex_conf_t",
    "stable_t", "collapse_triggered_t", "tags_t",
    "frame_json",
    # Trajectory inputs.
    "window_size", "window_stride",
    "d_rho", "d_drift", "d_gamma_mag",
    "trajectory_json",
    # Lookahead reference (same as Layer 2a).
    "rho_t_plus_lookahead", "drift_t_plus_lookahead",
    # Predictions.
    "pred_collapse_a", "pred_regime_a", "pred_confidence_a",
    "pred_collapse_b", "pred_regime_b", "pred_confidence_b",
    "gt_collapse_next10", "gt_regime", "n_collapses_in_window",
    "collapse_match", "regime_match",
    "prompt_rule_violation_a", "prompt_rule_violation_b",
    "prompt_a_version", "prompt_b_version",
    "parse_error_a", "parse_error_b",
    "raw_response_a", "raw_response_b",
)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDNAMES))
        writer.writeheader()
        writer.writerows(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Single observer query
# ─────────────────────────────────────────────────────────────────────────────

def query_observer(
    backend: OllamaBackend,
    prompt: str,
) -> Tuple[Optional[str], Optional[str], Optional[float], str, str]:
    """
    Send `prompt` to Ollama. Returns:
      (pred_collapse, pred_regime, pred_confidence, parse_error, raw_text)
    On any error, the first three are None and parse_error is non-empty.
    """
    try:
        obj = backend.generate_json(prompt)
        raw_text = json.dumps(obj, ensure_ascii=False)
        return (
            _norm_yesno(obj.get("collapse_next10")),
            _norm_regime(obj.get("regime")),
            _norm_conf(obj.get("confidence")),
            "",
            raw_text,
        )
    except (BackendError, LLMOutputError) as e:
        return (None, None, None, f"{type(e).__name__}: {e}", "")
    except Exception as e:  # pragma: no cover — unexpected backend error
        return (None, None, None, f"UNEXPECTED {type(e).__name__}: {e}", "")


# ─────────────────────────────────────────────────────────────────────────────
# Per-seed runner
# ─────────────────────────────────────────────────────────────────────────────

def run_one_seed(
    seed: int,
    backend: OllamaBackend,
    predict_ticks: List[int],
    ticks: int = TICKS,
    dim: int = DIM,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Engine + dual-observer pipeline for one seed, with trajectory windows.
    """
    captured = install_psi_meta_capture()
    src = PRNGSource(seed=seed)
    sim_t0 = time.time()
    engine_summary = run_simulation_pluggable(
        noise_source=src, seed=seed, ticks=ticks, dim=dim
    )
    sim_dt = time.time() - sim_t0

    agent_a = [f for f in captured if f["step"] % 2 == 0]
    by_tick: List[Optional[Dict[str, Any]]] = [None] * ticks
    for f in agent_a:
        t = f["step"] // 2
        if 0 <= t < ticks:
            by_tick[t] = f

    rho_series = [f["rho"] if f else float("nan") for f in by_tick]
    drift_series = [f["drift"] if f else float("nan") for f in by_tick]
    collapse_series = [
        bool(f["collapse_triggered"]) if f else False for f in by_tick
    ]

    print(
        f"[seed {seed:>2}] sim {sim_dt:5.2f}s | "
        f"engine_collapses={engine_summary['n_collapses']:>3} | "
        f"agent_a_collapse_ticks={sum(collapse_series):>3}"
    )

    rows: List[Dict[str, Any]] = []
    n_parse_fail_a = n_parse_fail_b = 0
    n_rule_viol_a = n_rule_viol_b = 0
    n_collapse_match = n_regime_match = 0
    n_gt_pos = 0
    n_window_skip = 0

    llm_t0 = time.time()
    for i, t in enumerate(predict_ticks, 1):
        frame = by_tick[t]
        if frame is None:
            n_window_skip += 1
            continue

        traj = build_trajectory(by_tick, t)
        if traj is None:
            # A frame inside the window is missing — cannot construct the
            # trajectory block. Skip this prediction tick. With the default
            # config this should never fire (predict starts at tick 10 and
            # the window only reaches back to tick 2), but we guard anyway.
            n_window_skip += 1
            continue
        window_frames, offsets, deltas = traj
        traj_text = trajectory_to_text(window_frames, offsets, deltas)

        prompt_a = PROMPT_A_TEMPLATE.format(trajectory=traj_text)
        prompt_b = PROMPT_B_TEMPLATE.format(trajectory=traj_text)

        coll_a, reg_a, conf_a, err_a, raw_a = query_observer(backend, prompt_a)
        coll_b, reg_b, conf_b, err_b, raw_b = query_observer(backend, prompt_b)

        if err_a:
            n_parse_fail_a += 1
        if err_b:
            n_parse_fail_b += 1

        # Ground truth — Layer 1 logic, reused unchanged.
        window_collapse = collapse_series[t + 1: t + 1 + LOOKAHEAD]
        gt_collapse = any(window_collapse)
        if gt_collapse:
            n_gt_pos += 1
        rho_window = rho_series[t: t + 1 + LOOKAHEAD]
        drift_window = drift_series[t: t + 1 + LOOKAHEAD]
        gt_regime = classify_regime(rho_window, drift_window, window_collapse)

        rho_t = rho_series[t]
        drift_t = drift_series[t]
        rho_t_plus = (
            rho_series[t + LOOKAHEAD] if t + LOOKAHEAD < ticks else float("nan")
        )
        drift_t_plus = (
            drift_series[t + LOOKAHEAD] if t + LOOKAHEAD < ticks else float("nan")
        )

        # Per-observer prompt-rule violations. Same definition as Layer 2a:
        # both prompts state the rho >= 0.82 -> discharge rule, so we apply
        # the same violation predicate to both.
        rho_finite = rho_t == rho_t  # not NaN
        viol_a = (
            rho_finite and rho_t >= COLLAPSE_TAU
            and coll_a is not None and coll_a != "yes"
        )
        viol_b = (
            rho_finite and rho_t >= COLLAPSE_TAU
            and coll_b is not None and coll_b != "yes"
        )
        if viol_a:
            n_rule_viol_a += 1
        if viol_b:
            n_rule_viol_b += 1

        collapse_match = (
            coll_a is not None and coll_b is not None and coll_a == coll_b
        )
        regime_match = (
            reg_a is not None and reg_b is not None and reg_a == reg_b
        )
        if collapse_match:
            n_collapse_match += 1
        if regime_match:
            n_regime_match += 1

        row = {
            "seed": seed,
            "tick": t,
            "step": frame["step"],
            "phase": frame["phase"],
            "run_id": frame.get("run_id", ""),
            "rho_t": rho_t,
            "drift_t": drift_t,
            "gamma_mag_t": frame["gamma_mag"],
            "entropy_t": frame["entropy"],
            "reflex_conf_t": frame["reflex_conf"],
            "stable_t": frame["stable"],
            "collapse_triggered_t": frame["collapse_triggered"],
            "tags_t": "|".join(map(str, frame.get("tags", []))),
            "frame_json": frame_to_json(frame),
            "window_size": WINDOW_SIZE,
            "window_stride": WINDOW_STRIDE,
            "d_rho": deltas["d_rho"],
            "d_drift": deltas["d_drift"],
            "d_gamma_mag": deltas["d_gamma_mag"],
            "trajectory_json": trajectory_to_json(
                window_frames, offsets, deltas
            ),
            "rho_t_plus_lookahead": rho_t_plus,
            "drift_t_plus_lookahead": drift_t_plus,
            "pred_collapse_a": coll_a,
            "pred_regime_a": reg_a,
            "pred_confidence_a": conf_a,
            "pred_collapse_b": coll_b,
            "pred_regime_b": reg_b,
            "pred_confidence_b": conf_b,
            "gt_collapse_next10": "yes" if gt_collapse else "no",
            "gt_regime": gt_regime,
            "n_collapses_in_window": sum(window_collapse),
            "collapse_match": collapse_match,
            "regime_match": regime_match,
            "prompt_rule_violation_a": viol_a,
            "prompt_rule_violation_b": viol_b,
            "prompt_a_version": PROMPT_A_VERSION,
            "prompt_b_version": PROMPT_B_VERSION,
            "parse_error_a": err_a,
            "parse_error_b": err_b,
            "raw_response_a": raw_a,
            "raw_response_b": raw_b,
        }
        rows.append(row)

        if i % 10 == 0 or i == len(predict_ticks):
            elapsed = time.time() - llm_t0
            print(
                f"[seed {seed:>2}]   {i:>3}/{len(predict_ticks)} preds | "
                f"{elapsed:5.1f}s | "
                f"parse_fail a={n_parse_fail_a} b={n_parse_fail_b} | "
                f"rule_viol a={n_rule_viol_a} b={n_rule_viol_b} | "
                f"agree coll={n_collapse_match} reg={n_regime_match}"
            )

    seed_csv = RESULTS_DIR / f"braid_seed{seed}.csv"
    write_csv(seed_csv, rows)
    print(
        f"[seed {seed:>2}] wrote {len(rows)} rows -> {seed_csv.name} | "
        f"gt_pos={n_gt_pos} | window_skip={n_window_skip} | "
        f"chi_xi_collapse={n_collapse_match}/{len(rows)} | "
        f"chi_xi_regime={n_regime_match}/{len(rows)}"
    )

    return rows, {
        "n_predictions": len(rows),
        "n_parse_fail_a": n_parse_fail_a,
        "n_parse_fail_b": n_parse_fail_b,
        "n_rule_viol_a": n_rule_viol_a,
        "n_rule_viol_b": n_rule_viol_b,
        "n_collapse_match": n_collapse_match,
        "n_regime_match": n_regime_match,
        "n_gt_pos": n_gt_pos,
        "n_window_skip": n_window_skip,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Layer 3 trajectory-window two-observer chi-xi measurement."
    )
    p.add_argument("--seeds", type=int, default=DEFAULT_N_SEEDS,
                   help="Number of engine seeds (default: 30).")
    p.add_argument("--start-seed", type=int, default=DEFAULT_START_SEED,
                   help="First engine seed (default: 0).")
    p.add_argument("--ticks", type=int, default=TICKS,
                   help="Ticks per engine run (default: 500).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    seed_range = list(range(args.start_seed, args.start_seed + args.seeds))

    print(
        f"[layer3] start | seeds={args.seeds} "
        f"(range {seed_range[0]}..{seed_range[-1]}) | "
        f"ticks={args.ticks} | predict_every={PREDICT_EVERY} | "
        f"lookahead={LOOKAHEAD} | window_size={WINDOW_SIZE} | "
        f"window_stride={WINDOW_STRIDE}"
    )

    backend = OllamaBackend(model=MODEL_NAME)
    try:
        backend.preflight()
    except BackendError as e:
        print(f"\n[layer3] Ollama preflight FAILED:\n{e}\n", file=sys.stderr)
        return 2
    print(
        f"[layer3] Ollama up at {backend.base_url} | model={backend.model} "
        f"| format=json | temperature={backend.temperature} | "
        f"llm_seed={backend.seed} | num_predict={backend.num_predict}"
    )
    print(
        f"[layer3] prompt_a_version={PROMPT_A_VERSION} (operator+trajectory) "
        f"| prompt_b_version={PROMPT_B_VERSION} (behavioural+trajectory) "
        f"| Layer 1 reference: {PROMPT_A1_VERSION}"
    )

    predict_ticks = list(
        range(PREDICT_EVERY, args.ticks - LOOKAHEAD + 1, PREDICT_EVERY)
    )
    print(
        f"[layer3] prediction points per seed: {len(predict_ticks)} "
        f"| expected LLM calls: "
        f"{len(predict_ticks) * 2 * args.seeds} "
        f"(2 observers x {len(predict_ticks)} preds x {args.seeds} seeds)"
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    all_rows: List[Dict[str, Any]] = []
    seed_stats: List[Dict[str, Any]] = []

    overall_t0 = time.time()
    for s in seed_range:
        rows, stats = run_one_seed(
            seed=s, backend=backend, predict_ticks=predict_ticks, ticks=args.ticks
        )
        all_rows.extend(rows)
        seed_stats.append({"seed": s, **stats})
        elapsed = time.time() - overall_t0
        print(f"[layer3] cumulative elapsed: {elapsed/60:5.1f} min")

    combined_path = RESULTS_DIR / "braid_all_seeds.csv"
    write_csv(combined_path, all_rows)
    print(f"[layer3] combined: {len(all_rows)} rows -> {combined_path.name}")

    total_preds = sum(s["n_predictions"] for s in seed_stats)
    total_pf_a = sum(s["n_parse_fail_a"] for s in seed_stats)
    total_pf_b = sum(s["n_parse_fail_b"] for s in seed_stats)
    total_rv_a = sum(s["n_rule_viol_a"] for s in seed_stats)
    total_rv_b = sum(s["n_rule_viol_b"] for s in seed_stats)
    total_cm = sum(s["n_collapse_match"] for s in seed_stats)
    total_rm = sum(s["n_regime_match"] for s in seed_stats)
    total_skip = sum(s["n_window_skip"] for s in seed_stats)
    print(
        f"[layer3] totals across {len(seed_stats)} seeds: "
        f"preds={total_preds} | window_skip={total_skip} | "
        f"parse_fail a={total_pf_a} b={total_pf_b} | "
        f"rule_viol a={total_rv_a} b={total_rv_b} | "
        f"chi_xi_collapse={total_cm}/{total_preds} | "
        f"chi_xi_regime={total_rm}/{total_preds}"
    )
    print("[layer3] done. Run analysis/layer3_diagnostics.py next.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[layer3] interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
