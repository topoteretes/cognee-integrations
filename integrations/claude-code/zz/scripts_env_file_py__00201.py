URL = "https://api.cognee.ai/{base}/x"
            url = str(json.loads(_URL_RECORD.read_text(encoding="utf-8")).get("url") or "")
        except Exception:
            url = ""
    if url:
        # Like an export: the file's own value does not replace it.
        os.environ["COGNEE_BASE_URL"] = url


def cloud_key_option() -> str:
    """The Cognee API key option as Claude Code handed it to this process, or ""."""
