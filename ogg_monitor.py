#### import libraries
from __future__ import annotations
from typing import Annotated
import operator
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from pydantic import BaseModel, Field
from langgraph.graph import MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode

from paramiko_tools import check_disk, check_log, connect_ssh, get_process

from config import setting
#### defining model
planner_model = ChatOpenAI(model="gpt-4o-mini", temperature=0.1)

memory = InMemorySaver()
#### defining prompts
def get_intake_node_prompt():
    return """
You are an config verifier. 
Your task is checking if the existing config is fully filled or not.
Rules:
1. If the config is fully filled (not empty), return is_clear=true and return the config, else return is_clear=false and ask the user for missing fields.
2. No follow-up questions, no explanations
Input:
- User Message: the last message from user
- Existing Config: the config provided by user or extracted from the user message
- Conversation Summary: a brief summary of the conversation history, it can be empty if no conversation
Output:
- is_clear: if the Updated Config is fully fill return true, else return false
- config: the updated config
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
    # print(conversation)
    config = state.get("config", setting.server_config)
    context_section = f"Context Section:\nUser Message: {last_message}\nExisting Config:\n{config}\n Conversation Summary:\n{state.get('conversation_summary', '')}"
    print(context_section)
    llm_with_structure = planner_model.with_config(temperature=0.1).with_structured_output(QueryAnalysis)
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

    summary_response = planner_model.with_config(temperature=0.2).invoke([SystemMessage(content=get_conversation_summary_prompt()), HumanMessage(content=conversation)])
    return {"conversation_summary": summary_response.content}
#### defining edges
def route_after_request(state: AgentState) -> Literal["request_clarification", END]:
    if not state.get("is_complete", False):
        return "request_clarification"
    return END
#### defining graph
def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node(request_clarification)
    builder.add_node('summarize_history',summarize_history)

    builder.add_edge(START, "summarize_history")
    builder.add_edge("summarize_history", "intake")
    builder.add_conditional_edges("intake", route_after_request)
    builder.add_edge("request_clarification", "summarize_history")


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


        
