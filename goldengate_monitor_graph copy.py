#!/usr/bin/env python

from __future__ import annotations
from typing import Annotated
import operator
import json
from datetime import datetime
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
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

MAX_TOOL_CALLS = 8
MAX_ITERATIONS = 10
tool_node = ToolNode(tools_list)

def _default_config() -> dict[str, Any]:
    return {
        "server":{
            "hostname": "54.89.249.254",
            "username": "ec2-user",
            "password": "12345678",
            "key_filename": "abc.pem"
        },
        "ogg_env": {
            
        },
        "process_filters": {
            "names": [],
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
    answer: str
    tool_call_count: Annotated[int, operator.add] = 0
    iteration_count: Annotated[int, operator.add] = 0

def _get_plan_prompt(config):
     return """You are an Oracle GoldenGate expert. Your task is to collect the user's infomation and fill in the config
You are an Oracle GoldenGate expert. Your task is to collect the user's infomation and fill in the config
Here is the default config:
{}
Rules:
1. only get the infomation related to user, do not make any assumption.
2. If some field in the config is incomplete, let it be empty.

Workflow:
1. Collect user infomation and fill in the config
2. If all infomation filled, return the complete config and set `is_complete` to true if all infomation is filled, else false.

Output:
the config with users infomation provided in json format, like default config

""".format(config)

def intake_node(state: AgentState):
    state['config'] = _default_config()
    sys_msg = SystemMessage(content=_get_plan_prompt(state['config']))
    human_msg = HumanMessage(content=state['task'])
    response = model.invoke([sys_msg, human_msg])
    print(f"Model response: {response}")
    return {"config": getattr(response, "content", str(response))}



def request_more_infomation(state: AgentState) -> bool:
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
        data_collected = state.get("data_collected", {})
        messages = state.get("messages", [])
        response = model.invoke([SystemMessage(content=_get_ochestrator_prompt())] + messages)
        tool_calls = getattr(response, "tool_calls", None) or []
        return {"messages": [response], "tool_call_count": len(tool_calls), "iteration_count": 1}
    else:
        return END   


def _get_analyze_prompt():
    return """
Your are an Oracle GoldenGate analyst. Your task is to analyze the collected infomation and determine if it's sufficient to identify the problem and provide recommendation.

"""
def analyze_data_node(state: AgentState):
    data_collected = state.get("data_collected", {})
    messages = state.get("messages", [])
    response = model.invoke([SystemMessage(content=_get_analyze_prompt())] + messages)
    return {"messages": [response], "iteration_count": state.get("iteration_count", 0) + 1}

def route_after_request(state: AgentState) -> Literal["intake", "orchestration"]:
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
    builder.add_node("request_clarification", request_more_infomation)
    builder.add_node("tools", tool_node)
    builder.add_node("orchestration", orchestrator_node)
    builder.add_node("analyze_data_node", analyze_data_node)

    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_request)
    builder.add_edge("request_clarification", "intake")
    builder.add_conditional_edges("orchestration", route_after_orchestrator_call, {"tools": "tools", "analyze_data_node": "analyze_data_node"})
    builder.add_edge("analyze_data_node", END)

    return builder.compile(checkpointer=memory, interrupt_before=["request_clarification"])


graph = build_graph()


if __name__ == "__main__":
    config = {"configurable": {"thread_id": "1"}}
    task = input("Enter your task: ")
    
    try:
        # Get current state to check for interrupts
        current_state = graph.get_state(config)
        print(f"Current state: {current_state}")
        if current_state.next:
            # There's a pending interrupt, update state with new input
            graph.update_state(config, {"task": task})
            stream_input = None
        else:
            # New execution
            stream_input = {"task": task}
        
        # Stream events from the graph
        print(f"Stream input: {stream_input}")
        for event in graph.stream(stream_input, config=config):
            print(f"Event: {event}")
    
    except Exception as e:
        print(f"❌ Error: {str(e)}")