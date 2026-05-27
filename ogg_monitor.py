#### import libraries
from __future__ import annotations
from typing import Annotated
import operator
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from pydantic import BaseModel, Field
from langgraph.graph import MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode

from paramiko_tools import check_disk, check_log, connect_ssh, get_process

from config import setting
#### defining model
tools_list = [connect_ssh, get_process, check_log, check_disk]
model = ChatOpenAI(model="gpt-4o", temperature=0.1).bind_tools(tools_list)
MAX_TOOL_CALLS = 8
MAX_ITERATIONS = 10
tool_node = ToolNode(tools_list)
memory = InMemorySaver()
#### defining prompts
def get_intake_node_prompt():
    return """
You are an config verifier. 
Your task is to extract the infomation from User Message and fill in Existing Config
Rules:
1. The config is filled when all values are not empty (For example 'username': 'ec2-user' -> valid,  'username': '' -> invalid)
2. The Last Tool Message should output no error
3. If the config is fully filled (pass rule 1, 2), return is_clear=true and return the config, else return is_clear=false and ask the user provide infomation
4. No follow-up questions, no explanations

Input:
- User Message: the last message from user
- Last Tool Message: the most recent tool output, if any
- Existing Config: the config provided by user
- Conversation Summary: a brief summary of the conversation history, it can be empty if no conversation
Output:
- is_clear:
- config:
"""
def get_conversation_summary_prompt() -> str:
    return """You are an expert conversation summarizer.

Your task is to create a brief 1-2 sentence summary of the conversation (max 30-50 words).

Include:
- Main topics discussed
- Important infomation provided by the user
- Any unresolved questions if applicable

Exclude:
- Greetings, misunderstandings, off-topic content.

Output:
- Return ONLY the summary.
- Do NOT include any explanations or justifications.
- If no meaningful topics exist, return an empty string.
"""
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
def _get_analyze_prompt():
    return """
Your are an Oracle GoldenGate analyst. Your task is to analyze the collected infomation and determine if it's sufficient to identify the problem and provide recommendation.
"""
#### helper
def is_config_complete(config: str) -> bool:
    #### parsing through the config, return true if none of all the fields is empty
    try:
        config_dict = eval(config) if isinstance(config, str) else config
        if not isinstance(config_dict, dict):
            return False
        #### check recursively
        for key, value in config_dict.items():
            #### if value is a dict, check recursively
            if isinstance(value, dict):
                if not is_config_complete(value):
                    return False
        return True
    except Exception as e:
        print(f"Error parsing config: {e}")
        return False
#### defining state
class AgentState(MessagesState, total=False):
    task: str
    is_complete: bool
    config: dict[str, Any]
    clarification_question: str
    conversation_summary: str
    data_collected: str
    tool_error_message: str
    tool_call_count: Annotated[int, operator.add] = 0
    iteration_count: Annotated[int, operator.add] = 0
#### defining schema
class QueryAnalysis(BaseModel):
    is_clear: bool = Field(
        description="Indicates if the user's question is clear and answerable."
    )
    config: str = Field(
        description="The config provided by the user or extracted from the user message."
    )
    clarification_needed: str = Field(
        description="Explanation if the question is unclear."
    )
    reasoning: str = Field(
        description="The reasoning process the model went through to determine if the config is clear or not."
    )
#### defining nodes
def intake_node(state: AgentState):
    """Get the user last quesition, get the context, building llm with structured output, then invoke to 
    know if the infomation is provided enough, if true set is_clear=true, else set false and assign
    "message" = AIMessage(content=clarification)
    """
    # print(state)
    last_message = state["messages"][-1].content if state["messages"] else None
    last_tool_message = next((msg.content for msg in reversed(state.get("messages", [])) if isinstance(msg, ToolMessage)), None)
    # print(conversation)
    config = state.get("config", setting.server_config)
    context_section = f"Context Section:\nUser Message: {last_message}\nLast Tool Message: {last_tool_message or ''}\nExisting Config:\n{config}\n Conversation Summary:\n{state.get('conversation_summary', '')}"
    print(context_section)
    llm_with_structure = model.with_config(temperature=0.1).with_structured_output(QueryAnalysis)
    response = llm_with_structure.invoke([SystemMessage(content=get_intake_node_prompt()) , HumanMessage(content=context_section)])
    
    if response.is_clear:
        return {"is_complete": True, "config": response.config}
    else:
        clarification = response.clarification_needed if response.clarification_needed and len(response.clarification_needed.strip()) > 10 else "I need more information to understand your question."
        return {"is_complete": False, "clarification_question": clarification, "messages": [AIMessage(content=clarification)], "config": response.config}
