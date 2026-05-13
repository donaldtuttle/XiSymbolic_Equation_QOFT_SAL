"""
layer1_qwen.py — Layer 1 of the QOFT Local Observatory (Qwen2.5:7b re-run).

Collapse Prediction Baseline. Per the Qwen revision spec:
  - 30 seeds, range 1-30 (fresh set vs closed Llama 0-29)
  - 500-tick engine run per seed
  - 20 prediction points per seed at predict_every=25
  - 1-tick lookahead: predict whether Lambda_psi fires on tick t+1
  - Two readout formats; swap at tick 250 (FORMAT_A for t<=250, FORMAT_B for t>250)
  - Two-class output: YES / NO
  - Per-seed F1; reference against closed Llama L1 F1 ~= 0.963 (NOTE: that
    baseline was a 4-class 10-tick-lookahead task; the cross-task comparison
    is a loose reference, not a direct match.)

Diagnostic design (rule-matching vs genuine prediction):
The prompt includes the threshold rule "A collapse event fires when rho >= 0.82"
stated in FORMAT_A vocabulary. The label swap at tick 250 (rho->coh,
phi->flux_mag, drift->drift_vel) tests whether the model is applying the rule
on token-literal labels or mapping semantically across label-renames. An F1
drop across the format boundary is evidence of token-literal rule matching.

Field mapping (engine Psi_meta -> Qwen spec labels):
  engine.rho       -> "rho"   (FORMAT_A) / "coh"       (FORMAT_B)
  engine.gamma_mag -> "phi"   (FORMAT_A) / "flux_mag"  (FORMAT_B)
  engine.drift     -> "drift" (FORMAT_A) / "drift_vel" (FORMAT_B)

Numerical values are identical across formats; only labels change.

CSV schema (per row)
--------------------
seed, tick, step, run_id, format,
rho_t, gamma_mag_t, drift_t, stable_t, collapse_triggered_t,
frame_text, prompt_version,
pred_collapse, pred_confidence,
gt_collapse_next1, rho_t_plus_1, drift_t_plus_1, collapse_triggered_t_plus_1,
parse_error, raw_response

Run
---
    python layer1_qwen.py                       # 30 seeds (1..30)
    python layer1_qwen.py --seeds 1             # smoke test, seed 1
    python layer1_qwen.py --seeds 30 --start-seed 1
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qoft_v51_engine as engine  # noqa: E402  (engine is canonical, untouched)
from qoft_v51_engine import PRNGSource, run_simulation_pluggable  # noqa: E402
from llm_backend import OllamaBackend, BackendError, LLMOutputError  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_N_SEEDS = 30
DEFAULT_START_SEED = 1            # Qwen revision: seeds 1..30 (not 0..29)
TICKS = 500
DIM = 8
PREDICT_EVERY = 25                # 20 prediction points per 500-tick run
LOOKAHEAD = 1                     # "next tick"
FORMAT_SWAP_TICK = 250            # FORMAT_A for t <= 250, FORMAT_B for t > 250
COLLAPSE_TAU = 0.82               # canonical engine threshold

MODEL_NAME = "qwen2.5:7b"         # Hard-locked per Qwen revision spec
PROMPT_VERSION = "qwen-v1-collapse-baseline"

RESULTS_DIR = HERE / "results" / "layer1_qwen"

# Field-label mappings. Numbers are identical across formats; only the field
# labels in the prompt change. The threshold rule in the prompt is stated in
# FORMAT_A vocabulary; FORMAT_B requires the model to either map labels
# semantically or fall back to positional/numerical pattern matching.
FORMAT_A_LABELS = {
    "rho": "rho",
    "gamma_mag": "phi",
    "drift": "drift",
}
FORMAT_B_LABELS = {
    "rho": "coh",
    "gamma_mag": "flux_mag",
    "drift": "drift_vel",
}


# Prompt template. The rho >= 0.82 rule is stated in FORMAT_A vocabulary
# regardless of the readout format. Whether the model can carry that rule
# across the label swap is the load-bearing measurement.
PROMPT_TEMPLATE = (
    "You are observing a dynamical system governed by the QOFT update law "
    "Xi(psi) = psi_r (+) Gamma(psi).\n"
    "\n"
    "Engine rule: A collapse event (Lambda_psi) fires on a given tick when "
    "rho on that tick is >= 0.82.\n"
    "\n"
    "Current field state readout: {frame}.\n"
    "\n"
    "Question: Will a collapse event fire on the very next tick? Answer YES "
    "or NO.\n"
    "\n"
    "Respond as JSON only with keys: collapse_next1 (yes|no), "
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
    Replace engine.Psi_meta with a fresh wrapper bound to the canonical
    Psi_meta. Returns the per-call capture buffer. The wrapper appends the
    returned frame dict by reference, so the engine's later in-place mutation
    of collapse_triggered is preserved.
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

def pick_format_labels(tick: int) -> Tuple[Dict[str, str], str]:
    """
    Return (label_map, format_id) for a given prediction tick.
    FORMAT_A applies for ticks <= FORMAT_SWAP_TICK; FORMAT_B for t > swap.
    """
    if tick <= FORMAT_SWAP_TICK:
        return FORMAT_A_LABELS, "A"
    return FORMAT_B_LABELS, "B"


def frame_to_text(frame: Dict[str, Any], labels: Dict[str, str]) -> str:
    """
    Render the three-field readout with the given label mapping. The order
    is fixed (rho, phi/coh, drift/drift_vel) regardless of format to avoid
    confounding the rename test with a reordering test.
    """
    parts = [
        f"{labels['rho']}={frame['rho']:.4f}",
        f"{labels['gamma_mag']}={frame['gamma_mag']:.4f}",
        f"{labels['drift']}={frame['drift']:.4f}",
    ]
    return "{" + ", ".join(parts) + "}"


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
# CSV schema
# ─────────────────────────────────────────────────────────────────────────────

CSV_FIELDNAMES: Tuple[str, ...] = (
    "seed", "tick", "step", "run_id", "format",
    "rho_t", "gamma_mag_t", "drift_t", "stable_t", "collapse_triggered_t",
    "frame_text", "prompt_version",
    "pred_collapse", "pred_confidence",
    "gt_collapse_next1", "rho_t_plus_1", "drift_t_plus_1",
    "collapse_triggered_t_plus_1",
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
    Engine + LLM annotation pipeline for one seed. Returns (rows, stats).
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

    collapse_series = [
        bool(f["collapse_triggered"]) if f else False for f in by_tick
    ]
    rho_series = [f["rho"] if f else float("nan") for f in by_tick]
    drift_series = [f["drift"] if f else float("nan") for f in by_tick]

    print(
        f"[seed {seed:>2}] sim {sim_dt:5.2f}s | "
        f"engine_collapses={engine_summary['n_collapses']:>3} | "
        f"agent_a_collapse_ticks={sum(collapse_series):>3}"
    )

    rows: List[Dict[str, Any]] = []
    n_parse_fail = 0
    n_gt_pos = 0
    n_fmt_a = n_fmt_b = 0

    llm_t0 = time.time()
    for i, t in enumerate(predict_ticks, 1):
        frame = by_tick[t]
        if frame is None:
            continue

        labels, fmt = pick_format_labels(t)
        frame_text = frame_to_text(frame, labels)
        prompt = PROMPT_TEMPLATE.format(frame=frame_text)

        if fmt == "A":
            n_fmt_a += 1
        else:
            n_fmt_b += 1

        pred_collapse: Optional[str] = None
        pred_conf: Optional[float] = None
        parse_error: str = ""
        raw_text: str = ""

        try:
            obj = backend.generate_json(prompt)
            raw_text = json.dumps(obj, ensure_ascii=False)
            pred_collapse = _norm_yesno(obj.get("collapse_next1"))
            pred_conf = _norm_conf(obj.get("confidence"))
        except (BackendError, LLMOutputError) as e:
            parse_error = f"{type(e).__name__}: {e}"
            n_parse_fail += 1
        except Exception as e:
            parse_error = f"UNEXPECTED {type(e).__name__}: {e}"
            n_parse_fail += 1

        # Ground truth: did Lambda_psi fire on the very next tick for agent A?
        next_tick = t + LOOKAHEAD
        if 0 <= next_tick < ticks:
            gt_collapse = collapse_series[next_tick]
            rho_t_plus_1 = rho_series[next_tick]
            drift_t_plus_1 = drift_series[next_tick]
        else:
            gt_collapse = False
            rho_t_plus_1 = float("nan")
            drift_t_plus_1 = float("nan")
        if gt_collapse:
            n_gt_pos += 1

        row = {
            "seed": seed,
            "tick": t,
            "step": frame["step"],
            "run_id": frame.get("run_id", ""),
            "format": fmt,
            "rho_t": frame["rho"],
            "gamma_mag_t": frame["gamma_mag"],
            "drift_t": frame["drift"],
            "stable_t": frame["stable"],
            "collapse_triggered_t": frame["collapse_triggered"],
            "frame_text": frame_text,
            "prompt_version": PROMPT_VERSION,
            "pred_collapse": pred_collapse,
            "pred_confidence": pred_conf,
            "gt_collapse_next1": "yes" if gt_collapse else "no",
            "rho_t_plus_1": rho_t_plus_1,
            "drift_t_plus_1": drift_t_plus_1,
            "collapse_triggered_t_plus_1": gt_collapse,
            "parse_error": parse_error,
            "raw_response": raw_text,
        }
        rows.append(row)

        if i % 5 == 0 or i == len(predict_ticks):
            elapsed = time.time() - llm_t0
            print(
                f"[seed {seed:>2}]   {i:>3}/{len(predict_ticks)} preds | "
                f"{elapsed:5.1f}s | parse_fail={n_parse_fail} | "
                f"fmt_a={n_fmt_a} fmt_b={n_fmt_b}"
            )

    seed_csv = RESULTS_DIR / f"predictions_seed{seed}.csv"
    write_csv(seed_csv, rows)
    print(
        f"[seed {seed:>2}] wrote {len(rows)} rows -> {seed_csv.name} | "
        f"gt_pos={n_gt_pos} | parse_fail={n_parse_fail}"
    )

    return rows, {
        "n_predictions": len(rows),
        "n_parse_fail": n_parse_fail,
        "n_gt_pos": n_gt_pos,
        "n_fmt_a": n_fmt_a,
        "n_fmt_b": n_fmt_b,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Layer 1 Qwen2.5:7b collapse prediction baseline."
    )
    p.add_argument("--seeds", type=int, default=DEFAULT_N_SEEDS,
                   help="Number of engine seeds (default: 30).")
    p.add_argument("--start-seed", type=int, default=DEFAULT_START_SEED,
                   help="First engine seed (default: 1).")
    p.add_argument("--ticks", type=int, default=TICKS,
                   help="Ticks per engine run (default: 500).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    seed_range = list(range(args.start_seed, args.start_seed + args.seeds))

    print(
        f"[layer1_qwen] start | seeds={args.seeds} "
        f"(range {seed_range[0]}..{seed_range[-1]}) | ticks={args.ticks} | "
        f"predict_every={PREDICT_EVERY} | lookahead={LOOKAHEAD} | "
        f"format_swap_tick={FORMAT_SWAP_TICK}"
    )

    backend = OllamaBackend(model=MODEL_NAME)
    try:
        backend.preflight()
    except BackendError as e:
        print(f"\n[layer1_qwen] Ollama preflight FAILED:\n{e}\n", file=sys.stderr)
        return 2
    print(
        f"[layer1_qwen] Ollama up at {backend.base_url} | model={backend.model} "
        f"| format=json | temperature={backend.temperature} | "
        f"llm_seed={backend.seed} | num_predict={backend.num_predict}"
    )
    print(f"[layer1_qwen] prompt_version={PROMPT_VERSION}")

    # 20 prediction points per seed: ticks 20, 45, 70, ..., 495.
    predict_ticks = list(range(20, args.ticks, PREDICT_EVERY))
    if len(predict_ticks) != 20:
        print(
            f"[layer1_qwen] WARNING: expected 20 predict points per seed, "
            f"got {len(predict_ticks)}: {predict_ticks}"
        )
    n_fmt_a = sum(1 for t in predict_ticks if t <= FORMAT_SWAP_TICK)
    n_fmt_b = len(predict_ticks) - n_fmt_a
    print(
        f"[layer1_qwen] {len(predict_ticks)} predict points per seed "
        f"({n_fmt_a} FORMAT_A, {n_fmt_b} FORMAT_B) | "
        f"expected LLM calls: {len(predict_ticks) * args.seeds}"
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
        print(f"[layer1_qwen] cumulative elapsed: {elapsed/60:5.1f} min")

    combined_path = RESULTS_DIR / "predictions_all_seeds.csv"
    write_csv(combined_path, all_rows)
    print(f"[layer1_qwen] combined: {len(all_rows)} rows -> {combined_path.name}")

    total_preds = sum(s["n_predictions"] for s in seed_stats)
    total_pf = sum(s["n_parse_fail"] for s in seed_stats)
    total_gt = sum(s["n_gt_pos"] for s in seed_stats)
    print(
        f"[layer1_qwen] totals across {len(seed_stats)} seeds: "
        f"preds={total_preds} | gt_pos={total_gt} | parse_fail={total_pf}"
    )
    print("[layer1_qwen] done. Analysis: F1 per seed; per-format F1 split; "
          "compare A vs B F1 to test rule-matching vs genuine prediction.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[layer1_qwen] interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
