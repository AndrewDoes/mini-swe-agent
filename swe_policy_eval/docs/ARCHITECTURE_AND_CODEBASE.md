# Pre-Thesis Codebase & Architecture Documentation (`swe_policy_eval`)

## 1. Pre-Thesis Research Context

This codebase implements a controlled experimental evaluation for a **Pre-Thesis study on Agentic Loop Engineering** on **SWE-bench Verified** (built on top of `mini-swe-agent` v2.x).

Specifically, the experiment compares **Fixed Retry (`fixed`)** against **Progress-Aware Loop Engineering (`controller`)** across three configurable dimensions while holding the underlying LLM, system prompt, bash toolset, and container environment strictly constant:

| Experimental Parameter | Symbol | Meaning | Where It Is Configured | Current Default in Code |
| :--- | :---: | :--- | :--- | :--- |
| **Test runs per method** | **$15$** | How many times the test suite is repeated for each method (`fixed` and `controller`) | `--repeats 15` (or `repeats:` in `config/experiment.yaml`) | `repeats: 1` |
| **Tasks per test run** | **$n$** | How many SWE-bench Verified tasks are executed in each test run *(to be decided)* | `--n-main <n>` in `run_experiment.py` (or `--pilot-size <n>` in `select_tasks.py`) | `10` for pilot; `--n-main` required for main |
| **Max loops per task** | **$k$** | Maximum outer-loop iterations ("rounds") / steps allowed per task *(to be decided, e.g. ~10)* | `policies.fixed.rounds` & `limits.step_ceiling` in `config/experiment.yaml` | `rounds: 4`, `steps_per_round: 15`, `step_ceiling: 60` |

### The Two Competing Methods:

1. **Fixed Retry Schedule (`fixed` — Baseline Arm)**
   A pre-committed, state-blind multi-round retry loop. It executes $R$ fixed rounds of $K$ agent steps ($4 \times 15 = 60$ steps) and injects the same generic continuation prompt between rounds regardless of whether tests are already passing, failing, or the patch is stuck.
2. **Progress-Aware Loop Controller (`controller` — Adaptive Arm)**
   A deterministic, rule-based feedback controller that inspects *visible* progress signals at the end of every round (non-test source diff SHA, line count, modified files, visible test-runner execution outcomes, and cumulative dollar spend). Based on these signals, it dynamically decides:
   - **Whether to terminate early** (e.g., when tests pass after the minimum round threshold, when the patch stagnates across `patience` rounds, or when approaching the budget guard), saving unnecessary token/dollar spend and avoiding regression edits.
   - **How many steps to allocate to the next round** (`min_steps`, `steps_per_round`, or `max_steps`).
   - **What targeted feedback ("nudge") to inject** (e.g., prompting the agent to write an initial edit if the diff is empty, attaching the failing test output tail when tests fail, or asking the agent to run verification tests when a patch exists without test runs).

Because the controller is strictly rule-based and makes **zero extra LLM calls**, 100% of the recorded token and dollar cost in both arms belongs to the coding agent itself.

---

## 2. Repository Directory & File Map

