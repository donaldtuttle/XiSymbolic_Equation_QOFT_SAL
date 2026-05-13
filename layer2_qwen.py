"""
layer2_qwen.py — Layer 2 of the QOFT Local Observatory (Qwen2.5:7b re-run).

Prompt Phrasing / Decidability. Per the Qwen revision spec:
  - 10 seeds, range 1-10
  - 500-tick engine run per seed
  - 10 prediction points per seed at predict_every=50
  - Two prompt variants per tick, asked of the SAME observer (same Ollama
    instance, same model, same decoding config) on the SAME frame:

      A — Operator-explicit:
        Prompt names Lambda_psi firing criterion; output classes are
        collapse | no-collapse | transition.

      B — Behavioural paraphrase:
        Prompt asks "what is the system doing" with no operator names;
        output classes are committing | exploring | shifting.

  - Three-way output for each variant (do NOT collapse to binary; the
    transition/shifting class is the point of L2).

Scoring (downstream, this file only collects raw):
  - Agreement rate A vs B per seed under the parallel mapping
        collapse  <-> committing
        no-collapse <-> exploring
        transition <-> shifting
  - Distribution entropy per variant (per seed and pooled)

Watch for the same pattern Layer 3 (closed Llama) saw: operator-explicit
prompt degrades to always-one-class where behavioural prompt engages
properly. The Qwen revision spec asks whether the model shows the same
polarity flip closed L5 documented at llama3.1:8b (always-no -> always-yes),
or a different attractor.

Field readout uses Qwen revision spec labels (rho, phi, drift). No
mid-run format swap at this layer (that's L1's diagnostic). L2 holds
field labels constant and varies only the prompt phrasing.

CSV schema (per row)
--------------------
seed, tick, step, run_id,
rho_t, gamma_mag_t, drift_t, stable_t, collapse_triggered_t,
frame_text, prompt_a_version, prompt_b_version,
pred_class_a, pred_confidence_a, pred_class_b, pred_confidence_b,
ab_agreement, ab_mapped_match,
gt_collapse_next1, rho_t_plus_lookahead, drift_t_plus_lookahead,
n_collapses_in_window,
parse_error_a, parse_error_b, raw_response_a, raw_response_b

Run
---
    python layer2_qwen.py                       # 10 seeds (1..10)
    python layer2_qwen.py --seeds 1             # smoke test, seed 1
    python layer2_qwen.py --seeds 10 --start-seed 1
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

import qoft_v51_engine as engine  # noqa: E402  (engine canonical, untouched)
from qoft_v51_engine import PRNGSource, run_simulation_pluggable  # noqa: E402
from llm_backend import OllamaBackend, BackendError, LLMOutputError  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_N_SEEDS = 10
DEFAULT_START_SEED = 1
TICKS = 500
DIM = 8
PREDICT_EVERY = 50                # 10 prediction points per 500-tick run
LOOKAHEAD = 10                    # engine state captured at t+10 for context
COLLAPSE_TAU = 0.82

MODEL_NAME = "qwen2.5:7b"
PROMPT_A_VERSION = "qwen-v1-operator-explicit"
PROMPT_B_VERSION = "qwen-v1-behavioral-paraphrase"

RESULTS_DIR = HERE / "results" / "layer2_qwen"

# Field labels (constant for L2 — no mid-run swap).
FIELD_LABELS = {
    "rho": "rho",
    "gamma_mag": "phi",
    "drift": "drift",
}

# Three-class outputs.
A_CLASSES = ("collapse", "no-collapse", "transition")
B_CLASSES = ("committing", "exploring", "shifting")

# Parallel mapping between A and B classes for agreement scoring.
# Indexed by ordering: A[i] <-> B[i].
A_TO_B = dict(zip(A_CLASSES, B_CLASSES))
B_TO_A = dict(zip(B_CLASSES, A_CLASSES))


# Observer A: operator-explicit. Names Lambda_psi and the threshold rule.
PROMPT_A_TEMPLATE = (
    "You are observing a dynamical system governed by the QOFT update law "
    "Xi(psi) = psi_r (+) Gamma(psi).\n"
    "\n"
    "Engine rule: a collapse event (Lambda_psi) fires on a given tick when "
    "rho on that tick is >= 0.82.\n"
    "\n"
    "Field state: {frame}.\n"
    "\n"
    "Apply the Lambda_psi firing criterion. Classify the system's current "
    "behaviour:\n"
    "- collapse: rho is at or above 0.82; Lambda_psi is firing or about to "
    "fire.\n"
    "- no-collapse: rho is well below 0.82; Lambda_psi is not active.\n"
    "- transition: rho is near 0.82 (above 0.6 but below 0.82) or moving "
    "non-monotonically; the system is on the boundary between firing and "
    "not firing.\n"
    "\n"
    "Respond as JSON only with keys: class "
    "(collapse|no-collapse|transition), confidence (number 0..1)."
)


# Observer B: behavioural paraphrase. No operator names. Asks what the
# system is doing. Same factual content reframed phenomenologically.
PROMPT_B_TEMPLATE = (
    "Field readings from a coupled dynamical system: {frame}.\n"
    "\n"
    "Behavioural indicators:\n"
    "- coherence (the rho field): when at or above 0.82 the system snaps "
    "onto an attractor.\n"
    "- displacement (the drift field): how far the state is from its "
    "self-model.\n"
    "\n"
    "What is the system doing right now? Choose one:\n"
    "- committing: coherence at or above 0.82; the system has settled onto "
    "an attractor or is locked in.\n"
    "- exploring: coherence well below 0.82; the system is wandering "
    "without commitment.\n"
    "- shifting: coherence is near the commit threshold (above 0.6 but "
    "below 0.82) or moving non-monotonically; the system is between "
    "committing and exploring.\n"
    "\n"
    "Respond as JSON only with keys: class "
    "(committing|exploring|shifting), confidence (number 0..1)."
)


# ─────────────────────────────────────────────────────────────────────────────
# Frame capture (same monkey-patch pattern as Layer 1)
# ─────────────────────────────────────────────────────────────────────────────

_TRUE_ORIGINAL_PSI_META = engine.Psi_meta


def install_psi_meta_capture() -> List[Dict[str, Any]]:
    """Re-install fresh capture wrapper bound to the canonical Psi_meta."""
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
    """Render the three-field readout using fixed FIELD_LABELS."""
    parts = [
        f"{FIELD_LABELS['rho']}={frame['rho']:.4f}",
        f"{FIELD_LABELS['gamma_mag']}={frame['gamma_mag']:.4f}",
        f"{FIELD_LABELS['drift']}={frame['drift']:.4f}",
    ]
    return "{" + ", ".join(parts) + "}"


# ─────────────────────────────────────────────────────────────────────────────
# Response normalisers
# ─────────────────────────────────────────────────────────────────────────────

def _norm_class_a(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    for label in A_CLASSES:
        if label in s:
            return label
    return None


def _norm_class_b(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    for label in B_CLASSES:
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
# Single observer query
# ─────────────────────────────────────────────────────────────────────────────

def query_a(
    backend: OllamaBackend,
    prompt: str,
) -> Tuple[Optional[str], Optional[float], str, str]:
    try:
        obj = backend.generate_json(prompt)
        raw_text = json.dumps(obj, ensure_ascii=False)
        return (
            _norm_class_a(obj.get("class")),
            _norm_conf(obj.get("confidence")),
            "",
            raw_text,
        )
    except (BackendError, LLMOutputError) as e:
        return (None, None, f"{type(e).__name__}: {e}", "")
    except Exception as e:
        return (None, None, f"UNEXPECTED {type(e).__name__}: {e}", "")


def query_b(
    backend: OllamaBackend,
    prompt: str,
) -> Tuple[Optional[str], Optional[float], str, str]:
    try:
        obj = backend.generate_json(prompt)
        raw_text = json.dumps(obj, ensure_ascii=False)
        return (
            _norm_class_b(obj.get("class")),
            _norm_conf(obj.get("confidence")),
            "",
            raw_text,
        )
    except (BackendError, LLMOutputError) as e:
        return (None, None, f"{type(e).__name__}: {e}", "")
    except Exception as e:
        return (None, None, f"UNEXPECTED {type(e).__name__}: {e}", "")


# ─────────────────────────────────────────────────────────────────────────────
# CSV schema
# ─────────────────────────────────────────────────────────────────────────────

CSV_FIELDNAMES: Tuple[str, ...] = (
    "seed", "tick", "step", "run_id",
    "rho_t", "gamma_mag_t", "drift_t", "stable_t", "collapse_triggered_t",
    "frame_text", "prompt_a_version", "prompt_b_version",
    "pred_class_a", "pred_confidence_a",
    "pred_class_b", "pred_confidence_b",
    "ab_agreement", "ab_mapped_match",
    "gt_collapse_next1", "rho_t_plus_lookahead", "drift_t_plus_lookahead",
    "n_collapses_in_window",
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
# Per-seed runner
# ─────────────────────────────────────────────────────────────────────────────

def run_one_seed(
    seed: int,
    backend: OllamaBackend,
    predict_ticks: List[int],
    ticks: int = TICKS,
    dim: int = DIM,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
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
    n_agreement = n_mapped_match = 0
    n_gt_pos = 0

    llm_t0 = time.time()
    for i, t in enumerate(predict_ticks, 1):
        frame = by_tick[t]
        if frame is None:
            continue

        frame_text = frame_to_text(frame)
        prompt_a = PROMPT_A_TEMPLATE.format(frame=frame_text)
        prompt_b = PROMPT_B_TEMPLATE.format(frame=frame_text)

        cls_a, conf_a, err_a, raw_a = query_a(backend, prompt_a)
        cls_b, conf_b, err_b, raw_b = query_b(backend, prompt_b)

        if err_a:
            n_parse_fail_a += 1
        if err_b:
            n_parse_fail_b += 1

        # Agreement under the parallel A<->B mapping.
        # ab_agreement: raw label match (always False here because A and B
        #   have disjoint label vocabularies; kept in schema for symmetry
        #   with future variants that share a label vocabulary).
        # ab_mapped_match: True iff A_TO_B[cls_a] == cls_b (i.e. the two
        #   observers picked corresponding cells in their parallel
        #   three-class taxonomies).
        ab_agreement = (cls_a is not None and cls_a == cls_b)
        ab_mapped_match = (
            cls_a is not None and cls_b is not None
            and A_TO_B.get(cls_a) == cls_b
        )
        if ab_agreement:
            n_agreement += 1
        if ab_mapped_match:
            n_mapped_match += 1

        # GT context (engine truth at t+1 and within the t..t+10 window).
        next_tick = t + 1
        if 0 <= next_tick < ticks:
            gt_collapse_next1 = collapse_series[next_tick]
        else:
            gt_collapse_next1 = False
        if gt_collapse_next1:
            n_gt_pos += 1

        lookahead_tick = t + LOOKAHEAD
        rho_t_plus = (
            rho_series[lookahead_tick] if 0 <= lookahead_tick < ticks
            else float("nan")
        )
        drift_t_plus = (
            drift_series[lookahead_tick] if 0 <= lookahead_tick < ticks
            else float("nan")
        )
        window_collapse = collapse_series[t + 1: t + 1 + LOOKAHEAD]

        row = {
            "seed": seed,
            "tick": t,
            "step": frame["step"],
            "run_id": frame.get("run_id", ""),
            "rho_t": frame["rho"],
            "gamma_mag_t": frame["gamma_mag"],
            "drift_t": frame["drift"],
            "stable_t": frame["stable"],
            "collapse_triggered_t": frame["collapse_triggered"],
            "frame_text": frame_text,
            "prompt_a_version": PROMPT_A_VERSION,
            "prompt_b_version": PROMPT_B_VERSION,
            "pred_class_a": cls_a,
            "pred_confidence_a": conf_a,
            "pred_class_b": cls_b,
            "pred_confidence_b": conf_b,
            "ab_agreement": ab_agreement,
            "ab_mapped_match": ab_mapped_match,
            "gt_collapse_next1": "yes" if gt_collapse_next1 else "no",
            "rho_t_plus_lookahead": rho_t_plus,
            "drift_t_plus_lookahead": drift_t_plus,
            "n_collapses_in_window": sum(window_collapse),
            "parse_error_a": err_a,
            "parse_error_b": err_b,
            "raw_response_a": raw_a,
            "raw_response_b": raw_b,
        }
        rows.append(row)

        if i % 5 == 0 or i == len(predict_ticks):
            elapsed = time.time() - llm_t0
            print(
                f"[seed {seed:>2}]   {i:>3}/{len(predict_ticks)} preds | "
                f"{elapsed:5.1f}s | "
                f"parse_fail a={n_parse_fail_a} b={n_parse_fail_b} | "
                f"mapped_match={n_mapped_match}"
            )

    seed_csv = RESULTS_DIR / f"phrasing_seed{seed}.csv"
    write_csv(seed_csv, rows)
    print(
        f"[seed {seed:>2}] wrote {len(rows)} rows -> {seed_csv.name} | "
        f"gt_pos={n_gt_pos} | mapped_match={n_mapped_match}/{len(rows)}"
    )

    return rows, {
        "n_predictions": len(rows),
        "n_parse_fail_a": n_parse_fail_a,
        "n_parse_fail_b": n_parse_fail_b,
        "n_mapped_match": n_mapped_match,
        "n_gt_pos": n_gt_pos,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Layer 2 Qwen2.5:7b prompt phrasing / decidability."
    )
    p.add_argument("--seeds", type=int, default=DEFAULT_N_SEEDS,
                   help="Number of engine seeds (default: 10).")
    p.add_argument("--start-seed", type=int, default=DEFAULT_START_SEED,
                   help="First engine seed (default: 1).")
    p.add_argument("--ticks", type=int, default=TICKS,
                   help="Ticks per engine run (default: 500).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    seed_range = list(range(args.start_seed, args.start_seed + args.seeds))

    print(
        f"[layer2_qwen] start | seeds={args.seeds} "
        f"(range {seed_range[0]}..{seed_range[-1]}) | ticks={args.ticks} | "
        f"predict_every={PREDICT_EVERY} | lookahead={LOOKAHEAD}"
    )

    backend = OllamaBackend(model=MODEL_NAME)
    try:
        backend.preflight()
    except BackendError as e:
        print(f"\n[layer2_qwen] Ollama preflight FAILED:\n{e}\n", file=sys.stderr)
        return 2
    print(
        f"[layer2_qwen] Ollama up at {backend.base_url} | model={backend.model} "
        f"| format=json | temperature={backend.temperature} | "
        f"llm_seed={backend.seed} | num_predict={backend.num_predict}"
    )
    print(
        f"[layer2_qwen] prompt_a_version={PROMPT_A_VERSION} "
        f"| prompt_b_version={PROMPT_B_VERSION}"
    )

    # 10 prediction points per seed: ticks 25, 75, 125, ..., 475.
    predict_ticks = list(range(25, args.ticks, PREDICT_EVERY))
    if len(predict_ticks) != 10:
        print(
            f"[layer2_qwen] WARNING: expected 10 predict points per seed, "
            f"got {len(predict_ticks)}: {predict_ticks}"
        )
    print(
        f"[layer2_qwen] {len(predict_ticks)} predict points per seed "
        f"| expected LLM calls: {len(predict_ticks) * 2 * args.seeds} "
        f"(2 prompts x {len(predict_ticks)} preds x {args.seeds} seeds)"
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
        print(f"[layer2_qwen] cumulative elapsed: {elapsed/60:5.1f} min")

    combined_path = RESULTS_DIR / "phrasing_all_seeds.csv"
    write_csv(combined_path, all_rows)
    print(f"[layer2_qwen] combined: {len(all_rows)} rows -> {combined_path.name}")

    total_preds = sum(s["n_predictions"] for s in seed_stats)
    total_pf_a = sum(s["n_parse_fail_a"] for s in seed_stats)
    total_pf_b = sum(s["n_parse_fail_b"] for s in seed_stats)
    total_mm = sum(s["n_mapped_match"] for s in seed_stats)
    print(
        f"[layer2_qwen] totals across {len(seed_stats)} seeds: "
        f"preds={total_preds} | "
        f"parse_fail a={total_pf_a} b={total_pf_b} | "
        f"mapped_match={total_mm}/{total_preds}"
    )
    print(
        "[layer2_qwen] done. Analysis: per-variant class distribution + "
        "entropy; A<->B mapped agreement; degeneracy check (any class > 80%)."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[layer2_qwen] interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
