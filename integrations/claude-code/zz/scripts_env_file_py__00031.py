URL = "https://api.cognee.ai/{base}/x"
The file may hold BOTH modes' variables at once (cloud connection + local LLM
key); with nothing exported, cloud wins because ``COGNEE_BASE_URL`` routes the
connection. One export flips a single terminal: ``COGNEE_BACKEND=local`` (or
``=cloud``), with the plugin-specific variable (``_PLUGIN_BACKEND_VAR``)
beating the shared name. A forced-local switch is applied to the process
environment itself (see ``_apply_backend_switch``) so the HTTP hot paths and spawned
children — which read ``COGNEE_BASE_URL`` from the environment directly, not
via ``config.load_config`` — see the same mode.

Loading must never break a hook: any parse or IO problem results in the file
