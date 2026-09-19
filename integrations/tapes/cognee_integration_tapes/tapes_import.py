import os
import sys
import json
import fcntl
import hashlib
import asyncio
import logging
import contextlib
from dataclasses import dataclass
from pathlib import Path
import cognee
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("cognee_integration_tapes")

KEYS = [
    ("command",),
    ("skill",),
    ("subagent_type", "prompt"),
    ("file_path",), ("filePath",), ("notebook_path",),
    ("cron", "recurring"),
    ("channel_id", "team"),
    ("query",),
    ("url",),
    ("message",),
    ("taskId", "subject", "description"),
    ("status",),
]
MAX_KEY_LENGTH = 80


@dataclass(frozen=True)
class Config:
    tapes_base_url: str
    dataset_name: str
    manifest_path: Path
    graph_output_path: str


def load_config() -> Config:
    dataset_name = os.environ.get("TAPES_DATASET", "tapes_sessions")
    return Config(
        tapes_base_url=os.environ.get("TAPES_BASE_URL", "http://localhost:8081"),
        dataset_name=dataset_name,
        manifest_path=Path(os.environ.get("MANIFEST_PATH", f".cognee-manifest-{dataset_name}.json")),
        graph_output_path=os.environ.get("GRAPH_OUTPUT", "graph.html"),
    )


@contextlib.contextmanager
def manifest_lock(config: Config):
    #Exclusive file lock so two overlapping runs can't read/write manifest.json concurrently and corrupt or silently drop each other's progress

    lock_path = config.manifest_path.with_suffix(config.manifest_path.suffix + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def get_status(session: dict) -> str:
    rollup = session.get("session", {}).get("rollup") or {}
    return rollup.get("status", "")


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def get_last_seen_at(session: dict) -> str | None:
    #NVERIFIED — no /export call has ever successfully returned a payload
    #to check this against (every attempt during development 404'd; only
    #demo/seed sessions were available locally). This checks the nested
    #ession-object location, then falls back to top-level, as a best guess
    #at two plausible shapes — not a confirmed contract.
 
    #If this returns None, the checkpoint silently fails to advance and every
    #run re-fetches full history (harmless — the manifest hash still blocks
    #re-ingestion — but wasteful and worth noticing). The warning below exists
    #so that failure mode is visible instead of silent. TODO: verify against
    #a real, successfully exported session and remove this warning once confirmed.
    return (
        session.get("session", {}).get("last_seen_at")
        or session.get("last_seen_at")
    )


def summarize_tool_input(tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""

    for key_group in KEYS:
        matched = [k for k in key_group if k in tool_input]
        if matched:
            parts = []
            for k in matched:
                value = str(tool_input[k])
                if len(value) > MAX_KEY_LENGTH:
                    value = value[:MAX_KEY_LENGTH] + "..."
                parts.append(f"{k}: {value}")
            return f"({', '.join(parts)})"
    return ""


def fetch_all_session_ids(config: Config, since_timestamp: str | None = None) -> list[str]:
    url = f"{config.tapes_base_url}/v1/sessions"
    session_ids = []
    cursor = None

    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to list sessions (cursor=%s): %s — stopping pagination with %d session id(s) collected so far.",
                cursor, e, len(session_ids),
            )
            break

        for item in payload.get("items", []):
            last_seen_at = item.get("last_seen_at")
            if since_timestamp is None or not last_seen_at:
                session_ids.append(item["id"])
                continue

            try:
                if parse_ts(last_seen_at) > parse_ts(since_timestamp):
                    session_ids.append(item["id"])
            except ValueError:
                if last_seen_at >= since_timestamp:
                    session_ids.append(item["id"])

        cursor = payload.get("next_cursor")
        if not cursor:
            break

    return session_ids


def fetch_session(config: Config, session_id: str) -> dict:
    url = f"{config.tapes_base_url}/v1/sessions/{session_id}/export"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_session_batch(config: Config, since_timestamp: str | None = None) -> list[dict]:
    session_ids = fetch_all_session_ids(config, since_timestamp)
    sessions = []
    for session_id in session_ids:
        try:
            sessions.append(fetch_session(config, session_id))
        except requests.exceptions.RequestException as e:
            logger.error("Failed to fetch session %s: %s — skipping, continuing with the rest.", session_id, e)
            continue
    return sessions


def load_manifest(config: Config) -> dict:
    if config.manifest_path.exists():
        try:
            return json.loads(config.manifest_path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_manifest(config: Config, manifest: dict) -> None:
    config.manifest_path.write_text(json.dumps(manifest, indent=4))


def build_metadata(session: dict) -> dict:
    return {
        "session": {
            "id": session.get("session", {}).get("id", ""),
            "name": session.get("session", {}).get("name", ""),
            "display_title": session.get("session", {}).get("display_title", ""),
            "harness_id": session.get("session", {}).get("harness_id", ""),
            "started_at": session.get("session", {}).get("started_at", ""),
            "rollup": {"model": (session.get("session", {}).get("rollup") or {}).get("model", "")},
        }
    }


def read_trace(session: dict) -> str:
    session_text = ""
    traces = session.get("traces", []) if isinstance(session, dict) else []

    for trace_entry in traces:
        if not isinstance(trace_entry, dict):
            continue

        trace = trace_entry.get("trace", {})
        spans = trace_entry.get("spans", [])
        if not isinstance(trace, dict):
            continue

        user_prompt = trace.get("user_prompt", "")
        turn_text = f"User: {user_prompt}\n\n"

        # Only "main" LLM spans become transcript text — this deliberately
        # skips injected system context, permission-check offshoots, and
        # other harness-internal spans that would bloat the graph without
        # adding conversational signal.
        main_spans = [
            span for span in spans
            if isinstance(span, dict) and span.get("kind") == "llm" and span.get("call_kind") == "main"
        ]
        main_spans.sort(key=lambda s: s.get("seq", 0))

        assistant_parts = []
        for span in main_spans:
            for block in span.get("output", []):
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        assistant_parts.append(text)
                elif block_type == "tool_use":
                    tool_name = block.get("tool_name", "unknown")
                    detail = summarize_tool_input(block.get("tool_input", {}))
                    assistant_parts.append(f"[used tool: {tool_name}{detail}]")
                elif block_type == "thinking":
                    continue

        turn_text += "Assistant: " + "\n".join(assistant_parts) + "\n\n"
        session_text += turn_text

    return session_text


async def process_session(config: Config, session: dict, manifest: dict) -> tuple[dict, bool, bool]:
    session_id = session.get("session", {}).get("id", "")
    status = get_status(session)

    if status != "completed":
        # Incomplete sessions intentionally do NOT advance the checkpoint
        # (see main()) — this session gets re-checked on every subsequent
        # run until it completes, rather than being silently skipped forever.
        logger.info("Session %s status is '%s', skipping.", session_id, status)
        return manifest, False, False

    metadata = build_metadata(session)
    header = (
        f"Session ID: {metadata['session']['id']}\n"
        f"Name: {metadata['session']['name']}\n"
        f"Display title: {metadata['session']['display_title']}\n"
        f"Harness: {metadata['session']['harness_id']}\n"
        f"Started: {metadata['session']['started_at']}\n"
        f"Model: {metadata['session']['rollup']['model']}\n\n"
    )
    session_text = header + read_trace(session)

    if not session_text.strip():
        logger.info("Session %s produced an empty transcript, skipping.", session_id)
        return manifest, False, False

    session_hash = hashlib.sha256(session_text.encode("utf-8")).hexdigest()
    manifest.setdefault("sessions", {})

    if manifest["sessions"].get(session_id) == session_hash:
        logger.info("No changes for session %s, skipping.", session_id)
        return manifest, True, False

    manifest["sessions"][session_id] = session_hash
    logger.info("Ingesting session %s.", session_id)
    await cognee.add(data=session_text, dataset_name=config.dataset_name)
    return manifest, True, True


async def main():
    config = load_config()

    with manifest_lock(config):
        manifest = load_manifest(config)
        checkpoint = manifest.get("last_synced_at")  # None on first run
        sessions = fetch_session_batch(config, since_timestamp=checkpoint)
        logger.info("Fetched %d session(s).", len(sessions))

        latest_seen = None
        any_added = False

        for session in sessions:
            manifest, was_completed, was_added = await process_session(config, session, manifest)
            save_manifest(config, manifest)  # incremental save, protects progress if a later session fails
            any_added = any_added or was_added

            if was_completed:
                latest_seen_at = get_last_seen_at(session)
                if latest_seen_at:
                    dt = parse_ts(latest_seen_at)
                    if latest_seen is None or dt > latest_seen:
                        latest_seen = dt

        if latest_seen:
            manifest["last_synced_at"] = latest_seen.isoformat()
            logger.info("Updated last_synced_at to %s", manifest["last_synced_at"])
            save_manifest(config, manifest)

    if any_added:
        await cognee.cognify(datasets=[config.dataset_name])
        logger.info("Cognify run complete.")
    else:
        logger.info("No new content added — skipping cognify.")

    await cognee.visualize_graph(
        destination_file_path=os.path.abspath(config.graph_output_path),
        dataset=config.dataset_name,
        full=True,
    )
    logger.info("Graph visualization saved to: %s", config.graph_output_path)


def cli():
    async def run_and_drain():
        await main()
        await cognee.disconnect()
    asyncio.run(run_and_drain())


if __name__ == "__main__":
    cli()