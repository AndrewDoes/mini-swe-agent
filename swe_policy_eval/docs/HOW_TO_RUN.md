# Step-by-Step Guide: Running `swe_policy_eval`

This guide walks through every step required to configure, test, execute, evaluate, and analyze the **Fixed Retry Policy (`fixed`) vs. Progress-Aware Loop Controller (`controller`)** Pre-Thesis experiment on **SWE-bench Verified** across **Windows, macOS, and Linux**.

> For a deep dive into every folder, source file, class, and policy rule in the repository, see [`docs/ARCHITECTURE_AND_CODEBASE.md`](ARCHITECTURE_AND_CODEBASE.md).

---

## Overview of the Pipeline

```
[1. Configure]       .env (API key) + config/experiment.yaml (seed, model, caps, policies)
       │
       ▼
[2. Smoke Test]      pytest tests -q  (verifies policies, round loop, caps, and diff logic)
       │
       ▼
[3. Select Tasks]    scripts/select_tasks.py  ──► results/tasks.json (10 pilot + 490 main IDs)
       │
       ▼
[4. Pre-pull Images] scripts/prepull.py       ──► pulls swebench Docker images in advance
       │
       ▼
[5. Pilot Stage]     scripts/run_experiment.py --stage pilot
                     scripts/evaluate.py       --stage pilot
                     scripts/analyze.py        --stage pilot
       │
       ▼             (Inspect pilot cost & variability -> freeze parameters & pick N_main)
[6. Main Stage]      scripts/run_experiment.py --stage main --n-main <N> --repeats <R>
                     scripts/evaluate.py       --stage main
                     scripts/analyze.py        --stage main
```

---

## Prerequisites

1. **Docker**
   - **Windows / macOS**: Install and start **Docker Desktop** (make sure the whale icon shows the engine is running before executing SWE-bench tasks).
   - **Linux**: Install Docker Engine and ensure your user is in the `docker` group (`docker info` should succeed).
   - **Disk Space**: Each SWE-bench task image is ~2–5 GB. Having **30–50 GB of free disk space** is recommended.
2. **API Key**
   - An **OpenRouter API key** (default config uses `openrouter` with `qwen/qwen3-coder-30b-a3b-instruct`), or an API key for another provider supported by `mini-swe-agent` / `litellm`.
   - **Important**: Use a paid/metered model endpoint that reports non-zero USD cost per call so the hard budget caps (`per_run_cap_usd` and `global_cap_usd`) operate accurately.

---

## Step 1: Configure Your API Key and Experiment Settings

### 1.1 Set up `.env`
From inside the `swe_policy_eval/` folder, create `.env` from `.env.example` (if not already present) and add your API key:

```bash
cd swe_policy_eval
cp .env.example .env
```

Edit `swe_policy_eval/.env`:
```ini
MSWEA_MODEL_NAME="openrouter/qwen/qwen3-coder-30b-a3b-instruct"
OPENROUTER_API_KEY="sk-or-v1-your-actual-key"
```

### 1.2 Review `config/experiment.yaml` (Setting $k$ Loops, Steps, and Budgets)
Open `swe_policy_eval/config/experiment.yaml`. Here is how your three experimental dimensions map to the config:

1. **15 Test Runs per Method (`repeats`)**:
   - Set `repeats: 15` in `config/experiment.yaml` (or pass `--repeats 15` on the command line).
2. **$n$ Tasks per Run (`--n-main <n>`)**:
   - Chosen when running `run_experiment.py --stage main --n-main <n>` (the current `results/tasks.json` has `10` tasks in `pilot_ids` and `490` tasks in `main_ids_ordered`).
3. **Maximum $k$ Loops (Rounds) per Task (`rounds` and `step_ceiling`)**:
   - In the code, each outer policy loop is called a **`round`**, and each round lets the agent execute up to `steps_per_round` bash commands (`step_ceiling` is the total step cap across all rounds of a task):
   - For example, if you decide on **$k = 10$ loops per task** with `6` steps per loop ($10 \times 6 = 60$ total steps):
     ```yaml
     limits:
       step_ceiling: 60           # k loops * steps_per_round (e.g. 10 * 6 = 60)
     repeats: 15                  # 15 test runs per method
     policies:
       fixed:
         rounds: 10               # k = 10 loops per task
         steps_per_round: 6       # steps per loop
       controller:
         steps_per_round: 6
     ```
   - *(Current default in `config/experiment.yaml` is `rounds: 4`, `steps_per_round: 15`, `step_ceiling: 60`, `repeats: 1`.)*

---

## Step 2: Choose How to Run (Docker Compose vs. Local Python)

You can run all commands in one of two ways:

### Option A: Using Docker Compose (Recommended for All OSes)
Everything runs inside a standardized Linux container that talks to your host's Docker daemon via `/var/run/docker.sock`.

