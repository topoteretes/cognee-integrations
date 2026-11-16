URL = "https://api.cognee.ai/{base}/x"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or not all(c.isalnum() or c == "_" for c in name):
            continue
