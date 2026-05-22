#!/usr/bin/env python

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict

from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langchain_openai import ChatOpenAI

from paramiko_tools import check_disk, check_log, connect_ssh, get_process

_ = load_dotenv()

memory = InMemorySaver()
model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0).bind_tools(
    [connect_ssh, get_process, check_log, check_disk]
)

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

PLAN_PROMPT="""
Given the default configuration and user overrides, create a monitoring plan for the Oracle GoldenGate environment.
{}
""".format(_default_config())
def intake_node(state: AgentState):
    messages = [
        SystemMessage(content=PLAN_PROMPT), 
        HumanMessage(content=state['task'])
    ]
    response = model.invoke(messages)
    return {"config": response}

def monitor_plan_node(state: AgentState) -> dict[str, Any]:
    config = state["config"]
    plan = {
        "host": config["host"],
        "ogg_env": config["ogg_env"],
        "process_filters": config["process_filters"],
        "expected_processes": config.get("expected_processes", []),
        "process_state_rules": config["process_state_rules"],
        "lag_time_rules": config["lag_time_rules"],
        "disk_usage_rules": config["disk_usage_rules"],
        "recommendations": config["recommendations"],
    }
    return {"monitor_plan": plan}
def _get_discover_prompt(info):
    DISCOVER_PROMPT="""
    Here is the infomation to connect to host and get the processes infomation
    {}. Using tools
    `connect to host`: connect to host
    `get processes`: get the list of GoldenGate processes, including their names, types, and hosts.
    `check log`: check the log of a process, return the recent log content.
    `check disk usuage`: check the disk usage of OGG home, trail and report directory, return the usage percentage.
    """.format(info)
    HUMAN_PROMPT="""    Please use the above information to connect to the host and retrieve the list of GoldenGate processes, including their names, types, and hosts. Return the information in a structured format like this:
    [
        {"name": "EXT_SALES", "type": "EXTRACT", "host": "db-goldengate-01"},
        {"name": "REP_SALES", "type": "REPLICAT", "host": "db-goldengate-01"},
        ...
    ]"""
    return DISCOVER_PROMPT, HUMAN_PROMPT
def discover_processes_node(state: AgentState) -> dict[str, Any]:
    monitor_plan = state["monitor_plan"]
    #### retrieve the host information
    system_message, human_message = _get_discover_prompt(monitor_plan["host"])
    messages = [
        SystemMessage(content=system_message),
        HumanMessage(content=human_message)
    ]
    response = model.invoke(messages)
    return {"discovered_processes": response}

def _get_classify_prompt(info, rules):
    CLASSIFY_PROMPT="""
    Here is the monitoring data for the GoldenGate processes and disk usage:
    {}.
    Here is the monitoring plan and rules:
    {}

    return as this format:
    {
        {"process_name": "EXT_SALES", "state", "severity": "healthy", "message": "EXT_SALES is running normally."},
        {"process_name": "REP_SALES", "state", "severity": "critical", "message": "REP_SALES is in ABENDED state and log contains ERROR entries."},
        {"disk_resource": "trail filesystem", "severity": "critical", "message": "Trail filesystem usage is 91% which exceeds the critical threshold of 90%."},
         ...
    }
    """.format(info, rules)
    return CLASSIFY_PROMPT

def classify_problem_node(state: AgentState) -> dict[str, Any]:
    data = state['discovered_processes']
    rules = f"""
    "process_state_rules": {state["monitor_plan"]["process_state_rules"]},
    "lag_time_rules": {state["monitor_plan"]["lag_time_rules"]},
    "disk_usage_rules": {state["monitor_plan"]["disk_usage_rules"]},
    """
    system_message = _get_classify_prompt(data, rules)
    messages = [
        SystemMessage(content=system_message),
    ]
    response = model.invoke(messages)
    return {"problems": response}

def _get_announce_prompt(data):
    ANNOUNCE_PROMPT="""
    Here is the health status of the GoldenGate environment:
    {}.

    Please summarize the health status in a user-friendly report format, including any detected problems and recommended next steps.
    """.format(data)
    return ANNOUNCE_PROMPT
def announce_user_node(state: AgentState) -> dict[str, Any]:
    problems = state.get("problems", [])
    system_message = _get_announce_prompt(problems)
    messages = [
        SystemMessage(content=system_message),
    ]
    response = model.invoke(messages)
    return {"announcement": response}

def _default_config() -> dict[str, Any]:
    return {
        "host":{
            "name": "localhost",
            "username": "root",
            "password": "<set-in-secret-store>",
        },
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
            "bad_states": ["ABENDED", "STOPPED"],
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

def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node("monitor_plan", monitor_plan_node)
    builder.add_node("discover_processes", discover_processes_node)
    builder.add_node("classify_problem", classify_problem_node)
    builder.add_node("announce_user", announce_user_node)

    builder.set_entry_point("intake")
    builder.add_edge("intake", "monitor_plan")
    builder.add_edge("monitor_plan", "discover_processes")
    builder.add_edge("discover_processes", "classify_problem")
    builder.add_edge("classify_problem", "announce_user")
    builder.add_edge("announce_user", END)

    return builder.compile(checkpointer=memory)


graph = build_graph()


if __name__ == "__main__":
    sample_state = AgentState(
        task="Monitor the Oracle GoldenGate environment and report any issues.",
    )

    thread = {"configurable": {"thread_id": "1"}}
    for event in graph.stream(sample_state, thread):
        print(event)