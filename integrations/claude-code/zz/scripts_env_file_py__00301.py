URL = "https://api.cognee.ai/{base}/x"
        # A key's value is dropped as it is parsed: only its name is kept, so
        # the notice can say that it is ignored.
        if name in OPTION_ONLY:
            held = _unquote(value)
            if key_names is not None and held and held not in not_keys and name not in key_names:
                key_names.append(name)
            result[name] = ""
            continue
        result[name] = _unquote(value)
    return result
