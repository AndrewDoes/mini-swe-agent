# swe_policy_eval

Fixed-schedule policy vs. adaptive controller, on SWE-bench Verified, built on
[mini-swe-agent](https://github.com/swe-agent/mini-swe-agent) (v2.x).

> **Documentation**:
> - **Step-by-Step Execution Guide**: [`docs/HOW_TO_RUN.md`](docs/HOW_TO_RUN.md)
> - **Pre-Thesis Architecture & Folder/Source Breakdown**: [`docs/ARCHITECTURE_AND_CODEBASE.md`](docs/ARCHITECTURE_AND_CODEBASE.md)

## Where things live

```
swe_policy_eval/
├── config/experiment.yaml          # seed, model, budget caps, both policies' parameters
├── src/swe_policy_eval/
│   ├── policies/
│   │   ├── base.py                 # Policy interface, RoundState, Decision
│   │   ├── fixed.py                # >>> FIXED POLICY
│   │   ├── controller.py           # >>> CONTROLLER
│   │   └── __init__.py             # build_policy("fixed" | "controller", params)
│   ├── rounds_agent.py             # DefaultAgent + round loop; asks the policy after each round
│   ├── cost_tracker.py             # per-call ledger (tokens, $) + hard caps
│   ├── recording_env.py            # records each round's actions for logging / test parsing
│   └── check_parsing.py            # finds and parses test runs among the agent's commands
├── scripts/
│   ├── select_tasks.py             # seeded pilot + main, disjoint, IDs saved
│   ├── prepull.py                  # pre-pulls SWE-bench Docker images for pilot or main
│   ├── run_experiment.py           # both policies, fresh containers, repeats, resumable
│   ├── evaluate.py                 # hidden tests via the official swebench harness
│   └── analyze.py                  # metrics, paired bootstrap CIs, variability
├── tests/test_smoke.py             # Docker-free tests (scripted model, local git repo)
├── Dockerfile                      # Portable runner container with Docker CLI + dependencies
├── docker-compose.yml              # Mounts /var/run/docker.sock + results/ + .env
└── results/                        # created at run time
```

## Procedure mapping (Section H)

| Step                                          | Where                                                          |
| --------------------------------------------- | -------------------------------------------------------------- |
| 1. Inclusion criteria, budget ceiling         | `config/experiment.yaml` (`inclusion`, `limits`)               |
| 2. Both policies + cost tracker with hard cap | `policies/`, `cost_tracker.py`                                 |
| 3. Pilot, then freeze parameters              | `run_experiment.py --stage pilot`, then edit/freeze the config |
| 4. Fresh container per (task, policy, repeat) | `get_sb_environment()` called inside every `run_one`           |
| 5. Repeats                                    | `--repeats` / `repeats:` in config                             |
| 6. JSON log per round                         | `<run_dir>/rounds.jsonl` and `result.json["rounds_log"]`       |
| 7. Hidden-test evaluation, metrics            | `evaluate.py`, `analyze.py`                                    |

## Run it (Option A: Docker Compose — works on Windows, macOS, and Linux)

1. Copy `.env.example` to `.env` and set your `OPENROUTER_API_KEY`:
   ```bash
   cp .env.example .env
   ```
2. Build and run the smoke tests:
   ```bash
   docker compose build
   docker compose run --rm swe-policy-eval python -m pytest tests -q
   ```
3. Run the experiment pipeline:
   ```bash
   docker compose run --rm swe-policy-eval python scripts/select_tasks.py
   docker compose run --rm swe-policy-eval python scripts/prepull.py --stage pilot --platform linux/amd64
   docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage pilot --dry-run
   docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage pilot
   docker compose run --rm swe-policy-eval python scripts/evaluate.py --stage pilot
   docker compose run --rm swe-policy-eval python scripts/analyze.py --stage pilot

   # Main stage:
   docker compose run --rm swe-policy-eval python scripts/run_experiment.py --stage main --n-main 50 --repeats 2 --workers 4
   docker compose run --rm swe-policy-eval python scripts/evaluate.py --stage main
   docker compose run --rm swe-policy-eval python scripts/analyze.py --stage main
   ```

## Run it (Option B: Local Python Virtual Environment)

```bash
python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# macOS / Linux:        source .venv/bin/activate
pip install -e .                       # needs Docker + an API key in .env
python -m pytest tests -q              # no Docker needed

python scripts/select_tasks.py         # writes results/tasks.json (seed from config)
python scripts/run_experiment.py --stage pilot --dry-run     # shows worst-case spend
python scripts/run_experiment.py --stage pilot
python scripts/evaluate.py   --stage pilot
python scripts/analyze.py    --stage pilot

# after reading the pilot's cost and variability, pick the size:
python scripts/run_experiment.py --stage main --n-main 50 --repeats 2 --workers 4
python scripts/evaluate.py --stage main && python scripts/analyze.py --stage main
```

`tasks.json` stores the seed, the pool hash, the pilot IDs and the full ordered list
of remaining IDs. The main run uses a prefix of that list, so replication needs only
the seed and `--n-main`.

## Design decisions to check against your protocol

- **A round** = up to N agent steps (one model call + its commands). Rounds are my
  interpretation of your "every round" logging; change `steps_per_round` to taste.
- **Fixed policy**: R rounds of K steps, same neutral nudge each time, never stops early.
- **Controller**: rule-based and deterministic, no extra model calls, so the ledger
  contains only agent spend. It stops on passing tests, a stagnant patch, or a budget
  guard, and otherwise sizes the next round and writes a targeted nudge. Rules are
  documented at the top of `controller.py`.
- **Equal ceilings**: both arms share `per_run_cap_usd` and `step_ceiling`.
- **Visible vs hidden tests**: during a run only tests the agent itself ran are used.
  The hidden FAIL_TO_PASS / PASS_TO_PASS tests are executed only by `evaluate.py`.
- **Final patch**: the agent's own submission if it submitted, otherwise a harness
  `git diff` of tracked files with test paths excluded. Same rule for both arms.
  New untracked source files are not captured by the fallback.
- **Cost cap**: checked before each call with a per-call reserve, so overshoot is
  bounded but not exactly zero. Per-run cap ends that run; global cap stops everything.

## Caveats

- litellm must know your model's prices, otherwise mini-swe-agent raises a cost error
  (set `MSWEA_COST_TRACKING=ignore_errors` only if you will cost runs some other way,
  since the caps then see $0).
- `tests_pass` can trigger on a test the agent wrote itself. That is a limitation of
  using only visible signals; tighten it in `controller.py` if it matters.
- The Docker/API path (`run_experiment.py` against real containers) was not run in
  development; the smoke tests cover the round loop, policies, caps and patch logic.

