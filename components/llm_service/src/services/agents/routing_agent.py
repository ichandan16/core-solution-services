# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Routing Agent """
from typing import List, Tuple
from langchain.agents import AgentExecutor
from common.models import QueryEngine, User, UserChat, Agent
from common.models.agent import AgentCapability
from common.models.llm import CHAT_AI
from common.utils.logging_handler import Logger
from services.agents.db_agent import run_db_agent
from services.agents.agents import BaseAgent
from services.agents.agent_service import (
    agent_plan,
    parse_action_output,
    parse_plan_step,
    run_agent)
from services.agents.utils import agent_executor_arun_with_logs
from services.query.query_service import query_generate

Logger = Logger.get_logger(__file__)

async def run_routing_agent(prompt: str,
                            agent_name: str,
                            user: User,
                            user_chat: UserChat,
                            llm_type: str=None) -> Tuple[str, dict]:
  """
  Determine intent from user prompt for best route to fulfill user
  input.  Then execute that route.
  Args:
    prompt: user prompt
    agent_name: routing agent name.  "default": use the first routing agent
    user: User model for user making request
    user_chat: optional existing user chat object for previous chat history
    llm_type: optional llm_type to use for agents, otherwise llm_type of
      routing agent is used
  Returns:
    tuple of route (AgentCapability value), response data dict
  """

  # get the intent based on prompt by running intent agent
  route, route_logs = await run_intent(
      agent_name, prompt, chat_history=user_chat.history)

  Logger.info(f"Intent chooses this best route: {route}, " \
              f"based on user prompt: {prompt}")
  Logger.info(f"Chosen route: {route}")

  route_parts = route.split(":", 1)
  route_type = route_parts[0]
  agent_logs = None

  # create default chat_history_entry
  chat_history_entry = {
    "route": route_type,
    "route_name": route,
  }

  # get routing agent model
  routing_agent = Agent.find_by_name(agent_name)
  if not routing_agent:
    raise RuntimeError(f"Cannot find model for {agent_name}")

  # llm_type can be passed as an argument
  # otherwise llm_type is whatever is set for routing agent
  if llm_type is None:
    llm_type = routing_agent.llm_type
  if not llm_type:
    raise RuntimeError("Agent {agent_name} does not have llm_type set.")

  # Query Engine route
  if route_type == AgentCapability.QUERY.value:
    # Run RAG via a specific query engine
    query_engine_name = route_parts[1]
    Logger.info("Dispatch to Query Engine: {query_engine_name}")

    query_engine = QueryEngine.find_by_name(query_engine_name)
    Logger.info("Query Engine: {query_engine}")

    query_result, query_references = await query_generate(
          user.id,
          prompt,
          query_engine,
          llm_type,
          sentence_references=True)
    Logger.info(f"Query response="
                f"[{query_result}]")

    response_data = {
      "route": route_type,
      "route_name": f"Query Engine: {query_engine_name}",
      "output": query_result.response,
      "query_engine_id": query_result.query_engine_id,
      "query_references": query_references,
    }
    chat_history_entry = response_data
    chat_history_entry[CHAT_AI] = query_result.response

  # Database route
  elif route_type == AgentCapability.DATABASE.value:
    # Run a query against a DB dataset. Return a dict of
    # "columns: column names, "data": row data
    dataset_name = route_parts[1]

    Logger.info(f"Dispatch to DB Query: {dataset_name}")

    db_result, agent_logs = await run_db_agent(
        prompt, llm_type, dataset_name, user.email)

    # TODO: Update with the output generated from the LLM.
    if db_result.get("data", None):
      response_output = "Here is the database query result in the attached " \
                        "resource."
    else:
      response_output = "Unable to find the query result from the database."

    response_data = {
      "route": route_type,
      "route_name": f"Database Query: {dataset_name}",
      f"{CHAT_AI}": response_output,
      "content": response_output,
      "dataset": dataset_name,
      "resources": db_result["resources"],
    }
    chat_history_entry = response_data

  # Plan route
  elif route_type == AgentCapability.PLAN.value:
    # Run PlanAgent to generate a plan
    output, user_plan = await agent_plan(
        agent_name="Plan", prompt=prompt, user_id=user.id)
    plan_data = user_plan.get_fields(reformat_datetime=True)
    plan_data["id"] = user_plan.id
    chat_history_entry[CHAT_AI] = output
    chat_history_entry["plan"] = plan_data
    agent_logs = output

    response_data = {
      "route": route_type,
      "route_name": AgentCapability.PLAN.value,
      "content": output,
      "plan": plan_data,
    }

  # Anything else including Chat route.
  else:
    # Run with the generic ChatAgent for anything else.
    output = await run_agent("Chat", prompt)
    chat_history_entry[CHAT_AI] = output
    response_data = {
      "route": AgentCapability.CHAT.value,
      "route_name": AgentCapability.CHAT.value,
      "content": output
    }

  # Appending Agent's thought process.
  if agent_logs:
    chat_history_entry["agent_logs"] = agent_logs
    response_data["agent_logs"] = agent_logs
  if route_logs:
    chat_history_entry["route_logs"] = route_logs
    response_data["route_logs"] = route_logs

  # update chat data in response
  user_chat.update_history(custom_entry=chat_history_entry)
  user_chat.save()
  chat_data = user_chat.get_fields(reformat_datetime=True)
  chat_data["id"] = user_chat.id
  response_data["chat"] = chat_data

  Logger.info(f"Dispatch agent {agent_name} response: "
              f"route [{route}] response {response_data}")

  return route, response_data


