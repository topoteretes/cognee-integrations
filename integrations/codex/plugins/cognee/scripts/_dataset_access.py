"""Dataset UUIDs are authoritative; names are only for owned datasets."""

import json
import os
from uuid import UUID


def dataset_id(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return ""


def write_fields(value):
    ident = dataset_id(value)
    return {"dataset_id": ident} if ident else {"dataset_name": value}


def recall_fields(value, scope):
    ident = dataset_id(value)
    fields = {"dataset_ids": [ident]} if ident else ({"datasets": [value]} if value else {})
    # Session history remains bound to ONE dataset. Federated graph recall is
    # a separate read: never send the active session's binding with it.
    if scope == ["graph"]:
        from _plugin_common import load_graph_read_scope

        selected = load_graph_read_scope()
        raw = (
            json.dumps(selected)
            if selected is not None
            else os.environ.get("COGNEE_PLUGIN_READ_DATASET_IDS", "").strip()
        )
        if selected == []:
            return fields, False
        if raw:
            values = json.loads(raw)
            if not isinstance(values, list) or not values or not all(dataset_id(v) for v in values):
                raise ValueError(
                    "COGNEE_PLUGIN_READ_DATASET_IDS must be a nonempty JSON list of UUIDs"
                )
            return {"dataset_ids": list(dict.fromkeys(dataset_id(v) for v in values))}, True
    return fields, False
