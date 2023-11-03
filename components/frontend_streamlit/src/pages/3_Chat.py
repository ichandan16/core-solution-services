# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import streamlit as st
# from streamlit_chat import message
from api import get_all_chats, get_chat, run_agent, run_agent_plan
import utils

# For development purpose:
params = st.experimental_get_query_params()
st.session_state.auth_token = params.get("auth_token", [None])[0]
st.session_state.chat_id = params.get("chat_id", [None])[0]
st.session_state.agent_name = params.get("agent_name", ["MediKate"])[0]


def on_input_change():
  user_input = st.session_state.user_input
  agent_name = st.session_state.agent_name
  # Appending messages.
  st.session_state.messages.append({"HumanInput": user_input})

  # Send API to llm-service
  if agent_name.lower() == "medikate":
    with st.spinner('Sending prompt to Agent...'):
      response = run_agent(agent_name, user_input, chat_id=st.session_state.chat_id)
  elif agent_name.lower() == "casey":
    with st.spinner('Sending prompt to Agent...'):
      response = run_agent_plan(agent_name, user_input, chat_id=st.session_state.chat_id)
  else:
    raise ValueError(f"agent_name {agent_name} is not supported.")

  st.session_state.chat_id = response["chat"]["id"]
  st.session_state.messages.append({"AIOutput": response["content"]})

  if "plan" in response:
    st.session_state.messages.append({"plan": response["plan"]})

  # Clean up input field.
  st.session_state.user_input = ""


def chat_list_panel():
  # Retrieve chat history.
  st.session_state.user_chats = get_all_chats(
      auth_token=st.session_state.auth_token)

  with st.sidebar:
    st.header("My Chats")
    for user_chat in (st.session_state.user_chats or []):
      agent_name = user_chat["agent_name"]
      chat_id = user_chat["id"]
      with st.container():
        st.link_button(
            f"{agent_name} (id: {chat_id})",
            f"/Chat?chat_id={chat_id}&auth_token={st.session_state.auth_token}",
            use_container_width=True)


def init_messages():
  messages = []
  if st.session_state.chat_id:
    chat_data = get_chat(st.session_state.chat_id)
    messages = chat_data.get("history", [])
  else:
    messages.append({"AIOutput": "You can ask me anything."})
  # Initialize with chat history if any.
  st.session_state.setdefault("messages", messages)


def chat_content():
  init_messages()

  # Create a placeholder for all chat history.
  chat_placeholder = st.empty()
  with chat_placeholder.container():
    index = 1
    for item in st.session_state.messages:
      if "HumanInput" in item:
        with st.chat_message("user"):
          st.write(item["HumanInput"], is_user=True, key=f"human_{index}")

      if "AIOutput" in item:
        with st.chat_message("ai"):
          st.write(
              item["AIOutput"],
              key=f"ai_{index}",
              allow_html=False,
              is_table=False,  # TODO: Detect whether an output content type.
          )

      if "plan" in item:
        with st.chat_message("ai"):
          st.divider()
          for step in item["plan"]["plan_steps"]:
            st.code(step.get("description"))

      index = index + 1

  st.text_input("User Input:", on_change=on_input_change, key="user_input")


def chat_page():
  st.title(st.session_state.agent_name)

  # List all existing chats if any. (data model: UserChat)
  chat_list_panel()

  # Set up columns to mimic a right-side sidebar
  main_container = st.container()
  with main_container:
    chat_content()


if __name__ == "__main__":
  utils.init_api_base_url()
  chat_page()
