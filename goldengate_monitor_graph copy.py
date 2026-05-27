#!/usr/bin/env python

from __future__ import annotations
from typing import Annotated
import operator
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from langgraph.graph import MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode

from paramiko_tools import check_disk, check_log, connect_ssh, get_process

_ = load_dotenv()

memory = InMemorySaver()
tools_list = [connect_ssh, get_process, check_log, check_disk]
model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0).bind_tools(tools_list)
planner_model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)

MAX_TOOL_CALLS = 8
MAX_ITERATIONS = 10
tool_node = ToolNode(tools_list)


class IntakeResult(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    is_complete: bool = False
    clarification_question: str = ""

def _default_config() -> dict[str, Any]:
    return {
        "server":{
            "hostname": "54.89.249.254",
            "username": "ec2-user",
            "password": "12345678",
            "key_filename": "abc.pem"
        },
        "process_filters": {
            "names": ["EXT1", "RPT1"],
        },
    
        "process_state_rules": {
            "healthy_states": ["RUNNING"],
            "bad_states": ["ABENDED", "STOPPED"],
        },
        "lag_time_rules": {
            "warning_seconds": 300,
            "critical_seconds": 900,
        },
        "disk_usage_rules": {
        },
        "is_complete": False,

    }
class AgentState(MessagesState, total=False):
    task: str
    is_complete: bool
    should_investigate: bool
    data_collected: dict[str, Any]
    config: dict[str, Any]
    clarification_question: str
    user_context: Annotated[list[str], operator.add]
    answer: str
    tool_call_count: Annotated[int, operator.add] = 0
    iteration_count: Annotated[int, operator.add] = 0

def _get_plan_prompt(config):
     return """You are an Oracle GoldenGate expert. Collect the user's information and fill the monitoring config.

Default config template:
{}

Rules:
1. Use only information explicitly provided by the user.
2. Keep any unknown field empty instead of guessing.
3. Set `is_complete` to true only when the minimum information needed to continue is present.
4. If information is missing, ask exactly one focused follow-up question in `clarification_question`.
5. If information is complete, return an empty `clarification_question`.

Return structured output with these fields:
- `config`: the updated config
- `is_complete`: boolean
- `clarification_question`: one short question for the user when more information is needed
""".format(config)


def _build_intake_context(user_context: list[str]) -> str:
    lines = [f"User input {index}: {entry}" for index, entry in enumerate(user_context, start=1)]
    return "\n".join(lines)

def intake_node(state: AgentState):
    current_config = state.get("config") or _default_config()
    user_context = state.get("user_context") or [state["task"]]
    sys_msg = SystemMessage(content=_get_plan_prompt(current_config))
    human_msg = HumanMessage(content=_build_intake_context(user_context))
    response = planner_model.with_structured_output(IntakeResult).invoke([sys_msg, human_msg])

    clarification_question = response.clarification_question.strip()
    # print(response)
    if not response.is_complete and not clarification_question:
        clarification_question = "Please provide the missing Oracle GoldenGate host or process information."

    return {
        "config": response.config or current_config,
        "is_complete": response.is_complete,
        "clarification_question": clarification_question,
    }



def request_clarification(state: AgentState) -> dict[str, Any]:
    return {}

def _get_ochestrator_prompt():
    return """
    Your are an Oracle GoldenGate data collector. Your task is to analyze the collected infomation and determine if it's sufficient to identify the problem and provide recommendation.

    Rules:
    1. You MUST call 'connect_ssh' first.
    2. Only CALL ONCE for each tool.

    Workflow:
    1. Check for the data collected. Identify what has already been collected and what is still missing.
    2. Call 'get_process' to get OGG processes status

    """
def orchestrator_node(state: AgentState):
    if state.get("is_complete"):
        messages = state.get("messages", [])
        if not messages:
            messages = [HumanMessage(content=f"Collected monitoring config: {state.get('config', {})}")]
        response = model.invoke([SystemMessage(content=_get_ochestrator_prompt())] + messages)
        tool_calls = getattr(response, "tool_calls", None) or []
        return {"messages": [response], "tool_call_count": len(tool_calls), "iteration_count": 1}
    else:
        return {}


def _get_analyze_prompt():
    return """
Your are an Oracle GoldenGate analyst. Your task is to analyze the collected infomation and determine if it's sufficient to identify the problem and provide recommendation.
"""
def analyze_data_node(state: AgentState):
    data_collected = state.get("data_collected", {})
    messages = state.get("messages", [])
    response = model.invoke([SystemMessage(content=_get_analyze_prompt())] + messages)
    return {"messages": [response], "iteration_count": state.get("iteration_count", 0) + 1}

def route_after_request(state: AgentState) -> Literal["request_clarification", "orchestration"]:
    if not state.get("is_complete", False):
        return "request_clarification"
    return "orchestration"
    
def route_after_orchestrator_call(state: AgentState) -> Literal["tools", "analyze_data_node"]:
    iteration = state.get("iteration_count", 0)
    tool_count = state.get("tool_call_count", 0)

    if iteration >= MAX_ITERATIONS or tool_count > MAX_TOOL_CALLS:
        return "analyze_data_node"

    messages = state.get("messages", [])
    if not messages:
        return "analyze_data_node"
    
    last_message = messages[-1]
    tool_calls = getattr(last_message, "tool_calls", None) or []

    if not tool_calls:
        return "analyze_data_node"
    
    return "tools"



def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node(request_clarification)
    builder.add_node("tools", tool_node)
    builder.add_node("orchestration", orchestrator_node)
    builder.add_node("analyze_data_node", analyze_data_node)

    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_request)
    builder.add_edge("request_clarification", "intake")
    builder.add_edge("tools", "orchestration")
    builder.add_conditional_edges("orchestration", route_after_orchestrator_call, {"tools": "tools", "analyze_data_node": "analyze_data_node"})
    builder.add_edge("analyze_data_node", END)

    return builder.compile(checkpointer=memory, interrupt_before=["request_clarification"])


graph = build_graph()


if __name__ == "__main__":
    config = {"configurable": {"thread_id": "1"}}
    
    try:
        task = input("Enter your task: ").strip()
        stream_input = {"task": task, "user_context": [task]}

        while True:
            for event in graph.stream(stream_input, config=config):
                print(f"Event: {event}")

            current_state = graph.get_state(config)
            print(f"Current state: {current_state}")

            if not current_state.next:
                break

            state_values = getattr(current_state, "values", {}) or {}
            clarification_question = state_values.get("clarification_question") or "Please provide the missing information."
            print(f"\nClarification needed: {clarification_question}")

            follow_up = input("Additional information: ").strip()
            if not follow_up:
                print("No additional information provided. Stopping execution.")
                break

            graph.update_state(config, {"task": follow_up, "user_context": [follow_up]})
            stream_input = None
    
    except Exception as e:
        print(f"❌ Error: {str(e)}")