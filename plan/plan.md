# Oracle GoldenGate Monitor Implementation Plan

This document is aligned to the current implementation in [goldengate_monitor_graph.py](/D:/project/prj4/goldengate_monitor_graph.py). It describes what is already built, the purpose of each function and node, the current graph flow, and the main review points before the design is expanded further.

## Current Scope

The current implementation is a read-only LangGraph workflow for Oracle GoldenGate monitoring. It does not connect to GGSCI, Admin Client, REST APIs, or operating system telemetry yet. Instead, it accepts structured input in state, applies configured monitoring rules, classifies the environment as `good` or `bad`, and produces a final report.

The current graph implements:

1. Input normalization.
2. Monitoring rule construction.
3. Process discovery from provided input.
4. Status collection from provided metrics, logs, and disk values.
5. Problem classification.
6. Branching to good or bad reporting.
7. Final report generation.

## Implemented State Contract

The implemented shared state is `AgentState`.

Current fields:

1. `task` — freeform monitoring request.
2. `config` — user-provided configuration overrides.
3. `available_processes` — source inventory supplied to the graph.
4. `process_metrics` — process-level health metrics such as state and lag.
5. `process_logs` — raw log content keyed by process name.
6. `disk_metrics` — disk usage values for GoldenGate-related filesystems.
7. `request_context` — normalized request details prepared by `intake_node`.
8. `monitor_plan` — normalized rule set prepared by `monitor_plan_node`.
9. `discovered_processes` — filtered and normalized process inventory.
10. `status_snapshot` — collected process and disk health snapshot.
11. `problems` — structured detected issues.
12. `health_bucket` — branch result, currently `good` or `bad`.
13. `good_summary` — success-path summary.
14. `bad_summary` — failure-path summary.
15. `report` — final operator-facing report.

## Implemented Functions And Purpose

### `_default_config()`
Returns the default Oracle GoldenGate monitoring configuration.

Purpose:
1. Defines default OGG environment metadata.
2. Defines process filter defaults.
3. Defines default process state rules.
4. Defines default lag thresholds.
5. Defines default disk usage thresholds.
6. Defines category-specific operator recommendations.

### `_merge_dict(base, override)`
Recursively merges user config into the default config.

Purpose:
1. Allows partial config overrides.
2. Preserves missing default values.
3. Keeps nested rule structures simple to customize.

### `_matching_process(process, filters)`
Checks whether a process matches the configured type and name filters.

Purpose:
1. Restricts monitoring scope.
2. Keeps discovery and filtering logic separate.

### `_extract_error_lines(log_text)`
Extracts lines containing the word `ERROR` from a process log string.

Purpose:
1. Converts raw logs into simple error evidence.
2. Supplies `classify_problem_node` with operator-relevant log context.

### `intake_node(state)`
Normalizes the incoming request.

Purpose:
1. Reads the user-provided config from state.
2. Merges it into the default config.
3. Builds `request_context` for downstream nodes.

Outputs:
1. `config`
2. `request_context`

### `monitor_plan_node(state)`
Builds the concrete monitoring plan from normalized config.

Purpose:
1. Extracts OGG environment information.
2. Passes forward process filters.
3. Passes forward expected process names.
4. Emits `process_state_rules`.
5. Emits `lag_time_rules`.
6. Emits `disk_usage_rules`.
7. Emits operator recommendations.

Outputs:
1. `monitor_plan`

### `discover_processes_node(state)`
Builds the process inventory for the monitoring run.

Purpose:
1. Filters `available_processes` using `monitor_plan.process_filters`.
2. Compares discovered names to `expected_processes`.
3. Adds synthetic missing entries for expected-but-not-found processes.
4. Normalizes process records into a consistent structure.

Outputs:
1. `discovered_processes`

### `collect_status_node(state)`
Builds a snapshot of process and disk health.

Purpose:
1. Joins discovered processes with metrics from `process_metrics`.
2. Scans `process_logs` for `ERROR` entries.
3. Captures state, lag, checkpoint age, and restart information.
4. Appends disk usage metrics for OGG home, trail, and report filesystems.

Outputs:
1. `status_snapshot`

### `classify_problem_node(state)`
Evaluates the collected snapshot against the monitoring rules.

Purpose:
1. Evaluates process state using `process_state_rules`.
2. Evaluates lag using `lag_time_rules`.
3. Evaluates disk thresholds using `disk_usage_rules`.
4. Treats `ERROR` log lines as state-related problems.
5. Produces a structured list of detected problems.
6. Sets the branch bucket to `good` or `bad`.

Outputs:
1. `problems`
2. `health_bucket`

### `route_problem_bucket(state)`
Returns the branch label used by the conditional edge.

Purpose:
1. Routes the graph from `classify_problem` to `good` or `bad`.

### `good_node(state)`
Builds the success-path summary.

Purpose:
1. Counts the monitored processes.
2. Produces a concise healthy summary.

Outputs:
1. `good_summary`

### `bad_node(state)`
Builds the failure-path summary.

