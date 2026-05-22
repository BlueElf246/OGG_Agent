#!/usr/bin/env python

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langchain_openai import ChatOpenAI

from paramiko_tools import check_disk, check_log, connect_ssh, get_process

_ = load_dotenv()

memory = InMemorySaver()
model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0).bind_tools(
    [connect_ssh, get_process, check_log, check_disk]
)
MAX_DISCOVERY_ATTEMPTS = 3

def _default_config() -> dict[str, Any]:
    return {
        "server":{
            "hostname": "54.89.249.254",
            "username": "ec2-user",
            "password": "12345678",
            "key_filename": "abc.pem"
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
class AgentState(TypedDict, total=False):
    task: str
    intent: Literal["monitor_ogg", "chat"]
    chat_response: str
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
    discover_attempts: int
    discovery_status: Literal["complete", "retry", "failed"]
    discovery_prompt: str

PLAN_PROMPT="""
You are a helpful assistant. call the tools if asked.
Given the default configuration and user overrides, create a monitoring plan for the Oracle GoldenGate environment.
{}
""".format(_default_config())

def llm_node(state: AgentState) -> dict[str, Any]:
    messages = [
        SystemMessage(
            content=(
                "Classify the user's intent. Return only one token: monitor_ogg "
                "if the user wants Oracle GoldenGate monitoring, otherwise chat."
            )
        ),
        HumanMessage(content=state["task"]),
    ]
    response = model.invoke(messages)
    # print(f"LLM response: {response}")
    content = getattr(response, "content", "")
    intent = "monitor_ogg" if "monitor_ogg" in str(content).lower() else "chat"
    if intent == "chat":
        reply = model.invoke(
            [
                SystemMessage(content="You are a helpful assistant."),
                HumanMessage(content=state["task"]),
            ]
        )
        return {"intent": intent, "chat_response": getattr(reply, "content", str(reply))}
    return {"intent": intent}

def intake_node(state: AgentState):
    return {"config": _default_config()}


def route_from_intent(state: AgentState):
    return "intake" if state.get("intent") == "monitor_ogg" else END

def monitor_plan_node(state: AgentState) -> dict[str, Any]:
    config = state["config"]
    plan = {
        "host": config['server'],
        "ogg_env": config["ogg_env"],
        "process_filters": config["process_filters"],
        "expected_processes": config.get("expected_processes", []),
        "process_state_rules": config["process_state_rules"],
        "lag_time_rules": config["lag_time_rules"],
        "disk_usage_rules": config["disk_usage_rules"],
        "recommendations": config["recommendations"],
    }
    return {"monitor_plan": plan}
def _get_discover_prompt(info, missing_fields: list[str] | None = None, attempt: int = 1):
    missing_text = ", ".join(missing_fields) if missing_fields else "process_info, process_log, disk_usage"
    DISCOVER_PROMPT="""
    Here is the infomation to connect to host and get the processes infomation
    {}. Here are some tools you can use to retrieve the information:
    1. get_process to get the list of GoldenGate processes, including their names, types, and hosts.
    2. check_log to check the log of a specific process for any error messages or warnings.
    3. check_disk to inspect the disk usage of the host, especially for the filesystems   
    """.format(info)
    HUMAN_PROMPT="""    Attempt {attempt}/{max_attempts}.
    Missing or incomplete fields from the previous discovery attempt: {missing_text}.
    Please use the above information to connect to the host and retrieve the list of GoldenGate processes, including their names, types, and hosts. Return the information in a structured format like this:
    [
        {{`process_info`: (output from `get processes` command)}},
        {{`process_log`: (output from `check log` command for a specific process)}},
        {{`disk_usage`: (output from `check disk usage` command)}},
        ...
    ]""".format(attempt=attempt, max_attempts=MAX_DISCOVERY_ATTEMPTS, missing_text=missing_text)
    return DISCOVER_PROMPT, HUMAN_PROMPT


def _discovery_result_fields(data: Any) -> set[str]:
    fields: set[str] = set()
    print(f"Discovery result data: {data}")
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            fields.update(str(key) for key in value.keys())
            for item in value.values():
                visit(item)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return
            try:
                visit(json.loads(text))
            except Exception:
                lowered = text.lower()
                for field in ("process_info", "process_log", "disk_usage"):
                    if field in lowered:
                        fields.add(field)

    visit(data)
    return fields


def _discovery_is_complete(data: Any) -> tuple[bool, list[str]]:
    required_fields = {"process_info", "process_log", "disk_usage"}
    found_fields = _discovery_result_fields(data)
    missing_fields = sorted(required_fields - found_fields)
    return not missing_fields, missing_fields

def orchestration_node(state: AgentState):
    discovered_processes = state.get("discovered_processes", [])
    attempts = state.get("discover_attempts", 0)
    is_complete, missing_fields = _discovery_is_complete(discovered_processes)

    if is_complete:
        return {
            "discover_attempts": attempts,
            "discovery_status": "complete",
        }

    next_attempt = attempts + 1
    _, retry_prompt = _get_discover_prompt(
        state["monitor_plan"]["host"],
        missing_fields=missing_fields,
        attempt=next_attempt,
    )
    return {
        "discover_attempts": next_attempt,
        "discovery_status": "failed" if next_attempt >= MAX_DISCOVERY_ATTEMPTS else "retry",
        "discovery_prompt": retry_prompt,
    }


def route_from_discovery(state: AgentState):
    if state.get("discovery_status") == "complete":
        return "classify_problem"
    if state.get("discover_attempts", 0) >= MAX_DISCOVERY_ATTEMPTS:
        return END
    return "discover_processes"

def discover_processes_node(state: AgentState) -> dict[str, Any]:
    monitor_plan = state["monitor_plan"]
    #### retrieve the host information
    system_message, default_human_message = _get_discover_prompt(
        monitor_plan["host"],
        attempt=state.get("discover_attempts", 0) + 1,
    )
    human_message = state.get("discovery_prompt", default_human_message)
    messages = [
        SystemMessage(content=system_message),
        HumanMessage(content=human_message)
    ]
    response = model.invoke(messages)

    return {"discovered_processes": getattr(response, "content", str(response))}

def _get_classify_prompt(info, rules):
    CLASSIFY_PROMPT = """
    Here is the monitoring data for the GoldenGate processes and disk usage:
    {}.
    Here is the monitoring plan and rules:
    {}.

    return as this format:
    {{
        {{"process_name": "EXT_SALES", "state": "RUNNING", "severity": "healthy", "message": "EXT_SALES is running normally."}},
        {{"process_name": "REP_SALES", "state": "ABENDED", "severity": "critical", "message": "REP_SALES is in ABENDED state and log contains ERROR entries."}},
        {{"disk_resource": "trail filesystem", "severity": "critical", "message": "Trail filesystem usage is 91% which exceeds the critical threshold of 90%."}},
         ...
    }}
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



def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("llm_node", llm_node)
    builder.add_node("intake", intake_node)
    builder.add_node("monitor_plan", monitor_plan_node)
    builder.add_node("discover_processes", discover_processes_node)
    builder.add_node("orchestration", orchestration_node)
    builder.add_node("classify_problem", classify_problem_node)
    builder.add_node("announce_user", announce_user_node)

    builder.set_entry_point("llm_node")
    builder.add_conditional_edges("llm_node", route_from_intent)
    builder.add_edge("intake", "monitor_plan")
    builder.add_edge("monitor_plan", "discover_processes")
    builder.add_edge("discover_processes", "orchestration")
    builder.add_conditional_edges("orchestration", route_from_discovery)
    builder.add_edge("classify_problem", "announce_user")
    builder.add_edge("announce_user", END)

    return builder.compile(checkpointer=memory)


graph = build_graph()


if __name__ == "__main__":
    sample_state = AgentState(
        task=input("Enter your task: "),
    )

    thread = {"configurable": {"thread_id": "1"}}
    for event in graph.stream(sample_state, thread):
        # if event.intent == 'chat':
        #     print(f"Chat response: {event.chat_response}")
        # elif event.intent == 'monitor_ogg':
        #     print(event)
        print(f"Event: {event}")