# QOFT Local Observatory — Qwen2.5:7b Run Book

**Branch:** `claude/test-oscillation-detection-8ymqv`
**Model (hard-locked across the suite):** `qwen2.5:7b`
**Decoding (hard-locked):** `format=json`, `temperature=0`, `llm_seed=0`,
`num_predict=256`

This run book is the canonical execution order for the Qwen re-run. It
references the Cowork build for L6/L6b and the legacy Llama closed
results for L4/L5 (which are not replicated under Qwen).

---

## 0. Why this layout

The Layer 4/5/6 brief (closed Llama findings) plus the Qwen revision
spec produced the following design decisions:

- **L1, L2:** Qwen revision protocols (simpler), fresh tree under
  `layer{1,2}_qwen.py`. Llama L1/L2 results are already closed and not
  reproduced.
- **L3:** closed Llama v3.1 source verbatim with one delta — model
  flipped from `llama3.2` to `qwen2.5:7b`. Trajectory window /
  dual-observer / four-class regime is the load-bearing measurement
  apparatus that produced the oscillating class-collapse finding;
  changing the protocol here would break the Qwen vs Llama
  comparability on oscillating. v3.1 vs v3.2 version drift is
  documented in the file header.
- **L4 / L5:** SKIPPED. Closed Llama L4 (schema augmentation, 11
  within-window stats) and L5 (8B capacity step) already falsified
  schema-as-bottleneck and capacity-as-bottleneck. Reconstructing L4
  from the brief would introduce comparability drift; the closed result
  is referenced, not replicated.
- **L6:** label-semantics ablation. Three conditions: synonym
  substitution / numeric threshold / one-shot example. PENDING the L3
  read-out (if Qwen L3 also produces 0 oscillating, L6 is the
  load-bearing layer).
- **L6b:** NLA telemetry, runs parallel with L6. PENDING Cowork
  delivery of the NLA bridge (see `cowork_nla_build.md`).

---

## 1. Run order

```
1.  ollama pull qwen2.5:7b                       (Cowork box)
2.  ollama list                                  (confirm tag qwen2.5:7b)
3.  Pull NLA Qwen2.5 checkpoint from repo        (Cowork box)
4.  layer1_qwen.py                               (collapse prediction baseline)
5.  layer2_qwen.py                               (prompt phrasing / decidability)
6.  layer3_braid.py                              (trajectory-window dual-observer)
7.  Read Layer 3 result. If 0 oscillating again on qwen, proceed to L6.
8.  cowork_nla_build.md                          (Cowork builds NLA bridge)
9.  Cowork P1-P5 preflight. P2 and P3 are HARD go/no-go gates.
10. Layer 6 (label-semantics ablation; spec pending)
11. Layer 6b runs in parallel with L6 (NLA telemetry)
12. Compare NLA verbalization against Layer 6 classification results
```

P2 and P3 from the Cowork prompt are the only gates that block L6/L6b
from starting:

- **P2:** `ollama list` shows `qwen2.5:7b`. Mismatch = STOP, report.
- **P3:** NLA checkpoint for Qwen2.5 family exists in
  `docs/inference.md`. Missing = STOP, report.

No silent substitution at either gate.

---

## 2. Layer-by-layer commands

All commands assumed run from the repo root.

### Layer 1 — collapse prediction baseline (qwen)
```
python layer1_qwen.py                        # 30 seeds (1..30)
python layer1_qwen.py --seeds 1              # smoke test, seed 1
```
- 30 seeds × 20 prediction points = 600 LLM calls
- 1-tick lookahead, YES/NO output
- Format swap at tick 250 (FORMAT_A rho/phi/drift -> FORMAT_B
  coh/flux_mag/drift_vel). Threshold rule stated once, in FORMAT_A
  vocabulary; FORMAT_B adds a label-correspondence note but does NOT
  restate the rule.
- Output: `results/layer1_qwen/predictions_*.csv`

### Layer 2 — prompt phrasing / decidability (qwen)
```
python layer2_qwen.py                        # 10 seeds (1..10)
python layer2_qwen.py --seeds 1              # smoke test
```
- 10 seeds × 10 prediction points × 2 prompts = 200 LLM calls
- Three-way outputs (do NOT collapse to binary): A
  `collapse|no-collapse|transition`, B `committing|exploring|shifting`
