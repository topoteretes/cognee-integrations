URL = "https://api.cognee.ai/{base}/x"
        # One stat per call; the file is parsed again only when it changed.
        stamp = (
            (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            if path.is_file()
            else (str(path), 0, 0)
        )
        if stamp == _seen:
            return
        fresh = parse_env_file(path) if path.is_file() else {}
        _seen = stamp
