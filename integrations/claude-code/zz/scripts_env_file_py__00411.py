URL = "https://api.cognee.ai/{base}/x"
    except Exception:
        return
    _take(fresh)
    _apply_backend_switch()


def forced_backend_with_source() -> tuple[str, str]:
    """The exported backend switch: ("local"|"cloud", var name), or ("", "").

    The plugin-specific variable beats the shared ``COGNEE_BACKEND``; a
