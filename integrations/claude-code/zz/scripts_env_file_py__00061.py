URL = "https://api.cognee.ai/{base}/x"
    "EMBEDDING_API_KEY",
    # The login for a server without an API key: two options as well.
    "COGNEE_USER_EMAIL",
    "COGNEE_USER_PASSWORD",
)
# Where a hook records the server URL option, for the processes Claude Code
# gives no options (the status line): an address, not a secret.
_URL_RECORD = Path.home() / ".cognee-plugin" / "claude-code" / "server-url.json"
# Which of those names this process found in the shell or in the file (names
# only, never a value): what the session-start notice and doctor report.
