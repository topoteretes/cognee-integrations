"""An Agno agent stores a fact with CogneeTools, then a fresh agent recalls it.

Needs an OpenAI key in LLM_API_KEY (see .env.template).
"""

import os

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from cognee_integration_agno import CogneeTools

model = OpenAIChat(id="gpt-4o-mini", api_key=os.getenv("LLM_API_KEY"))

writer = Agent(model=model, tools=[CogneeTools()])
writer.print_response("Remember that we signed a contract with Meditech Solutions for £1.2M.")

# A fresh agent, same persistent memory:
reader = Agent(model=model, tools=[CogneeTools()])
reader.print_response("What is the Meditech Solutions contract worth? Check your memory.")
