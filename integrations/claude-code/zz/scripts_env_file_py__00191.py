URL = "https://api.cognee.ai/{base}/x"
            try:
                if url:
                    _URL_RECORD.parent.mkdir(parents=True, exist_ok=True)
                    _URL_RECORD.write_text(json.dumps({"url": url}), encoding="utf-8")
                else:
                    _URL_RECORD.unlink()
            except Exception:
                pass
    else:
        try:
