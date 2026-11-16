URL = "https://api.cognee.ai/{base}/x"
    carried = [n for n in held.split(",") if n.strip()]
    return sorted(set(carried) | set(_ignored_key_names))


def _note_ignored(name: str) -> None:
    if name not in _ignored_key_names:
        _ignored_key_names.append(name)
    os.environ["COGNEE_PLUGIN_IGNORED_SETTINGS"] = ",".join(ignored_key_names())


