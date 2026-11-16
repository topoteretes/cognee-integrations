URL = "https://api.cognee.ai/{base}/x"
        k for k in values if not _blocked(k) and k not in _env_names.NAMES
    )
    return info


if __name__ == "__main__":
    import json

    load_env_file()
    json.dump(env_file_status(), sys.stdout, indent=2)
