import os

import cognee
from cognee.api.v1.config import config
from cognee_integration_strands import cognee_tools, run_cognee_task
from dotenv import load_dotenv
from strands import Agent
from strands.models.openai import OpenAIModel

load_dotenv()

_COGNEE_DIR = os.path.join(os.path.dirname(__file__), "../.cognee")


def main():
    config.data_root_directory(os.path.join(_COGNEE_DIR, "data"))
    config.system_root_directory(os.path.join(_COGNEE_DIR, "system"))
    run_cognee_task(cognee.forget(everything=True))

    model = OpenAIModel(client_args={"api_key": os.getenv("LLM_API_KEY")}, model_id="gpt-4o")
    agent = Agent(model=model, tools=cognee_tools())

    agent('Remember this contract: "Meditech Solutions" — healthcare industry, worth £1.2M.')
    agent('Remember this contract: "QuantumSoft" — technology industry, worth £5.5M.')
    agent('Remember this contract: "Orion Retail Group" — retail industry, worth £850K.')

    # A fresh agent has no conversation history — it answers only from cognee's memory.
    fresh_agent = Agent(model=model, tools=cognee_tools())
    print(fresh_agent("Search memory: which contracts are in the healthcare industry?"))


if __name__ == "__main__":
    main()
