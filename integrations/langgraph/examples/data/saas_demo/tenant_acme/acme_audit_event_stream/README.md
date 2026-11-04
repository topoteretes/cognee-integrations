# acme_audit_event_stream (tenant_acme) — Temporal Dataset

**Purpose:** Time-ordered event stream for Temporal Cognify.

Use this dataset with:
- `await cognee.cognify(datasets=["acme_audit_event_stream"], temporal_cognify=True)`
- Then query with `SearchType.TEMPORAL` for before/after/between.

**Recommended NodeSets**
- `events`
- `temporal`
- `source:event_bus`

**Key Incident Window**
- 2026-01-03 08:15Z → 10:05Z