```bash
cd swe_policy_eval
docker compose build
```

Whenever a step below shows `<RUN>`, replace `<RUN>` with:
```bash
docker compose run --rm swe-policy-eval
```

### Option B: Using Local Python (`.venv`)
If you prefer running Python directly on your host machine:

- **Windows (PowerShell)**:
  ```powershell
  cd swe_policy_eval
  python -m venv .venv
  .venv\Scripts\Activate.ps1
  pip install -e .
  ```
- **macOS / Linux (bash/zsh)**:
  ```bash
  cd swe_policy_eval
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -e .
  ```

Whenever a step below shows `<RUN>`, simply omit `<RUN>` (run `python ...` directly in your activated `.venv`).

---

## Step 3: Run the Smoke Tests

Before spending any API credits or pulling SWE-bench containers, verify that the round loop, both policies, the cost tracker, and patch extraction pass all unit/smoke tests:

- **Docker Compose**:
  ```bash
  docker compose run --rm swe-policy-eval python -m pytest tests -v
  ```
- **Local `.venv`**:
  ```bash
  python -m pytest tests -v
  ```

**Expected output**: `6 passed`.

---

## Step 4: Select Tasks (`select_tasks.py`)

Note: `results/tasks.json` is already generated for `seed: 1234` (10 pilot tasks + 490 ordered main candidates). You only need to re-run this step if you change `seed` or `inclusion` in `config/experiment.yaml`:

- **Docker Compose**:
  ```bash
  docker compose run --rm swe-policy-eval python scripts/select_tasks.py
  ```
- **Local `.venv`**:
  ```bash
  python scripts/select_tasks.py
  ```

This writes `results/tasks.json` with:
- `pilot_ids`: 10 task IDs for the pilot stage.
- `main_ids_ordered`: 490 remaining task IDs in seeded random order (disjoint from `pilot_ids`).

---

## Step 5: Pre-Pull SWE-bench Docker Images (`prepull.py`)

Pre-pulling the task images prevents image downloads from slowing down or timing out your first experiment run:

- **Docker Compose**:
  ```bash
  docker compose run --rm swe-policy-eval python scripts/prepull.py --stage pilot --platform linux/amd64
  ```
- **Local `.venv`**:
  ```bash
  python scripts/prepull.py --stage pilot --platform linux/amd64
  ```

---

## Step 6: Run the Pilot Stage (`run_experiment.py --stage pilot`)

### 6.1 Dry Run (Check Run Count and Budget Ceiling)
Always run `--dry-run` first to confirm how many runs will execute and the worst-case USD spend:

- **Docker Compose**:
  ```bash
  docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage pilot --dry-run
  ```
- **Local `.venv`**:
  ```bash
  python scripts/run_experiment.py --stage pilot --dry-run
  ```

**Expected output**:
```text
10 tasks x 2 policies x 1 repeats = 20 runs; worst case $20 vs global cap $30
```

### 6.2 Execute the Pilot Run
- **Docker Compose**:
  ```bash
  docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage pilot --workers 2
  ```
- **Local `.venv`**:
  ```bash
  python scripts/run_experiment.py --stage pilot --workers 2
  ```

**What happens during the run**:
- For each task, `fixed` and `controller` are run back-to-back in randomized order, each in a **fresh Docker container**.
- Every LLM call is logged to `results/<experiment>/cost_ledger.jsonl`.
- Every round is logged to `results/<experiment>/pilot/<policy>/<instance_id>/rep0/rounds.jsonl`.
- When a run finishes, `result.json` and `trajectory.json` are saved in that run directory.
- **Resumability**: If interrupted (Ctrl+C, network drop, or Docker restart), simply re-run the exact same command. Completed runs (`result.json` present) are automatically skipped, and previous spend is restored from `cost_ledger.jsonl`.

---

## Step 7: Evaluate Hidden Tests & Analyze Pilot Results

### 7.1 Run Hidden SWE-bench Evaluation (`evaluate.py`)
During the agent run, only tests the agent itself executed were visible. Now grade the final patches against the official hidden `FAIL_TO_PASS` and `PASS_TO_PASS` test suites:

- **Docker Compose**:
  ```bash
  docker compose run --rm swe-policy-eval python scripts/evaluate.py --stage pilot --max-workers 4
  ```
- **Local `.venv`**:
  ```bash
  python scripts/evaluate.py --stage pilot --max-workers 4
  ```

This runs `swebench.harness.run_evaluation` and writes `results/<experiment>/eval/resolved.json`.

### 7.2 Compute Pilot Metrics (`analyze.py`)
- **Docker Compose**:
  ```bash
  docker compose run --rm swe-policy-eval python scripts/analyze.py --stage pilot
  ```
- **Local `.venv`**:
  ```bash
  python scripts/analyze.py --stage pilot
  ```

