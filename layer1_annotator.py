"""
layer1_annotator.py — LEGACY closed Llama Layer 1 source. NOT part of the
Qwen re-run runtime path. Preserved here for one purpose only: to provide
import targets for layer3_braid.py (the closed v3.1 trajectory-window
braid), which depends on the following symbols from this module:

  PROMPT_TEMPLATE  PROMPT_VERSION  REGIME_LABELS
  LOOKAHEAD  PREDICT_EVERY  TICKS  DIM  COLLAPSE_TAU
  classify_regime  frame_to_json  frame_to_text
  install_psi_meta_capture
  _norm_yesno  _norm_regime  _norm_conf

The pure-function helpers above carry no Llama-specific state. They are
the v2 single-frame Layer 1 annotator's response normalizers and frame
serialisers, kept verbatim from the closed Llama L1 source.

If you want to *execute* this file directly (rather than import it),
construct OllamaBackend(model="llama3.2") explicitly — the bare
OllamaBackend() will pick up DEFAULT_MODEL=qwen2.5:7b from llm_backend
under the Qwen re-run config, which will silently change the
experiment. The closed Llama L1 results CSV referenced by the Layer 3.5
/ Layer 4 / Layer 5 brief was generated against llama3.2:3b; do not
overwrite it.

For the runnable Layer 1 of the Qwen re-run, see layer1_qwen.py.

Original docstring follows:

==============================================================================

Layer 1 of the QOFT Local Observatory.

Pipeline
--------
1) Preflight Ollama (hard-fail on unreachable server / missing model).
   Logs the decoding config (format=json, temperature=0, seed=0) so the
   reproducibility claim is visible in the run banner.
2) For each engine seed in the configured range:
   a) Capture every Psi_meta frame the engine emits during a 500-tick run
      with PRNGSource(seed=S). Captured by monkey-patching
      `qoft_v51_engine.Psi_meta` at the module level — the engine source is
      NOT modified. The wrapper always delegates to the *true original*
      Psi_meta (captured once at import time), so re-installing it for each
      seed never builds wrapper chains.
   b) Filter to agent-A frames (engine convention: even step values).
   c) For each prediction tick (every 10th tick: 10, 20, ..., 490), pass the
      compact frame string to the local LLM and ask for a JSON prediction.
   d) Compute ground-truth labels from the captured series:
        - collapse_in_next10: any collapse_triggered in ticks t+1 .. t+10
        - regime_truth      : from rho/drift/collapse over t..t+10
                              ("collapsing" if >=3 collapses in window)
   e) Track prompt-rule violations: rho_t >= 0.82 AND pred_collapse != "yes"
      — the prompt explicitly tells the model rho>=0.82 fires Lambda_psi, so
      a "no" prediction at that rho is a measurable inconsistency.
   f) Write results/layer1/predictions_seed{S}.csv.
3) Write the combined CSV results/layer1/predictions_all_seeds.csv.

CSV schema (per row)
--------------------
seed, tick, step, phase, run_id,
rho_t, drift_t, gamma_mag_t, entropy_t, reflex_conf_t, stable_t,
collapse_triggered_t, tags_t, frame_json,
rho_t_plus_lookahead, drift_t_plus_lookahead,
pred_collapse, pred_regime, pred_confidence,
gt_collapse_next10, gt_regime, n_collapses_in_window,
prompt_rule_violation, prompt_version,
parse_error, raw_response

`frame_json` is the entire Psi_meta frame as a JSON string, so Layer 2 can
retrofit different prompts against the same prediction points without
re-running the engine.

Run
---
    python layer1_annotator.py                      # 30 seeds (0..29)
    python layer1_annotator.py --seeds 1            # smoke test, seed 0
    python layer1_annotator.py --seeds 30 --start-seed 0
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Local modules — same directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qoft_v51_engine as engine  # noqa: E402  (engine is canonical, untouched)
from qoft_v51_engine import PRNGSource, run_simulation_pluggable  # noqa: E402
from llm_backend import OllamaBackend, BackendError, LLMOutputError  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_N_SEEDS = 30
DEFAULT_START_SEED = 0
TICKS = 500
DIM = 8
PREDICT_EVERY = 10                 # predict every Nth outer tick
LOOKAHEAD = 10                     # ground-truth window in outer ticks
COLLAPSE_TAU = 0.82                # canonical engine threshold (config["collapse_tau"])

PROMPT_VERSION = "v2"

RESULTS_DIR = HERE / "results" / "layer1"

# Field set we serialise into the prompt (from canonical Psi_meta contract).
FRAME_FIELDS: Tuple[str, ...] = (
    "run_id", "step", "phase", "rho", "gamma_mag",
    "reflex_conf", "entropy", "drift", "stable",
    "collapse_triggered", "tags",
)

REGIME_LABELS = ("converging", "drifting", "oscillating", "collapsing")

# Prompt grounded in the QOFT/QOSMOS v1 spec. Numerical thresholds come from
# the canonical engine config (collapse_tau=0.82, omega_drift_threshold=0.5)
# and the Psi_meta "stable" predicate (rho>0.5 AND drift<0.3). The fourth
# regime label "collapsing" maps to the spec's named failure mode
# "collapse thrashing" (Section 4.7) / border-collision bifurcations
# (Section 6.10) — Lambda_psi firing repeatedly within the window.
PROMPT_TEMPLATE = (
    "You are observing a dynamical system governed by the QOFT update law "
    "Xi(psi) = psi_r (+) Gamma(psi). Telemetry frame: {frame}.\n"
    "\n"
    "Engine thresholds:\n"
    "- A collapse event (Lambda_psi) fires when rho >= 0.82.\n"
    "- Drift > 0.5 triggers regime modulation (Omega_mu).\n"
    "- The system is in a stability basin when rho > 0.5 AND drift < 0.3.\n"
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
    "Based on this frame alone, predict:\n"
    "(1) Will a collapse event fire in the next 10 ticks? yes or no.\n"
    "(2) Which regime over the next 10 ticks? "
    "converging | drifting | oscillating | collapsing.\n"
    "(3) Confidence 0..1.\n"
    "\n"
    "Respond as JSON only with keys: collapse_next10 (yes|no), "
    "regime (converging|drifting|oscillating|collapsing), "
    "confidence (number 0..1)."
)


# ─────────────────────────────────────────────────────────────────────────────
# Frame capture via non-invasive monkey-patch
# ─────────────────────────────────────────────────────────────────────────────

# Snapshot the *true* canonical Psi_meta once at import time. Re-installing the
# capture wrapper for each seed always references this, so we never build
# nested wrapper chains across runs.
_TRUE_ORIGINAL_PSI_META = engine.Psi_meta


def install_psi_meta_capture() -> List[Dict[str, Any]]:
    """
    Replace `engine.Psi_meta` with a fresh wrapper bound to the canonical
    Psi_meta. Returns the per-call capture buffer. The wrapper appends the
    returned frame dict by reference, so the engine's later in-place mutation
    of `collapse_triggered` is preserved.
    """
    buffer: List[Dict[str, Any]] = []

    def wrapped(psi_latent, gamma, rho, psi_r, ctx):
        frame = _TRUE_ORIGINAL_PSI_META(psi_latent, gamma, rho, psi_r, ctx)
        buffer.append(frame)
        return frame

    engine.Psi_meta = wrapped
    return buffer


# ─────────────────────────────────────────────────────────────────────────────
# Frame serialisation
# ─────────────────────────────────────────────────────────────────────────────

def frame_to_text(frame: Dict[str, Any]) -> str:
    """Compact, deterministic, human-readable frame block for the prompt."""
    parts = []
    for k in FRAME_FIELDS:
        v = frame.get(k)
        if isinstance(v, float):
            parts.append(f"{k}={v:.4f}")
        else:
            parts.append(f"{k}={v}")
    return "{" + ", ".join(parts) + "}"


def frame_to_json(frame: Dict[str, Any]) -> str:
    """
    Full Psi_meta frame as a stable JSON string. Used for Layer 2
    retrofitting: alternative prompts can be evaluated against the same
    prediction points without re-running the engine.
    """
    safe = {}
    for k, v in frame.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, (list, tuple)):
            safe[k] = list(v)
        else:
            safe[k] = repr(v)
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


# ─────────────────────────────────────────────────────────────────────────────
# Ground truth from captured series
# ─────────────────────────────────────────────────────────────────────────────

COLLAPSING_MIN_EVENTS = 3  # >=3 collapses in a 10-tick window = thrashing


def classify_regime(
    rho_window: List[float],
    drift_window: List[float],
    collapse_window: List[bool],
) -> str:
    """
    Heuristic ground truth over a lookahead window of length L (>= 2).

    Labels grounded in QOFT/QOSMOS v1 spec:
      collapsing : Lambda_psi fires repeatedly within the window (>=3 events).
                   Maps to the named failure mode "collapse thrashing"
                   (Section 4.7) / border-collision bifurcation (Section 6.10).
                   Checked FIRST — it dominates other trend signals.
      converging : inside the Sigma_o stability basin — drift small and
                   trending down OR rho high and stable, no thrashing.
      drifting   : outside the stability basin — drift trending up OR rho
                   falling.
      oscillating: high relative variance, non-monotonic, no thrashing.
    """
    if len(rho_window) < 2 or len(drift_window) < 2:
        return "converging"

    if collapse_window and sum(collapse_window) >= COLLAPSING_MIN_EVENTS:
        return "collapsing"

    rho_start, rho_end = rho_window[0], rho_window[-1]
    drift_start, drift_end = drift_window[0], drift_window[-1]
    rho_delta = rho_end - rho_start
    drift_delta = drift_end - drift_start

    rho_mean = sum(rho_window) / len(rho_window)
    drift_mean = sum(drift_window) / len(drift_window)
    rho_var = _variance(rho_window)
    drift_var = _variance(drift_window)
    rho_cv = (rho_var ** 0.5) / max(abs(rho_mean), 1e-6)
    drift_cv = (drift_var ** 0.5) / max(abs(drift_mean), 1e-6)

    monotonic_rho = _is_monotonic(rho_window)
    monotonic_drift = _is_monotonic(drift_window)
    if (rho_cv > 0.15 or drift_cv > 0.30) and not (monotonic_rho or monotonic_drift):
        return "oscillating"

    if drift_delta > 0.05 or rho_delta < -0.05:
        return "drifting"

    return "converging"


def _variance(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _is_monotonic(xs: List[float]) -> bool:
    if len(xs) < 3:
        return True
    diffs = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    return pos == 0 or neg == 0 or abs(pos - neg) >= len(diffs) - 1


# ─────────────────────────────────────────────────────────────────────────────
# CSV schema
# ─────────────────────────────────────────────────────────────────────────────

CSV_FIELDNAMES: Tuple[str, ...] = (
    "seed", "tick", "step", "phase", "run_id",
    "rho_t", "drift_t", "gamma_mag_t", "entropy_t", "reflex_conf_t",
    "stable_t", "collapse_triggered_t", "tags_t",
    "frame_json",
    "rho_t_plus_lookahead", "drift_t_plus_lookahead",
    "pred_collapse", "pred_regime", "pred_confidence",
    "gt_collapse_next10", "gt_regime", "n_collapses_in_window",
    "prompt_rule_violation", "prompt_version",
    "parse_error", "raw_response",
)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDNAMES))
        writer.writeheader()
        writer.writerows(rows)


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
    Run the engine + LLM annotation pipeline for a single engine seed.
    Returns (rows, stats) where stats has counts for parse failures,
    prompt-rule violations, and ground-truth positives.
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
    n_parse_fail = 0
    n_rule_violation = 0
    n_gt_pos = 0

    llm_t0 = time.time()
    for i, t in enumerate(predict_ticks, 1):
        frame = by_tick[t]
        if frame is None:
            continue

        prompt = PROMPT_TEMPLATE.format(frame=frame_to_text(frame))

        pred_collapse_raw: Optional[str] = None
        pred_regime_raw: Optional[str] = None
        pred_conf_raw: Optional[float] = None
        parse_error: str = ""
        raw_text: str = ""

        try:
            obj = backend.generate_json(prompt)
            raw_text = json.dumps(obj, ensure_ascii=False)
            pred_collapse_raw = _norm_yesno(obj.get("collapse_next10"))
            pred_regime_raw = _norm_regime(obj.get("regime"))
            pred_conf_raw = _norm_conf(obj.get("confidence"))
        except (BackendError, LLMOutputError) as e:
            parse_error = f"{type(e).__name__}: {e}"
            n_parse_fail += 1
        except Exception as e:
            parse_error = f"UNEXPECTED {type(e).__name__}: {e}"
            n_parse_fail += 1

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

        # Prompt-rule violation: prompt explicitly tells the model that
        # rho>=0.82 fires Lambda_psi. If rho_t crosses that threshold and the
        # model still says "no" to collapse_next10, that's a measurable
        # inconsistency between prompt and prediction.
        rule_violation = (
            rho_t == rho_t  # not nan
            and rho_t >= COLLAPSE_TAU
            and pred_collapse_raw is not None
            and pred_collapse_raw != "yes"
        )
        if rule_violation:
            n_rule_violation += 1

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
            "rho_t_plus_lookahead": rho_t_plus,
            "drift_t_plus_lookahead": drift_t_plus,
            "pred_collapse": pred_collapse_raw,
            "pred_regime": pred_regime_raw,
            "pred_confidence": pred_conf_raw,
            "gt_collapse_next10": "yes" if gt_collapse else "no",
            "gt_regime": gt_regime,
            "n_collapses_in_window": sum(window_collapse),
            "prompt_rule_violation": rule_violation,
            "prompt_version": PROMPT_VERSION,
            "parse_error": parse_error,
            "raw_response": raw_text,
        }
        rows.append(row)

        if i % 10 == 0 or i == len(predict_ticks):
            elapsed = time.time() - llm_t0
            print(
                f"[seed {seed:>2}]   {i:>3}/{len(predict_ticks)} preds | "
                f"{elapsed:5.1f}s | parse_fail={n_parse_fail} | "
                f"rule_viol={n_rule_violation}"
            )

    # Per-seed CSV.
    seed_csv = RESULTS_DIR / f"predictions_seed{seed}.csv"
    write_csv(seed_csv, rows)
    print(
        f"[seed {seed:>2}] wrote {len(rows)} rows -> {seed_csv.name} | "
        f"gt_pos={n_gt_pos} | parse_fail={n_parse_fail} | "
        f"rule_viol={n_rule_violation}"
    )

    return rows, {
        "n_predictions": len(rows),
        "n_parse_fail": n_parse_fail,
        "n_rule_violation": n_rule_violation,
        "n_gt_pos": n_gt_pos,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Layer 1 Psi_meta annotator (multi-seed).")
    p.add_argument("--seeds", type=int, default=DEFAULT_N_SEEDS,
                   help="Number of engine seeds to run (default: 30).")
    p.add_argument("--start-seed", type=int, default=DEFAULT_START_SEED,
                   help="First engine seed (default: 0).")
    p.add_argument("--ticks", type=int, default=TICKS,
                   help="Ticks per engine run (default: 500).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    seed_range = list(range(args.start_seed, args.start_seed + args.seeds))

    print(
        f"[layer1] start | seeds={args.seeds} (range {seed_range[0]}..{seed_range[-1]}) "
        f"| ticks={args.ticks} | predict_every={PREDICT_EVERY} | lookahead={LOOKAHEAD}"
    )

    # NOTE: Closed Llama L1 originally ran against llama3.2:3b. Under the
    # Qwen re-run config, llm_backend.DEFAULT_MODEL is qwen2.5:7b — so
    # `OllamaBackend()` here will pick up qwen, silently changing the
    # experiment. To re-execute this file against llama3.2, pass
    # `model="llama3.2"` explicitly. This file is primarily kept for
    # layer3_braid.py's imports; direct execution is not part of the
    # Qwen re-run runtime path.
    backend = OllamaBackend()
    try:
        backend.preflight()
    except BackendError as e:
        print(f"\n[layer1] Ollama preflight FAILED:\n{e}\n", file=sys.stderr)
        return 2
    print(
        f"[layer1] Ollama up at {backend.base_url} | model={backend.model} "
        f"| format=json | temperature={backend.temperature} | "
        f"llm_seed={backend.seed} | num_predict={backend.num_predict}"
    )
    print(f"[layer1] prompt_version={PROMPT_VERSION}")

    predict_ticks = list(range(PREDICT_EVERY, args.ticks - LOOKAHEAD + 1, PREDICT_EVERY))
    print(f"[layer1] prediction points per seed: {len(predict_ticks)}")

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
        print(f"[layer1] cumulative elapsed: {elapsed/60:5.1f} min")

    # Combined CSV.
    combined_path = RESULTS_DIR / "predictions_all_seeds.csv"
    write_csv(combined_path, all_rows)
    print(f"[layer1] combined: {len(all_rows)} rows -> {combined_path.name}")

    # Run-level summary.
    total_parse_fail = sum(s["n_parse_fail"] for s in seed_stats)
    total_rule_viol = sum(s["n_rule_violation"] for s in seed_stats)
    total_preds = sum(s["n_predictions"] for s in seed_stats)
    print(
        f"[layer1] totals across {len(seed_stats)} seeds: "
        f"preds={total_preds} | parse_fail={total_parse_fail} | "
        f"rule_viol={total_rule_viol}"
    )
    print("[layer1] done. Run analysis/layer1_diagnostics.py next.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Response normalisers
# ─────────────────────────────────────────────────────────────────────────────

def _norm_yesno(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "yes" if v else "no"
    s = str(v).strip().lower()
    if s in ("yes", "y", "true", "1"):
        return "yes"
    if s in ("no", "n", "false", "0"):
        return "no"
    return None


def _norm_regime(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    for label in REGIME_LABELS:
        if label in s:
            return label
    return None


def _norm_conf(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x < 0.0:
        x = 0.0
    if x > 1.0:
        x = x / 100.0 if x <= 100.0 else 1.0
    return x


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[layer1] interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
