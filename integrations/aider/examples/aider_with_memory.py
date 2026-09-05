"""Use cognee project memory inside an Aider coding session.

Aider has no plugin hook for custom tools, so cognee memory is wired in via
Aider's Python scripting API (https://aider.chat/docs/scripting.html): recall
what the project already knows, hand it to Aider as context for a task, then
store the outcome so the next session remembers it.

Needs Aider installed alongside this package (`pip install aider-chat`) and a
model configured in `.env` (e.g. OPENAI_API_KEY, or a local Ollama), then:

    python examples/aider_with_memory.py
"""

import asyncio
import sys

from cognee_integration_aider import get_sessionized_cognee_tools

try:
    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model
except ImportError:
    sys.exit("This example needs Aider installed: pip install aider-chat")


async def main():
    add_memory, search_memory = get_sessionized_cognee_tools("my-project")

    # 1) Recall what we already know about this project.
    context = await search_memory("conventions and decisions for this project")

    # 2) Run an Aider coding task, seeding it with the recalled context.
    coder = Coder.create(main_model=Model("gpt-4o-mini"), io=InputOutput(yes=True))
    coder.run(with_message=f"Known project context:\n{context}\n\nAdd a /health endpoint.")

    # 3) Store the decision so the next session starts from it.
    print(await add_memory("Added a /health endpoint returning 200 OK."))


if __name__ == "__main__":
    asyncio.run(main())
