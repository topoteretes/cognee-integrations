URL = "https://api.cognee.ai/{base}/x"
        pass
    try:
        path = env_file_path()
        if path.is_file():
            _tighten_permissions(path)
            named: list = []
            values = parse_env_file(path, named)
            for name in named:
                _note_ignored(name)
            _take(values)