def request_clarification(state: AgentState) -> dict[str, Any]:
    return {}
def summarize_history(state: AgentState):
    if len(state["messages"]) < 1:
        return {"conversation_summary": ""}
    
    relevant_msgs = [
        msg for msg in state["messages"][:-1]
        if isinstance(msg, (HumanMessage, AIMessage)) and not getattr(msg, "tool_calls", None)
    ]

    if not relevant_msgs:
        return {"conversation_summary": ""}
    
    conversation = "Conversation history:\n"
    for msg in relevant_msgs[-6:]:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        conversation += f"{role}: {msg.content}\n"

    summary_response = model.with_config(temperature=0.2).invoke([SystemMessage(content=get_conversation_summary_prompt()), HumanMessage(content=conversation)])
    return {"conversation_summary": summary_response.content}

def orchestrator_node(state: AgentState):
    messages = state.get("messages", [])
    latest_tool_message = next((msg for msg in reversed(messages) if isinstance(msg, ToolMessage)), None)

    if latest_tool_message and latest_tool_message.content and "error" in latest_tool_message.content.lower():
        tool_error_message = (
            "The current server config is not working. "
            f"Tool error: {latest_tool_message.content}. "
            "Please update the config and provide the correct host, username, password, port, or key file."
        )
        return {
            "messages": [AIMessage(content=tool_error_message)],
            "is_complete": False,
            "tool_error_message": tool_error_message,
            "clarification_question": tool_error_message,
        }

    config_messages = [HumanMessage(content=f"Collected monitoring config: {state.get('config', {})}")]
    response = model.invoke([SystemMessage(content=_get_ochestrator_prompt())] + config_messages + messages)
    tool_calls = getattr(response, "tool_calls", None) or []
    return {"messages": [response], "tool_call_count": len(tool_calls), "iteration_count": 1, "tool_error_message": ""}

def analyze_data_node(state: AgentState):
    data_collected = state.get("data_collected", {})
    messages = state.get("messages", [])
    response = model.invoke([SystemMessage(content=_get_analyze_prompt())] + messages)
    return {"messages": [response], "iteration_count": state.get("iteration_count", 0) + 1}

#### defining edges
def route_after_request(state: AgentState) -> Literal["request_clarification", "orchestration"]:
    if not state.get("is_complete", False):
        return "request_clarification"
    return 'orchestration'
def route_after_orchestrator_call(state: AgentState) -> Literal["tools", "analyze_data_node", "request_clarification"]:
    if state.get("tool_error_message"):
        return "request_clarification"

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
#### defining graph
def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node(request_clarification)
    builder.add_node('summarize_history',summarize_history)
    builder.add_node("orchestration", orchestrator_node)
    builder.add_node("tools", tool_node)
    builder.add_node("analyze_data_node", analyze_data_node)

    builder.add_edge(START, "summarize_history")
    builder.add_edge("summarize_history", "intake")
    builder.add_conditional_edges("intake", route_after_request)
    builder.add_edge("request_clarification", "summarize_history")
    builder.add_edge("tools", "orchestration")
    builder.add_conditional_edges("orchestration", route_after_orchestrator_call, {"tools": "tools", "analyze_data_node": "analyze_data_node", "request_clarification": "request_clarification"})
    builder.add_edge("analyze_data_node", END)

    return builder.compile(checkpointer=memory, interrupt_before=["request_clarification"])


graph = build_graph()

if __name__ == "__main__":
    config = {"configurable": {"thread_id": "1"}}
    default_server_config = setting.server_config

    task = input("Enter your task: ").strip()

    stream_input = {"messages": [HumanMessage(content=task)]}
    while True:
        for event in graph.stream(stream_input, config=config):
            print(f"Event: {event}")
        current_state = graph.get_state(config)
        # print(f"Current state: {current_state}")
        if current_state.next:
                # print("Provide more infomation")
                messages = current_state.values["messages"][-1]
                ai_message = messages.content
                print(f"Clarification question: {ai_message}")
                user_input = input("Your answer: ").strip()
                stream_input = {"messages": [HumanMessage(content=user_input)]}
                graph.update_state(config, stream_input)
                stream_input = None
        else:
            print("Task complete!")
            break


        
