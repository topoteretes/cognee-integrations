URL = "https://api.cognee.ai/{base}/x"
    info["ignored_keys"] = ignored_key_names()
    # A shell export (or another source) that set a different value wins over
    # the file; surface those so mode-selection surprises are debuggable.
    import _env_names

    info["overridden"] = sorted(
        _env_names.overridden({k: v for k, v in values.items() if not _blocked(k)})
    )
    # Settings for the local server only: never in the plugin's environment.
    info["passed_to_server"] = sorted(