```text
mini-swe-agent/
├── Original/                                 # Upstream mini-swe-agent v2.4.6 reference source
└── swe_policy_eval/                          # Main Pre-Thesis experimental harness
    ├── config/
    │   └── experiment.yaml                   # Master configuration (seed, model, caps, policy params)
    ├── src/
    │   └── swe_policy_eval/                  # Core Python package
    │       ├── __init__.py                   # Package docstring
    │       ├── rounds_agent.py               # Outer round-loop harness extending DefaultAgent
    │       ├── recording_env.py              # Environment wrapper capturing per-round bash commands
    │       ├── check_parsing.py              # Detector & parser for visible test-runner commands
    │       ├── cost_tracker.py               # Thread-safe per-call token/USD ledger & hard budget caps
    │       └── policies/                     # Loop-engineering policy implementations
    │           ├── __init__.py               # Policy registry & build_policy() factory
    │           ├── base.py                   # Data structures (PatchInfo, CheckRun, RoundState, Decision, Policy)
    │           ├── fixed.py                  # FixedPolicy (state-blind R x K retry schedule)
    │           └── controller.py             # ControllerPolicy (progress-aware adaptive loop controller)
    ├── scripts/                              # CLI entrypoints for the 5-stage experimental workflow
    │   ├── select_tasks.py                   # Step 1: Deterministic, seeded task sampling from SWE-bench
    │   ├── prepull.py                        # Step 2: Pre-downloads SWE-bench Docker images
    │   ├── run_experiment.py                 # Step 3: Executes (task, policy, repeat) runs in fresh containers
    │   ├── evaluate.py                       # Step 4: Runs official swebench hidden-test grading
    │   └── analyze.py                        # Step 5: Computes metrics, paired bootstrap CIs, & variability
    ├── tests/
    │   └── test_smoke.py                     # Docker-free unit & integration smoke tests (6 test suites)
    ├── docs/
    │   ├── HOW_TO_RUN.md                     # Step-by-step execution manual
    │   └── ARCHITECTURE_AND_CODEBASE.md      # This architecture & source-code reference
    ├── results/                              # Experiment outputs, task lists, trajectories, and summaries
    │   ├── tasks.json                        # Seeded pilot (10) & ordered main (490) SWE-bench instance IDs
    │   └── smoke/                            # Sample 1-task smoke artifacts
    ├── logs/                                 # Raw swebench evaluation logs per container
    ├── Dockerfile                            # Portable Linux runner container with Docker CLI & dependencies
    ├── docker-compose.yml                    # Compose service mounting /var/run/docker.sock & results/
    ├── .dockerignore                         # Excludes .venv, caches, and heavy logs from Docker build context
    ├── .env.example                          # Template for API keys (OPENROUTER_API_KEY, etc.)
    ├── pyproject.toml                        # Python package metadata and dependencies
    └── README.md                             # Project overview and quickstart
```

---

## 3. Detailed Breakdown of Every Folder and Source File

### 3.1 `config/` — Experiment Configuration

#### `config/experiment.yaml`
The single source of truth for all experimental parameters:
- **`experiment`**: Identifier string (`policy_comparison_v1`) used to name the output directory (`results/policy_comparison_v1/`).
- **`seed`**: Integer seed (`1234`) ensuring deterministic task sampling in `select_tasks.py`, deterministic interleaving of `fixed` vs. `controller` runs in `run_experiment.py`, and reproducible bootstrap confidence intervals in `analyze.py`.
- **`dataset`**: HuggingFace dataset identifier (`SWE-bench/SWE-bench_Verified`, split `test`, 500 verified GitHub issues).
- **`inclusion`**: Optional pre-sampling filters (`difficulty` list or `exclude_regex` on `instance_id`).
- **`model`**: Specifies `class: openrouter`, `name: qwen/qwen3-coder-30b-a3b-instruct`, and `kwargs: {max_tokens: 8192}`.
- **`limits`**: Equal hard ceilings enforced on both policies:
  - `global_cap_usd: 30.0` — Stops the entire experiment if total spend across all runs approaches this limit.
  - `per_run_cap_usd: 1.0` — Stops an individual `(task, policy, repeat)` run if its spend reaches \$1.00.
  - `reserve_per_call_usd: 0.02` — Buffer added to pre-call budget checks so a call cannot significantly overshoot the cap.
  - `step_ceiling: 60` — Maximum total agent steps (LLM call + bash execution) allowed in a single run.
- **`repeats`**: Number of repeated trials per `(task, policy)` pair to measure run-to-run variance.
- **`policies.fixed`**:
  - `rounds: 4`, `steps_per_round: 15` ($4 \times 15 = 60$ steps).