Purpose:
1. Formats each detected problem.
2. Includes restart time when available.
3. Includes a sample log error when available.
4. Appends operator guidance based on the problem category.

Outputs:
1. `bad_summary`

### `announce_user_node(state)`
Produces the final report string.

Purpose:
1. Adds environment name and timestamp.
2. Chooses `good_summary` or `bad_summary` based on `health_bucket`.
3. Produces the final operator-facing `report`.

Outputs:
1. `report`

### `build_graph()`
Constructs and compiles the LangGraph workflow.

Purpose:
1. Registers all implemented nodes.
2. Declares the entry point.
3. Declares the linear edges.
4. Declares the conditional branch from `classify_problem`.
5. Compiles the graph with `InMemorySaver()`.

## Implemented Graph Flow

Current node order:

1. `intake`
2. `monitor_plan`
3. `discover_processes`
4. `collect_status`
5. `classify_problem`
6. `good` or `bad`
7. `announce_user`
8. `END`

Current edges:

1. `intake -> monitor_plan`
2. `monitor_plan -> discover_processes`
3. `discover_processes -> collect_status`
4. `collect_status -> classify_problem`
5. `classify_problem -> good` when `health_bucket == "good"`
6. `classify_problem -> bad` when `health_bucket == "bad"`
7. `good -> announce_user`
8. `bad -> announce_user`
9. `announce_user -> END`

## Implemented Monitoring Rules

The current code evaluates three rule families inside `monitor_plan` and `classify_problem_node`.

### Process state rules
Configured in `process_state_rules`.

Current defaults:
1. Healthy states: `RUNNING`
2. Bad states: `ABENDED`, `STOPPED`, `MISSING`

### Lag time rules
Configured in `lag_time_rules`.

Current defaults:
1. Warning threshold: `300` seconds
2. Critical threshold: `900` seconds

### Disk usage rules
Configured in `disk_usage_rules`.

Current defaults:
1. OGG home warning: `80%`
2. OGG home critical: `90%`
3. Trail warning: `80%`
4. Trail critical: `90%`
5. Report warning: `80%`
6. Report critical: `90%`

## Sample Execution Path

The file currently includes a sample run in `__main__`.

Purpose:
1. Creates a sample state with one healthy Extract and one abended Replicat.
2. Adds a trail filesystem threshold breach.
3. Streams graph events using `thread = {"configurable": {"thread_id": "1"}}`.
4. Prints each event emitted by `graph.stream(...)`.

## Current Strengths

1. The implementation follows the lesson pattern closely: one `TypedDict` state, small node functions, explicit edges, and a conditional branch.
2. The graph is deterministic and easy to test because all inputs are passed through state.
3. The plan generation and classification logic are separated cleanly.
4. The current implementation already covers the three requested rule families: process state, lag, and disk usage.

## Current Gaps To Review

These are the main differences between the current implementation and the larger original target architecture.

1. No live GoldenGate integration yet. `discover_processes_node` reads `available_processes` from input state instead of querying GGSCI, Admin Client, or REST.
2. No external log or filesystem collection yet. `collect_status_node` reads `process_logs` and `disk_metrics` from input state.
3. No separate severity-classification node. Severity is assigned directly in `classify_problem_node`.
4. No enrichment loop. The current graph is single pass only.
5. No continuous polling path.
6. No escalation, ticketing, or notification channel integration.
7. No durable checkpointing. The graph currently uses `InMemorySaver()`.

## Recommended Review Items

These are the best next review points before expanding the implementation.

1. Decide whether to keep the current simple `good` and `bad` branch model or restore the larger investigate and escalate architecture later.
2. Decide how `discover_processes_node` should fetch real GoldenGate process inventory.
3. Decide whether `collect_status_node` should split into separate process, log, and disk collection nodes.
4. Decide whether warnings should still route to `bad`, or whether a third branch such as `warning` is needed.
5. Decide when to replace `InMemorySaver()` with durable checkpoint storage.

## Verification Checklist

1. Confirm `monitor_plan_node` emits `process_state_rules`, `lag_time_rules`, and `disk_usage_rules`.
2. Confirm `discover_processes_node` marks expected-but-missing processes as undiscovered.
3. Confirm `collect_status_node` includes both per-process records and a disk usage record.
4. Confirm `classify_problem_node` detects bad state, lag threshold breaches, disk threshold breaches, and `ERROR` log lines.
5. Confirm `route_problem_bucket` sends healthy runs to `good` and unhealthy runs to `bad`.
6. Confirm `announce_user_node` always emits a final `report`.

## Relevant Files

1. [goldengate_monitor_graph.py](/D:/project/prj4/goldengate_monitor_graph.py) — current implementation.
2. [context/Lesson_6_Student.py](/D:/project/prj4/context/Lesson_6_Student.py) — reference LangGraph example the implementation was modeled after.
3. [plan/graph_architecture.md](/D:/project/prj4/plan/graph_architecture.md) — architecture review document that may need further alignment if the implementation evolves again.