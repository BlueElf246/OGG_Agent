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
You are an config verifier
Your task is to return the config based on context_section.
Rule:
1. If any field in the Updated Config is EMPTY (for example ''), return is_clear=False,config={Updated Config} and provide clarification_needed with details about what's missing or incomplete.
2. If the config is complete and valid, return is_clear=True and config={Updated Config}.
Workflow:
1. Read the Exisiting Config
2. Create Updated Config from the Existing Config and Information from User Message if needed.  
3. return Update Config
"""
def get_conversation_summary_prompt() -> str:
    return """You are an expert conversation summarizer.

Your task is to create a brief 1-2 sentence summary of the conversation (max 30-50 words).

Include:
- Main topics discussed
- Important facts or entities mentioned
- Any unresolved questions if applicable
- Sources file name (e.g., file1.pdf) or documents referenced

Exclude:
- Greetings, misunderstandings, off-topic content.

Output:
- Return ONLY the summary.
- Do NOT include any explanations or justifications.
- If no meaningful topics exist, return an empty string.
"""
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
    last_message = state["messages"][-1] if state["messages"] else None
    # print(conversation)
    config = state.get("config", setting.server_config)
    context_section = f"Context Section:\nUser Message: {last_message}\nExisting Config:\n{config}\n Conversation Summary:\n{state.get('conversation_summary', '')}"
    print(context_section)
    llm_with_structure = planner_model.with_config(temperature=0.1).with_structured_output(QueryAnalysis)
    response = llm_with_structure.invoke([SystemMessage(content=get_intake_node_prompt()) , HumanMessage(content=context_section)])
    print('response: ', response)
    # print('config: ', response.config)
    # print('clarification_needed: ', response.clarification_needed)
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
    builder.add_edge("request_clarification", "intake")


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


        