- **`policies.controller`**:
  - `steps_per_round: 15` (initial/default step allocation when no patch exists yet)
  - `min_steps: 6` (short verification burst when a patch exists without test runs, or for edge-case confirmation)
  - `max_steps: 20` (extended debugging burst when visible tests are actively failing)
  - `min_rounds_before_stop: 2` (prevents premature termination on round 1 before edge cases are checked)
  - `patience: 2` (number of consecutive rounds with an identical patch SHA before declaring stagnation)
  - `cost_stop_frac: 0.9` (stops between rounds if cumulative cost reaches 90% of `per_run_cap_usd`)
  - `stop_on_empty_stagnation: true` (stops if the agent produces no source edits for `patience` consecutive rounds)
- **`environment`**: Docker execution settings (`pull_timeout: 3600`, `run_args: ["--rm", "--platform", "linux/amd64"]`).

---

### 3.2 `src/swe_policy_eval/` — Core Harness & Instrumentation

#### 1. `src/swe_policy_eval/rounds_agent.py`
Implements **`RoundsAgent`**, a subclass of `mini-swe-agent`'s `DefaultAgent` that wraps standard step-by-step execution inside an outer **round loop**:
- **`_DIFF_CMD`**: A harness-side `git diff` command executed inside `/testbed` that extracts modifications to tracked **source files only**, explicitly excluding test files (`**/tests/**`, `**/test_*.py`, `**/*_test.py`, `**/conftest.py`) and `patch.txt`. This prevents test edits or reproduction scripts from masquerading as source fixes.
- **`RoundsAgent.run_policy(task)`**:
  1. Initializes the system prompt and task description (`instance_template`) identically for both policies.
  2. Queries `self.policy.first_round_steps()` for Round 1's step budget.
  3. In each round:
     - Calls `_run_round(steps_budget)` to run up to `steps_budget` standard `mini-swe-agent` steps.
     - Drains the recorded bash actions from `RecordingEnv` (`self.renv.drain()`).
     - Parses any visible test-runner commands executed during the round via `extract_checks(records)`.
     - Takes a source-only git diff snapshot via `_snapshot_patch()` (computing SHA-1 hash, line count, and touched file list).
     - Queries cumulative token and USD totals from `CostTracker`.
     - Packages all visible metrics into a `RoundState` object.
     - If the agent exited on its own (`Submitted`, `LimitsExceeded`, `RepeatedFormatError`), records `Decision("stop", reason="agent_exit:<status>")` — the policy cannot veto an explicit agent submission or hard limit.
     - Otherwise, calls `self.policy.decide(state)` to get a `Decision` (`"continue"` or `"stop"`, `next_steps`, `nudge`, `reason`).
     - Appends a structured JSON record of the round to `rounds.jsonl` via `_log_round()`.
     - If `decision.action == "continue"`, sets `steps_budget = decision.next_steps` and appends `decision.nudge` as a `"user"` message in the conversation history.
  4. Calls `_finalize()` when the loop ends:
     - Uses the agent's own submitted patch if `exit_status == "Submitted"` and non-empty (`patch_source = "agent_submission"`).
     - Otherwise falls back to the harness's source-only `_DIFF_CMD` (`patch_source = "harness_diff"`). Same rule for both policies.
     - Saves `trajectory.json` and returns the final run summary dictionary.

#### 2. `src/swe_policy_eval/recording_env.py`
Implements **`RecordingEnv`**, a transparent proxy around the underlying execution environment (`DockerEnvironment` in real runs, `LocalEnvironment` in smoke tests):
- **`execute(action)`**: Intercepts every bash command run by the agent, measures wall-clock duration (`secs`), captures `command`, `returncode`, and `output` (or any raised exception like `Submitted`), and appends it to `self.records`.
- **`drain()`**: Returns and clears the list of action records for the round that just finished.
- **`raw(command)`**: Executes a harness-side command (such as `_DIFF_CMD` to inspect the git diff between rounds) **without** recording it in `self.records` and **without** exposing it to the agent's message history.

