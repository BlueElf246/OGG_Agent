#!/usr/bin/env python

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

_ = load_dotenv()

memory = SqliteSaver.from_conn_string(":memory:")


class AgentState(TypedDict, total=False):
    task: str
    config: dict[str, Any]
    available_processes: list[dict[str, Any]]
    process_metrics: dict[str, dict[str, Any]]
    process_logs: dict[str, str]
    disk_metrics: dict[str, float]
    request_context: dict[str, Any]
    monitor_plan: dict[str, Any]
    discovered_processes: list[dict[str, Any]]
    status_snapshot: list[dict[str, Any]]
    problems: list[dict[str, Any]]
    health_bucket: Literal["good", "bad"]
    good_summary: str
    bad_summary: str
    report: str


def _default_config() -> dict[str, Any]:
    return {
        "ogg_env": {
            "name": "local-dev",
            "host": "localhost",
            "username": "oggadmin",
            "password": "<set-in-secret-store>",
            "ogg_home": "/u01/app/ogg",
        },
        "process_filters": {
            "types": ["MANAGER", "EXTRACT", "REPLICAT", "DISTRIBUTION", "RECEIVER"],
            "names": [],
        },
        "expected_processes": [],
        "process_state_rules": {
            "healthy_states": ["RUNNING"],
            "bad_states": ["ABENDED", "STOPPED", "MISSING"],
        },
        "lag_time_rules": {
            "warning_seconds": 300,
            "critical_seconds": 900,
        },
        "disk_usage_rules": {
            "ogg_home_warning_pct": 80,
            "ogg_home_critical_pct": 90,
            "trail_warning_pct": 80,
            "trail_critical_pct": 90,
            "report_warning_pct": 80,
            "report_critical_pct": 90,
        },
        "recommendations": {
            "state": "Inspect the GoldenGate report file and recent restart history before attempting any restart.",
            "lag": "Check trail backlog, network latency, and downstream database apply performance.",
            "disk": "Free space, rotate logs, or expand the filesystem before trail growth causes a process failure.",
        },
    }


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _matching_process(process: dict[str, Any], filters: dict[str, Any]) -> bool:
    allowed_types = set(filters.get("types") or [])
    allowed_names = set(filters.get("names") or [])

    if allowed_types and process.get("type") not in allowed_types:
        return False
    if allowed_names and process.get("name") not in allowed_names:
        return False
    return True


def _extract_error_lines(log_text: str) -> list[str]:
    lines = []
    for line in log_text.splitlines():
        if "ERROR" in line.upper():
            lines.append(line.strip())
    return lines


def intake_node(state: AgentState) -> dict[str, Any]:
    user_config = state.get("config", {})
    config = _merge_dict(_default_config(), user_config)
    request_context = {
        "task": state.get("task", "Monitor Oracle GoldenGate processes"),
        "ogg_env": config["ogg_env"],
        "process_filters": config["process_filters"],
    }
    return {
        "config": config,
        "request_context": request_context,
    }


def monitor_plan_node(state: AgentState) -> dict[str, Any]:
    config = state["config"]
    plan = {
        "ogg_env": config["ogg_env"],
        "process_filters": config["process_filters"],
        "expected_processes": config.get("expected_processes", []),
        "process_state_rules": config["process_state_rules"],
        "lag_time_rules": config["lag_time_rules"],
        "disk_usage_rules": config["disk_usage_rules"],
        "recommendations": config["recommendations"],
    }
    return {"monitor_plan": plan}


def discover_processes_node(state: AgentState) -> dict[str, Any]:
    available_processes = state.get("available_processes", [])
    plan = state["monitor_plan"]
    filtered = [
        process
        for process in available_processes
        if _matching_process(process, plan["process_filters"])
    ]

    expected_names = set(plan.get("expected_processes") or [])
    discovered_names = {process.get("name") for process in filtered}

    for missing_name in sorted(expected_names - discovered_names):
        filtered.append(
            {
                "name": missing_name,
                "type": "UNKNOWN",
                "discovered": False,
            }
        )

    normalized = []
    for process in filtered:
        normalized.append(
            {
                "name": process.get("name", "UNKNOWN"),
                "type": process.get("type", "UNKNOWN"),
                "host": process.get("host", plan["ogg_env"].get("host")),
                "discovered": process.get("discovered", True),
            }
        )

    return {"discovered_processes": normalized}