This prints and saves `results/<experiment>/summary_pilot.json`, including:
- Per-policy `resolve_rate`, `mean_cost_usd`, `cost_per_resolved_usd`, token counts, mean rounds/steps, empty patch rate, and `stop_reasons` breakdown.
- Paired task-level differences (`paired_controller_minus_fixed`) with 95% bootstrap confidence intervals (`resolve_rate_diff_ci95`, `cost_diff_ci95`).
- Within-task variability across repeats (`variability`).

---

## Step 8: Choose $N$ Tasks After the Pilot & Run 15 Times per Method (`--stage main`)

The number of SWE-bench tasks $N$ (`--n-main <N>`) is **chosen after reading `summary_pilot.json`** (based on the pilot's per-run cost and variability). Once you pick $N$, running those $N$ tasks **15 times per method** (`--repeats 15`) executes:
$$N \text{ tasks} \times 2 \text{ methods (fixed, controller)} \times 15 \text{ repeats} = 30N \text{ total runs}$$

*(Remember to update `limits.global_cap_usd` in `config/experiment.yaml` so it covers the budget for $30N$ runs.)*

### 8.1 Pre-Pull the $N$ Main Task Images
Replace `<N>` with the number of tasks you decide to use:
```bash
# Docker Compose:
docker compose run --rm swe-policy-eval python scripts/prepull.py --stage main --n-main <N> --platform linux/amd64

# Or Local .venv:
python scripts/prepull.py --stage main --n-main <N> --platform linux/amd64
```

### 8.2 Dry-Run, Execute, Evaluate, and Analyze ($N$ Tasks $\times$ 15 Repeats per Method)
```bash
# 1. Dry-run (verifies N tasks x 2 policies x 15 repeats = 30*N runs and worst-case cost vs global_cap_usd)
docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage main --n-main <N> --repeats 15 --dry-run

# 2. Run the main experiment (N tasks, 15 runs per method across Fixed Retry and Controller)
docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage main --n-main <N> --repeats 15 --workers 4

# 3. Evaluate all generated patches with the hidden SWE-bench test suite
docker compose run --rm swe-policy-eval python scripts/evaluate.py --stage main --max-workers 4

# 4. Generate final statistical summary (task-level means over the 15 repeats, paired bootstrap CIs, & within-task variance)
docker compose run --rm swe-policy-eval python scripts/analyze.py --stage main
```

Final results are saved to `results/<experiment>/summary_main.json`.

---

## Output Directory Reference

```text
results/
├── tasks.json                                      # Seeded pilot (10) + main (490) task IDs
└── policy_comparison_v1/                           # <experiment> name from config/experiment.yaml
    ├── run_config_pilot.json                       # Snapshot of config & IDs at pilot start
    ├── run_config_main.json                        # Snapshot of config & IDs at main start
    ├── cost_ledger.jsonl                           # Every LLM call's tokens, cost, and cumulative totals
    ├── summary_pilot.json                          # Pilot metrics + paired bootstrap CIs
    ├── summary_main.json                           # Main metrics + paired bootstrap CIs
    ├── eval/
    │   ├── resolved.json                           # Resolved instance IDs per (stage, policy, repeat)
    │   └── <stage>_<policy>_rep<k>.jsonl           # Predictions files passed to swebench harness
    └── <stage>/                                    # "pilot" or "main"
        └── <policy>/                               # "fixed" or "controller"
            └── <instance_id>/
                └── rep<k>/
                    ├── rounds.jsonl                # Per-round state, visible checks, patch SHA, decision
                    ├── trajectory.json             # Full mini-swe-agent message trajectory
                    ├── result.json                 # Final patch, stop_reason, tokens, cost, wall_secs
                    └── error.json                  # Only present if an infra/container error occurred
```

---

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `failed to connect to the docker API` | Docker Desktop / daemon is not running. | Start **Docker Desktop** and wait until the engine status is green. |
| `Authentication failed` (HTTP 401) | `OPENROUTER_API_KEY` is missing or invalid. | Check `swe_policy_eval/.env` and ensure `OPENROUTER_API_KEY` is set without extra spaces. |
| `No valid cost information available ... cost 0.0` | Using a `:free` OpenRouter model that reports `$0` cost. | Switch to a paid model endpoint so budget caps work, or set `MSWEA_COST_TRACKING=ignore_errors` in `.env` (note: USD caps will see `$0`). |
| `global cap $X reached` | Total experiment spend hit `limits.global_cap_usd`. | Raise `global_cap_usd` in `config/experiment.yaml` and re-run `run_experiment.py` (completed runs are skipped automatically). |
| Disk space filling up during large runs | Many `sweb.eval.x86_64.*` Docker images cached on disk. | Run `docker image prune -a` between batches if disk space is tight. |
