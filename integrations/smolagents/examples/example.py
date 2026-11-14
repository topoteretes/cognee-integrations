"""A CodeAgent remembers a fact, then a fresh agent recalls it.

Needs an OpenAI key in LLM_API_KEY (see .env.template).
"""

import os

from cognee_integration_smolagents import cognee_tools
from smolagents import CodeAgent, OpenAIModel

model = OpenAIModel(model_id="gpt-4o-mini", api_key=os.getenv("LLM_API_KEY"))

writer = CodeAgent(tools=cognee_tools(), model=model)
writer.run("Remember that we signed a contract with Meditech Solutions for £1.2M.")

# A fresh agent, same persistent memory:
reader = CodeAgent(tools=cognee_tools(), model=model)
print(reader.run("What is the Meditech Solutions contract worth?"))
