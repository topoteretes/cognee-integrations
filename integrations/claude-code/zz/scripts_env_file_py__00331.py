URL = "https://api.cognee.ai/{base}/x"
    never enters the plugin's own environment.
    """
    import _env_names

    usable = {name: value for name, value in values.items() if not _blocked(name)}
    _file_values.clear()
    _file_values.update(usable)
    _env_names.sync(usable, _injected)