#### 3. `src/swe_policy_eval/check_parsing.py`
Detects and parses **visible test runs** among the commands the agent chose to execute during a round (without ever running or leaking SWE-bench's hidden evaluation tests):
- **`is_test_command(command)`**: Splits compound shell commands on `&&`, `;`, `||`, `|` and checks if any sub-command invokes a test runner (`pytest`, `py.test`, `-m pytest`, `runtests.py`, `manage.py test`, `tox`, `-m unittest`, `nosetests`, `bin/test`) while ignoring search/file commands (`grep`, `rg`, `cat`, `sed`, `find`, `ls`, `git`, `echo`, etc.).
- **`parse_check_output(command, output, returncode)`**: Parses `pytest` and `unittest`/Django test output using regular expressions to extract counts of `passed`, `failed`, `errors`, and `ran` tests, plus the last 25 lines (`tail`) of output. Sets `ok`:
  - `True` if `returncode == 0`, `failed + errors == 0`, and at least 1 test passed/ran.
  - `False` if `returncode != 0` or any failures/errors were parsed.
  - `None` if no tests ran (e.g., `pytest` exit code 5, `"no tests ran"`, `"collected 0 items"`, or exit 0 with unparseable test counts).
- **`extract_checks(records)`**: Filters a round's action records with `is_test_command()` and returns a list of `CheckRun` objects.

#### 4. `src/swe_policy_eval/cost_tracker.py`
Enforces hard dollar budgets and maintains an append-only per-call ledger (`cost_ledger.jsonl`):
- **`GlobalBudgetExceeded`**: Exception raised when `global_cost + reserve > global_cap`, halting the entire experiment across threads.
- **`CostTracker`**:
  - Thread-safe (`threading.Lock()`) tracker supporting multi-worker execution and automatic resume (loads existing `cost_ledger.jsonl` on startup to restore `global_cost`).
  - **`check(run_id)`**: Called *before* every LLM request. Raises `GlobalBudgetExceeded` if the global cap would be breached, or `minisweagent.exceptions.LimitsExceeded` (`exit_status: "RunBudgetExceeded"`) if the current run's spend plus `reserve_per_call_usd` exceeds `per_run_cap_usd`.
  - **`record(...)`**: Called *after* every LLM response (including `FormatError` responses that were still billed by the API). Updates per-run and global totals and writes a JSON line with timestamp, `run_id`, call index, model name, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd`, `run_cost_usd`, and `global_cost_usd`.
- **`TrackedModel`**: Proxy wrapper around any `mini-swe-agent` model that automatically invokes `tracker.check(run_id)` before `model.query()` and `tracker.record(...)` after `model.query()`.

---

### 3.3 `src/swe_policy_eval/policies/` — Loop Engineering Policies

#### 1. `src/swe_policy_eval/policies/base.py`
Defines the shared data structures and abstract interface used by both policies:
- **`PatchInfo`**: Snapshot of the non-test source diff at the end of a round (`nonempty: bool`, `sha: str`, `n_lines: int`, `files: list[str]`).
- **`CheckRun`**: Parsed result of a single visible test command (`command`, `returncode`, `ok`, `passed`, `failed`, `errors`, `ran`, `tail`).
- **`RoundState`**: Complete observable state passed to `policy.decide(state)` at the end of round `round_idx`:
  - Step counters: `round_idx`, `steps_this_round`, `total_steps`, `step_ceiling`, and property `remaining_steps`.
  - Cost/token counters: `cum_cost`, `round_cost`, `run_cost_cap`, `cum_input_tokens`, `cum_output_tokens`.
  - Progress signals: `patch` (`PatchInfo`), `prev_patch_shas` (list of patch SHAs from prior rounds), `checks` (list of `CheckRun` from this round), `n_actions`, and `history`.
- **`Decision`**: Output of `policy.decide(state)` containing `action` (`"continue"` or `"stop"`), `next_steps: int`, `nudge: str`, and `reason: str`.
- **`Policy` (ABC)**: Abstract base class requiring `first_round_steps() -> int` and `decide(state: RoundState) -> Decision`.

#### 2. `src/swe_policy_eval/policies/fixed.py`
Implements **`FixedPolicy`** (the **Fixed Retry** arm):
- Ignores `state.patch`, `state.checks`, and `state.cum_cost`.
- Stops only when `state.round_idx >= self.rounds` (`reason="fixed_schedule_complete"`) or `state.remaining_steps <= 0` (`reason="step_ceiling"`).
- Otherwise always continues for `min(steps_per_round, state.remaining_steps)` steps with the static prompt:
  > *"Continue working on the task. Round {next_round} of {rounds} is starting. When you are confident the fix is complete, submit it as instructed."*

#### 3. `src/swe_policy_eval/policies/controller.py`
Implements **`ControllerPolicy`** (the **Progress-Aware Loop Engineering** arm, aligned with **Section III-E Table I** of the paper):
Evaluates rules in strict priority order at the end of every round:

| Priority | Observed Situation (Table I) | Action | Next Steps | Reason & Controller Behavior |
| :---: | :--- | :---: | :---: | :--- |
| **1** | `s.remaining_steps <= 0` | `stop` | `0` | `step_ceiling` — Hard step limit reached. |
| **2** | **All agent-visible checks pass** (`patch.nonempty` AND `tests_pass` AND `round_idx >= min_rounds_before_stop`) | `stop` | `0` | `tests_pass` — Stop and submit for final evaluation. |
| **3a** | **Remaining budget below threshold** (1st time `cum_cost >= cost_stop_frac * run_cost_cap`) | `continue` | `min_steps` (6) | `low_budget_final_attempt` — Grants one final targeted attempt before stopping. |
| **3b** | **Remaining budget below threshold** (after final attempt) | `stop` | `0` | `budget_guard` — Stops run after the final targeted attempt. |
| **4** | **Regression detected** (`passed` dropped or previously passing suite broke) | `continue` | `min_steps` (6) | `regression_detected` (`revert_to_best=True`) — Automatically reverts working tree to best checkpoint and requests a narrower fix. |
| **5** | **No measurable progress for 3 rounds** (`patience: 3`) | `stop` | `0` | `patch_stagnant` (if patch non-empty) or `no_progress` (if patch empty). |
| **6** | **Repository tests passing increases, no regressions** | `continue` | `steps_per_round` (15) / `min_steps` (6) | `progress_continue` — Continues with next round to finish remaining failures or verify edge cases. |
| **7** | **Same failing test or error repeats for 2 rounds** (`repeat_failure_limit: 2`) | `continue` | `max_steps` (20) | `repeated_failure_new_diagnosis` — Replaces repair prompt and instructs agent to form a new root-cause diagnosis. |
| **8a** | `not patch.nonempty` | `continue` | `steps_per_round` (15) | `empty_patch` — Nudges agent to locate root cause and make a minimal edit to non-test source files. |
| **8b** | `tests_fail` (first occurrence of failure) | `continue` | `max_steps` (20) | `tests_failing` — Allocates extra steps and attaches failing test output tail (`failing.tail`). |
| **8c** | `patch.nonempty` AND `not s.checks` | `continue` | `min_steps` (6) | `unverified_patch` — Patch exists but no tests were run this round; nudges agent to verify it. |
| **8d** | `patch.nonempty` AND tests passed on Round 1 (`round_idx < min_rounds_before_stop`) | `continue` | `min_steps` (6) | `confirm_before_stop` — Asks for a short edge-case and regression check before stopping. |

#### 4. `src/swe_policy_eval/policies/__init__.py`
Exports `REGISTRY = {"fixed": FixedPolicy, "controller": ControllerPolicy}` and the factory function `build_policy(name, params)`.

---

### 3.4 `scripts/` — Experiment Workflow Scripts

1. **`scripts/select_tasks.py`**:
   Loads `SWE-bench/SWE-bench_Verified` from HuggingFace, applies any `inclusion` filters from `config/experiment.yaml`, sorts `instance_id`s canonically, shuffles them with `random.Random(cfg["seed"])`, and splits them into `pilot_ids` (first 10) and `main_ids_ordered` (remaining 490). Writes `results/tasks.json` along with a SHA-256 hash of the candidate pool.
2. **`scripts/prepull.py`**:
   Reads `results/tasks.json`, resolves the official SWE-bench Docker image name (`docker.io/swebench/sweb.eval.x86_64.<id>:latest`) for each selected task in `--stage pilot` or `--stage main --n-main <N>`, and runs `docker pull` sequentially so image downloads do not happen mid-experiment.
3. **`scripts/run_experiment.py`**:
   The main experiment runner:
   - Loads `.env` and `config/experiment.yaml`.
   - Builds the job list for `(rep, instance_id, policy)`, shuffling the order of `["fixed", "controller"]` per `(rep, instance_id)` using a seeded RNG so both arms are interleaved fairly.
   - For each job (`run_one`):
     - Skips if `result.json` already exists (making runs safely resumable).
     - Starts a **fresh Docker container** via `get_sb_environment()`.
     - Wraps the model in `TrackedModel` and instantiates `RoundsAgent`.
     - Executes `agent.run_policy(problem_statement)`, writes `rounds.jsonl`, `trajectory.json`, and `result.json` (or `error.json` on container/infra failure), and cleans up the container in a `finally` block.
4. **`scripts/evaluate.py`**:
   Groups all `result.json` files by `(policy, repeat)`, writes `<stage>_<policy>_rep<k>.jsonl` prediction files, invokes the official `swebench.harness.run_evaluation` module in Docker containers against the hidden `FAIL_TO_PASS` and `PASS_TO_PASS` tests, and aggregates the passing `instance_id`s into `results/<experiment>/eval/resolved.json`.
5. **`scripts/analyze.py`**:
   Joins `result.json` files with `eval/resolved.json` and computes all metrics and statistical tests from **Sections III-I and III-J**:
   - **Per-policy metrics (Section III-I)**:
     - `resolve_rate` and `success_rate_pct` ($\text{successful runs} / \text{total runs} \times 100\%$)
     - `cost_per_resolved_usd` ($\text{total API cost} / \text{successful runs}$, or `null` if 0 successes)
     - `false_completion_rate` (runs reported complete via `Submitted` or `tests_pass` that fail final checks, divided by total runs reported complete) and `agent_false_submission_rate`
     - `regression_rate` (share of evaluated runs breaking previously passing `PASS_TO_PASS` tests) and `in_loop_regression_rate`
     - `budget_exhaustion_rate` (share of runs exhausting dollar/step ceilings without verified success)
     - `mean_cost_usd`, `total_cost_usd`, `mean_input_tokens`, `mean_output_tokens`, `mean_rounds`, `mean_steps`, `empty_patch_rate`, and `stop_reasons`.
   - **Paired statistical comparison (`paired_controller_minus_fixed`, Section III-J)**:
     - **McNemar's test** (`mcnemar_test`) on paired binary task resolution outcomes (`p_value_exact`, continuity-corrected $\chi^2$, and Cohen's $g$ effect size).
     - **Wilcoxon signed-rank test** (`wilcoxon_signed_rank`) on paired task costs (`cost_wilcoxon`), step counts (`steps_wilcoxon`), and round counts (`rounds_wilcoxon`), reporting $W$, two-sided $p$-value, and matched-pairs rank-biserial correlation $r$ alongside Cohen's $d$.
     - **10,000-resample bootstrap 95% confidence intervals** (`resolve_rate_diff_ci95`, `cost_diff_ci95`, `steps_diff_ci95`, `rounds_diff_ci95`).
     - **Failure analysis case lists (`failure_analysis_cases`)**: Task IDs where `controller_helped`, `controller_hurt`, `both_resolved`, and `neither_resolved`.
   - **Repeat variability (`variability`)**: Within-task cost standard deviation and share of tasks with mixed resolve outcomes across repeats.

---

### 3.5 `tests/` — Verification Suite

#### `tests/test_smoke.py`
Contains 6 self-contained, Docker-free tests using `DeterministicModel` and a temporary local Git repository (`make_repo()`):
1. `test_check_parsing`: Verifies `is_test_command()` and `parse_check_output()` across `pytest`, Django `runtests.py`, `grep`, passing/failing runs, and exit code 5 (`no tests ran`).
2. `test_controller_stops_when_tests_pass`: Verifies that `ControllerPolicy` nudges on an unverified patch in Round 1 (`unverified_patch`) and stops immediately at the end of Round 2 once `pytest` passes (`tests_pass`), extracting the fix via `harness_diff`.
3. `test_fixed_runs_full_schedule_even_if_tests_pass`: Verifies that `FixedPolicy` continues running all scheduled rounds/steps even after the bug is fixed and tests pass in Round 1 (`fixed_schedule_complete`).
4. `test_agent_submission_is_used_and_policy_cannot_veto`: Verifies that when the agent executes `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && ...`, `RoundsAgent` immediately halts and uses `agent_submission`.
5. `test_per_run_cost_cap_is_hard`: Verifies that `CostTracker` terminates a run with `exit_status == "RunBudgetExceeded"` as soon as the next call would exceed `per_run_cap_usd`.
6. `test_controller_branches`: Unit-tests every decision branch of `ControllerPolicy` (`unverified_patch`, `tests_failing`, `patch_stagnant`, `no_progress`, `empty_patch`, `budget_guard`, `step_ceiling`, and remaining-step clamping).

---

## 4. Running the Pre-Thesis Experiment ($N$ Tasks $\times$ 15 Runs per Method)

Because `results/tasks.json` stores a seeded, shuffled list of **10 pilot tasks** (`pilot_ids`) and **490 disjoint main candidate tasks** (`main_ids_ordered`), you do not need to decide $N$ upfront:
1. First run the **pilot** (`--stage pilot`, which uses the 10 pilot tasks) to measure per-run cost and variance.
2. Decide how many tasks $N$ (`--n-main <N>`) to include in the main study.
3. Run those $N$ tasks **15 times per method** (`--repeats 15`), which executes $N \text{ tasks} \times 2 \text{ policies} \times 15 \text{ repeats} = 30N \text{ runs}$ (`rep0` through `rep14` for each task and policy), and `analyze.py` will automatically average over the 15 repeats per task and report within-task variance (`mean_within_task_cost_sd` and `share_tasks_with_mixed_outcomes`).

*(Be sure to raise `limits.global_cap_usd` in `config/experiment.yaml` to cover the $30N$ runs before starting the main stage.)*

### Using Docker Compose (Recommended)
```bash
cd swe_policy_eval

# 1. Build the runner image & verify smoke tests
docker compose build
docker compose run --rm swe-policy-eval python -m pytest tests -v

# 2. Pre-pull the N chosen SWE-bench task Docker images (replace <N> with your chosen task count)
docker compose run --rm swe-policy-eval python scripts/prepull.py --stage main --n-main <N> --platform linux/amd64

# 3. Dry-run to confirm N tasks x 2 policies x 15 repeats = 30*N runs and check budget cap
docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage main --n-main <N> --repeats 15 --dry-run

# 4. Run the N tasks 15 times per method (Fixed Retry vs. Controller)
docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage main --n-main <N> --repeats 15 --workers 4

# 5. Grade all generated patches (rep0..rep14 for each policy) with official hidden SWE-bench tests
docker compose run --rm swe-policy-eval python scripts/evaluate.py --stage main --max-workers 4

# 6. Generate final Pre-Thesis comparison metrics, repeat variability, & 95% bootstrap CIs
docker compose run --rm swe-policy-eval python scripts/analyze.py --stage main
```

### Using Local Python (`.venv`)
```powershell
cd swe_policy_eval
.venv\Scripts\python.exe scripts/prepull.py --stage main --n-main <N> --platform linux/amd64
.venv\Scripts\python.exe scripts/run_experiment.py --stage main --n-main <N> --repeats 15 --dry-run
.venv\Scripts\python.exe scripts/run_experiment.py --stage main --n-main <N> --repeats 15 --workers 4
.venv\Scripts\python.exe scripts/evaluate.py --stage main --max-workers 4
.venv\Scripts\python.exe scripts/analyze.py --stage main
```