async def run_intent(
    agent_name: str, prompt: str, chat_history:List = None) -> dict:
  """
  Evaluate a prompt to get the intent with best matched route.

  Args:
      prompt(str): the user input prompt
      agent_name(str): the name of the routing agent, or "default" to
                       use the first in the list
      chat_history(List): any previous chat history for context

  Returns:
      output(str): the output of the agent on the user input
      action_steps: the list of action steps take by the agent for the run
  """

  Logger.info(f"Running dispatch "
              f"with prompt=[{prompt}] and "
              f"chat_history=[{chat_history}]")

  # check for default routing agent
  if agent_name == "default":
    routing_agents = BaseAgent.get_agents_by_capability(
      AgentCapability.ROUTE.value
    )
    agent_name = routing_agents.keys()[0]

  # get llm service routing agent
  llm_service_agent = BaseAgent.get_llm_service_agent(agent_name)

  # load corresponding langchain agent and instantiate agent_executor
  langchain_agent = llm_service_agent.load_langchain_agent()
  intent_agent_tools = llm_service_agent.get_tools()
  Logger.info(f"Routing agent tools [{intent_agent_tools}]")

  agent_executor = AgentExecutor.from_agent_and_tools(
      agent=langchain_agent, tools=intent_agent_tools)

  # get dispatch prompt
  dispatch_prompt = get_dispatch_prompt(llm_service_agent)

  agent_inputs = {
    "input": dispatch_prompt + prompt,
    "chat_history": []
  }

  Logger.info("Running agent executor to get best matched route.... ")
  output, agent_logs = await agent_executor_arun_with_logs(
      agent_executor, agent_inputs)

  Logger.info(f"Agent {agent_name} generated output=[{output}]")
  Logger.info(f"run_intent - agent_logs: \n{agent_logs}")

  routes = parse_action_output("Route:", output) or []
  Logger.info(f"Output routes: {routes}")

  # If no best route(s) found, pass to Chat agent.
  if not routes or len(routes) == 0:
    return AgentCapability.CHAT.value, agent_logs

  # TODO: Refactor this with RoutingAgentOutputParser
  # Get the route for the best matched (first) returned routes.
  route, detail = parse_plan_step(routes[0])[0]
  Logger.info(f"route: {route}, {detail}")

  return route, agent_logs


def get_dispatch_prompt(llm_service_agent: BaseAgent) -> str:
  """ Construct dispatch prompt for intent agent """

  agent_name = llm_service_agent.name

  intent_list_str = ""
  intent_list = [
    f"- {AgentCapability.CHAT.value}" \
    " to to perform generic chat conversation.",
    f"- {AgentCapability.PLAN.value}" \
    " to compose, generate or create a plan.",
  ]
  for intent in intent_list:
    intent_list_str += \
      intent + "\n"

  # get query engines for this agent with their description as topics.
  query_engines = llm_service_agent.get_query_engines(agent_name)
  Logger.info(f"query_engines for {agent_name}: {query_engines}")
  for qe in query_engines:
    intent_list_str += \
      f"- [{AgentCapability.QUERY.value}:{qe.name}]" \
      f" to run a query on a search engine for information (not raw data)" \
      f" on the topics of {qe.description} \n"

  # get datasets for this with their descriptions as topics
  datasets = llm_service_agent.get_datasets(agent_name)
  Logger.info(f"datasets for {agent_name}: {datasets}")
  for ds_name, ds_config in datasets.items():
    description = ds_config["description"]
    intent_list_str += \
      f"- [{AgentCapability.DATABASE.value}:{ds_name}]" \
      f" to use SQL to retrieve rows of data from a database for data " \
      f"related to these areas: {description} \n"

  dispatch_prompt = \
    """The AI Routing Assistant has access to the following routes for a user prompt:
    {intent_list_str}
    Choose one route based on the question below:
    """
  Logger.info(f"dispatch_prompt: \n{dispatch_prompt}")

  return dispatch_prompt
  