def collect_status_node(state: AgentState) -> dict[str, Any]:
    metrics = state.get("process_metrics", {})
    logs = state.get("process_logs", {})
    disk_metrics = state.get("disk_metrics", {})
    snapshot = []

    for process in state.get("discovered_processes", []):
        process_name = process["name"]
        process_metric = metrics.get(process_name, {})
        log_text = logs.get(process_name, "")
        error_lines = _extract_error_lines(log_text)
        snapshot.append(
            {
                "name": process_name,
                "type": process["type"],
                "host": process["host"],
                "discovered": process["discovered"],
                "state": process_metric.get("state", "MISSING" if not process["discovered"] else "UNKNOWN"),
                "lag_seconds": process_metric.get("lag_seconds", 0),
                "checkpoint_age_seconds": process_metric.get("checkpoint_age_seconds", 0),
                "last_restart": process_metric.get("last_restart"),
                "log_errors": error_lines,
            }
        )

    snapshot.append(
        {
            "disk_usage": {
                "ogg_home_pct": disk_metrics.get("ogg_home_pct", 0.0),
                "trail_pct": disk_metrics.get("trail_pct", 0.0),
                "report_pct": disk_metrics.get("report_pct", 0.0),
            }
        }
    )

    return {"status_snapshot": snapshot}


def classify_problem_node(state: AgentState) -> dict[str, Any]:
    plan = state["monitor_plan"]
    process_state_rules = plan["process_state_rules"]
    lag_time_rules = plan["lag_time_rules"]
    disk_usage_rules = plan["disk_usage_rules"]
    problems = []

    for entry in state.get("status_snapshot", []):
        if "disk_usage" in entry:
            disk_usage = entry["disk_usage"]
            disk_checks = [
                ("ogg_home_pct", "ogg_home_warning_pct", "ogg_home_critical_pct", "OGG home filesystem"),
                ("trail_pct", "trail_warning_pct", "trail_critical_pct", "trail filesystem"),
                ("report_pct", "report_warning_pct", "report_critical_pct", "report filesystem"),
            ]
            for metric_key, warning_key, critical_key, label in disk_checks:
                usage = float(disk_usage.get(metric_key, 0.0))
                if usage >= disk_usage_rules[critical_key]:
                    problems.append(
                        {
                            "category": "disk",
                            "severity": "critical",
                            "resource": label,
                            "message": f"{label} usage is {usage:.1f}% which exceeds the critical threshold of {disk_usage_rules[critical_key]}%.",
                        }
                    )
                elif usage >= disk_usage_rules[warning_key]:
                    problems.append(
                        {
                            "category": "disk",
                            "severity": "warning",
                            "resource": label,
                            "message": f"{label} usage is {usage:.1f}% which exceeds the warning threshold of {disk_usage_rules[warning_key]}%.",
                        }
                    )
            continue

        name = entry["name"]
        state_value = entry["state"]
        lag_seconds = int(entry.get("lag_seconds", 0))

        if state_value in process_state_rules["bad_states"]:
            last_restart = entry.get("last_restart") or "unknown"
            problems.append(
                {
                    "category": "state",
                    "severity": "critical",
                    "process": name,
                    "message": f"{name} is in state {state_value}.",
                    "last_restart": last_restart,
                    "log_errors": entry.get("log_errors", []),
                }
            )

        if lag_seconds >= lag_time_rules["critical_seconds"]:
            problems.append(
                {
                    "category": "lag",
                    "severity": "critical",
                    "process": name,
                    "message": f"{name} lag is {lag_seconds} seconds which exceeds the critical threshold of {lag_time_rules['critical_seconds']} seconds.",
                }
            )
        elif lag_seconds >= lag_time_rules["warning_seconds"]:
            problems.append(
                {
                    "category": "lag",
                    "severity": "warning",
                    "process": name,
                    "message": f"{name} lag is {lag_seconds} seconds which exceeds the warning threshold of {lag_time_rules['warning_seconds']} seconds.",
                }
            )

        if entry.get("log_errors"):
            problems.append(
                {
                    "category": "state",
                    "severity": "critical",
                    "process": name,
                    "message": f"{name} log contains ERROR entries.",
                    "log_errors": entry["log_errors"],
                }
            )

    return {
        "problems": problems,
        "health_bucket": "good" if not problems else "bad",
    }


