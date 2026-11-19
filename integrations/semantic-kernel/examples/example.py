"""A Semantic Kernel agent stores a fact with CogneeMemoryPlugin, then recalls it.

Needs an OpenAI key in LLM_API_KEY (see .env.template). The model decides
when to call cognee's remember/search functions via auto function calling.
"""

import asyncio
import os

from cognee_integration_semantic_kernel import CogneeMemoryPlugin
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior
from semantic_kernel.connectors.ai.open_ai import (
    OpenAIChatCompletion,
    OpenAIChatPromptExecutionSettings,
)
from semantic_kernel.functions import KernelArguments


def build_kernel() -> Kernel:
    kernel = Kernel()
    kernel.add_service(
        OpenAIChatCompletion(ai_model_id="gpt-4o-mini", api_key=os.getenv("LLM_API_KEY"))
    )
    kernel.add_plugin(CogneeMemoryPlugin(), plugin_name="cognee")
    return kernel


async def main() -> None:
    kernel = build_kernel()
    settings = OpenAIChatPromptExecutionSettings(
        function_choice_behavior=FunctionChoiceBehavior.Auto()
    )
    arguments = KernelArguments(settings=settings)

    await kernel.invoke_prompt(
        "Remember that we signed a contract with Meditech Solutions for £1.2M.",
        arguments=arguments,
    )

    # A fresh kernel, same persistent memory:
    answer = await build_kernel().invoke_prompt(
        "What is the Meditech Solutions contract worth? Check your memory.",
        arguments=arguments,
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
