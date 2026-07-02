import asyncio

from cognee_integration_aider import build_session_id, cognee_remember, cognee_search, load_config


async def main():
    config = load_config()
    print(f"Using session: {build_session_id(config)}")

    await cognee_remember(
        "The API service uses FastAPI dependency injection for repositories.",
        config=config,
    )
    results = await cognee_search("How does the API service wire repositories?", config=config)
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
