"""Command line entrypoint for Aider/Cognee memory tools."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Sequence

from .config import load_config
from .doctor import render_report, run_doctor
from .session import build_session_id
from .tools import cognee_remember, cognee_search, cognee_tool_specs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cognee-aider")
    parser.add_argument("--session-id", help="Optional session suffix for this Aider run.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    remember = subparsers.add_parser("remember", help="Store memory in Cognee.")
    remember.add_argument("data", help="Text to store.")

    search = subparsers.add_parser("search", help="Search Cognee memory.")
    search.add_argument("query_text", help="Question or search query.")

    subparsers.add_parser("session", help="Print the resolved Cognee session ID.")
    subparsers.add_parser("config", help="Print the resolved configuration.")
    subparsers.add_parser("specs", help="Print JSON tool specs.")

    doctor = subparsers.add_parser("doctor", help="Run environment and configuration diagnostics.")
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of text.",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings as well as errors.",
    )
    doctor.add_argument(
        "--skip-network",
        action="store_true",
        help="Skip Cognee service health checks.",
    )
    doctor.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Timeout in seconds for Docker, database, and HTTP checks.",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    config = load_config()

    if args.command == "remember":
        print(await cognee_remember(args.data, config=config, session_id=args.session_id))
        return 0
    if args.command == "search":
        results = await cognee_search(
            args.query_text,
            config=config,
            session_id=args.session_id,
        )
        print(json.dumps(results))
        return 0
    if args.command == "session":
        print(build_session_id(config, session_id=args.session_id))
        return 0
    if args.command == "config":
        print(json.dumps(config.__dict__, indent=2, sort_keys=True))
        return 0
    if args.command == "specs":
        print(json.dumps(cognee_tool_specs(), indent=2, sort_keys=True))
        return 0
    if args.command == "doctor":
        report = run_doctor(
            config,
            timeout=max(0.1, args.timeout),
            skip_network=args.skip_network,
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            print(render_report(report, strict=args.strict))
        return report.exit_code(strict=args.strict)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
