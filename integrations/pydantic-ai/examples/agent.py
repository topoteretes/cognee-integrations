"""Run with LLM_API_KEY and OPENAI_API_KEY set; see ../README.md."""

from cognee_integration_pydantic_ai import cognee_toolset
from pydantic_ai import Agent


def main() -> None:
    toolset = cognee_toolset(user_id="retail-client-42", auto_recall=True)
    agent = Agent("openai:gpt-4o-mini", toolsets=[toolset])
    print(agent.run_sync("Remember that store 12 prefers email incident updates.").output)

    # A new agent still sees the same long-term memory in this user's scope.
    fresh_agent = Agent("openai:gpt-4o-mini", toolsets=[toolset])
    print(fresh_agent.run_sync("How should we send incident updates for store 12?").output)


if __name__ == "__main__":
    main()