- Parallel A↔B mapping for agreement scoring
- Output: `results/layer2_qwen/phrasing_*.csv`

### Layer 3 — trajectory-window dual-observer (qwen, closed v3.1 protocol)
```
python layer3_braid.py                       # 30 seeds (0..29)
python layer3_braid.py --seeds 1             # smoke test, seed 0
```
- 30 seeds × 49 prediction points × 2 observers = 2940 LLM calls
- Trajectory window N=5 frames at stride k=2 + rolling deltas
- Four-class regime (converging/drifting/oscillating/collapsing)
- 10-tick lookahead; predict_every=10
- Output: `results/layer3/braid_*.csv`
- **Load-bearing read after this:** is oscillating non-zero on any
  seed? If yes → L4/L5 hypotheses partially rehabilitated for Qwen;
  scope L6 design accordingly. If no → label-semantics is now the
  remaining hypothesis on this model too; L6 primary fork is right.

### Layer 4 / Layer 5 — SKIPPED on qwen
Reference the closed Llama findings from the L4/L5 brief. Do not
reconstruct L4 from the brief description (11 within-window stats);
that would introduce comparability drift. The closed result stands.

### Cowork build — NLA telemetry bridge
See `cowork_nla_build.md`. Runs on the Cowork Windows box, NOT in this
tree. Produces `nla_inference.py` and the Xi-agent collapse hook.

### Layer 6 — label-semantics ablation (qwen, post-P2/P3, PENDING SPEC)
Three conditions per the Qwen revision spec:
1. Synonym substitution ("swinging back and forth" for "oscillating")
2. Numeric threshold (binary rule: phi crosses its mean > 3 times in
   8 ticks → YES/NO; remove the category label entirely)
3. Example injection (one-shot example of oscillating behaviour
   before the classification task)

L6 source file will be `layer6_label_ablation.py`. NOT WRITTEN until
after the L3 read-out and Cowork P2/P3 pass.

### Layer 6b — NLA telemetry (qwen, parallel with L6, PENDING COWORK)
`layer6b_nla_telemetry.py` stub is staged. It will refuse to run until
`nla_inference.verbalize_activation` is importable. Cowork delivers
that module; once on the path and the layer index from
`docs/inference.md` is wired, the stub's `NotImplementedError` gate
gets removed and L6b orchestrates the L6 co-run with passive NLA
sampling.

---

## 3. What each layer outputs

```
results/
  layer1_qwen/
    predictions_seed{1..30}.csv
    predictions_all_seeds.csv
  layer2_qwen/
    phrasing_seed{1..10}.csv
    phrasing_all_seeds.csv
  layer3/
    braid_seed{0..29}.csv
    braid_all_seeds.csv
  layer6/                          (when L6 lands)
    ablation_seed{N}.csv
    ablation_all_seeds.csv
  layer6b/
    nla_collapse_log.jsonl
```

---

## 4. Cross-cutting hard rules

- **Do not change MODEL_NAME mid-layer.** All layers in this suite
  must use `qwen2.5:7b`. The `MODEL_NAME` constant in each runnable
  layer file is the authoritative source.
- **Do not silently substitute** on Cowork P2/P3 failure. Stop and
  report.
- **Do not modify `qoft_v51_engine.py`.** It is the canonical engine.
  Layer instrumentation captures `Psi_meta` via a non-invasive
  monkey-patch (`install_psi_meta_capture`); the engine source stays
  untouched.
- **Do not modify the L3 measurement apparatus.** The v3.1 trajectory
  window / dual-observer / four-class regime is the apparatus that
  produced the oscillating class-collapse finding. The Qwen re-run is
  about changing the model, not the apparatus.
- **Do not re-execute `layer1_annotator.py` directly.** It is legacy
  closed Llama source kept only for L3's imports. Under the Qwen
  default it would silently use `qwen2.5:7b` and overwrite the closed
  Llama L1 CSVs.

---

## 5. What this suite ultimately produces

- A clean Qwen baseline across L1-L3 directly comparable to the closed
  Llama results.
- Layer 3 read-out: is "0 oscillating" universal across model families
  or Llama-specific?
- Layer 6 answer (once spec is finalized): is oscillating a label
  problem or a representation problem?
- Layer 6b NLA verbalization log: what Qwen represents internally at
  collapse, paired with Layer 6's classification output.
- First correlation: QOFT field telemetry ↔ internal activation
  language.