def route_problem_bucket(state: AgentState) -> str:
    return state["health_bucket"]


def good_node(state: AgentState) -> dict[str, Any]:
    process_count = len([entry for entry in state.get("status_snapshot", []) if "name" in entry])
    summary = (
        f"All monitored GoldenGate processes passed the configured rules. "
        f"Checked {process_count} processes for state, lag, and disk health."
    )
    return {"good_summary": summary}


def bad_node(state: AgentState) -> dict[str, Any]:
    recommendations = state["monitor_plan"]["recommendations"]
    lines = ["Problems were detected in the GoldenGate environment:"]

    for problem in state.get("problems", []):
        lines.append(f"- [{problem['severity']}] {problem['message']}")
        if problem.get("last_restart"):
            lines.append(f"  Last restart or crash time: {problem['last_restart']}")
        if problem.get("log_errors"):
            first_error = problem["log_errors"][0]
            lines.append(f"  Sample log error: {first_error}")

        category = problem["category"]
        lines.append(f"  Recommended next step: {recommendations[category]}")

    return {"bad_summary": "\n".join(lines)}


def announce_user_node(state: AgentState) -> dict[str, Any]:
    env_name = state["monitor_plan"]["ogg_env"]["name"]
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    headline = f"Oracle GoldenGate monitor report for {env_name} at {timestamp}"

    if state["health_bucket"] == "good":
        body = state.get("good_summary", "No problems were detected.")
    else:
        body = state.get("bad_summary", "Problems were detected.")

    report = f"{headline}\n\n{body}"
    return {"report": report}


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node("monitor_plan", monitor_plan_node)
    builder.add_node("discover_processes", discover_processes_node)
    builder.add_node("collect_status", collect_status_node)
    builder.add_node("classify_problem", classify_problem_node)
    builder.add_node("good", good_node)
    builder.add_node("bad", bad_node)
    builder.add_node("announce_user", announce_user_node)

    builder.set_entry_point("intake")
    builder.add_edge("intake", "monitor_plan")
    builder.add_edge("monitor_plan", "discover_processes")
    builder.add_edge("discover_processes", "collect_status")
    builder.add_edge("collect_status", "classify_problem")
    builder.add_conditional_edges(
        "classify_problem",
        route_problem_bucket,
        {
            "good": "good",
            "bad": "bad",
        },
    )
    builder.add_edge("good", "announce_user")
    builder.add_edge("bad", "announce_user")
    builder.add_edge("announce_user", END)

    return builder.compile(checkpointer=memory)


graph = build_graph()


if __name__ == "__main__":
    sample_state: AgentState = {
        "task": "Monitor Oracle GoldenGate health",
        "config": {
            "ogg_env": {
                "name": "ogg-prod",
                "host": "db-goldengate-01",
                "ogg_home": "/u01/app/ogg",
            },
            "expected_processes": ["EXT_SALES", "REP_SALES"],
        },
        "available_processes": [
            {"name": "EXT_SALES", "type": "EXTRACT", "host": "db-goldengate-01"},
            {"name": "REP_SALES", "type": "REPLICAT", "host": "db-goldengate-01"},
        ],
        "process_metrics": {
            "EXT_SALES": {
                "state": "RUNNING",
                "lag_seconds": 120,
                "checkpoint_age_seconds": 90,
                "last_restart": "2026-05-22 08:15:00 UTC",
            },
            "REP_SALES": {
                "state": "ABENDED",
                "lag_seconds": 1250,
                "checkpoint_age_seconds": 920,
                "last_restart": "2026-05-22 09:05:00 UTC",
            },
        },
        "process_logs": {
            "EXT_SALES": "INFO Extraction healthy",
            "REP_SALES": "INFO Restart attempted\nERROR OGG-01296 Failed to write checkpoint record",
        },
        "disk_metrics": {
            "ogg_home_pct": 62.0,
            "trail_pct": 91.0,
            "report_pct": 48.0,
        },
    }

    result = graph.invoke(sample_state)
    print(result["report"])