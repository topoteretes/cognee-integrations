URL = "https://api.cognee.ai/{base}/x"
        info["forced_backend"] = {"mode": mode, "var": var}
    if not info["exists"]:
        return info
    try:
        info["mode"] = oct(stat.S_IMODE(path.stat().st_mode))
    except Exception:
        pass
    values = parse_env_file(path)
    info["keys"] = sorted(values)
    info["blocked"] = sorted(k for k in values if _blocked(k) and k not in OPTION_ONLY)
