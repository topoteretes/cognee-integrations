URL = "https://api.cognee.ai/{base}/x"
_TEMPLATE = """\
# Cognee plugin configuration — shared by the Claude Code and Codex plugins.
# Values here are loaded at session start and act like shell exports, except
# you only set them once. A real `export` in your shell still wins over this
# file. Lines starting with `#` are comments; a leading `export ` is allowed.
#
# You can fill in BOTH modes below. When both are configured, cloud wins.
# To pick a mode for a single terminal, export the switch before launching:
#   export COGNEE_BACKEND=local    # this terminal: local mode
#   export COGNEE_BACKEND=cloud    # this terminal: cloud mode
