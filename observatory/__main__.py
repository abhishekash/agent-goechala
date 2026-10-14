from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .core import Store, normalize_native
from .server import serve


def main():
    parser = argparse.ArgumentParser(prog="python -m observatory",
        description="Start the terminal cockpit with no subcommand, or choose a command below.")
    parser.add_argument("--db", default="observatory.db")
    sub = parser.add_subparsers(dest="command")
    cockpit = sub.add_parser("cockpit", help="one-command terminal dashboard and optional agent launch")
    cockpit.add_argument("--port", type=int, default=14318, help="local receiver port (default: 14318)")
    cockpit.add_argument("--endpoint", default=os.environ.get("OBSERVATORY_ENDPOINT"),
                         help="connect to an existing receiver instead of starting one")
    cockpit.add_argument("--service", default=os.environ.get("OTEL_SERVICE_NAME", "my-agent"))
    cockpit.add_argument("--python", help="Python executable in the agent's environment")
    cockpit.add_argument("--module", action="store_true", help="run the agent as a Python module")
    cockpit.add_argument("--capture-content", action="store_true", help="include prompts and model responses")
    cockpit.add_argument("--new-window", action="store_true", help="open the cockpit in a new terminal window")
    cockpit.add_argument("--once", action="store_true", help="print one snapshot and exit")
    cockpit.add_argument("--no-local-agents", dest="local_agents", action="store_false",
                         help="disable local Codex / Claude Code session discovery in a receiver started here")
    cockpit.add_argument("--metadata-only", action="store_true", help="omit native session text, commands and outputs")
    cockpit.add_argument("target", nargs="?", help="optional Python agent script or module to launch")
    cockpit.add_argument("arguments", nargs=argparse.REMAINDER, help="arguments passed to the agent")
    server = sub.add_parser("serve", help="run local dashboard and OTLP/HTTP receiver")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=4318)
    server.add_argument("--token", default=os.environ.get("OBSERVATORY_TOKEN"),
                        help="optional bearer token (or OBSERVATORY_TOKEN environment variable)")
    server.add_argument("--watch-jsonl", type=Path, action="append", default=[],
                        help="tail a harness JSONL file as spans are written; repeatable")
    server.add_argument("--watch-service", default="agent-harness")
    server.add_argument("--retention-days", type=float,
                        help="delete complete traces and telemetry older than this many days")
    server.add_argument("--no-local-agents", dest="local_agents", action="store_false",
                        help="disable automatic local Codex / Claude Code session discovery")
    server.add_argument("--metadata-only", action="store_true", help="omit native session text, commands and outputs")
    launcher = sub.add_parser("run", help="automatically capture model calls from a Python agent")
    launcher.add_argument("--endpoint", default=os.environ.get("OBSERVATORY_ENDPOINT", "http://127.0.0.1:4318"),
                          help="dashboard address (or OBSERVATORY_ENDPOINT)")
    launcher.add_argument("--service", default=os.environ.get("OTEL_SERVICE_NAME", "my-agent"),
                          help="name shown in the dashboard")
    launcher.add_argument("--module", action="store_true", help="run an importable module instead of a script")
    launcher.add_argument("--capture-content", action="store_true", help="include prompts and model responses")
    launcher.add_argument("target", help="your agent's Python script or module")
    launcher.add_argument("arguments", nargs=argparse.REMAINDER, help="arguments passed to your agent")
    importer = sub.add_parser("import-jsonl", help="import agent-harness span JSONL")
    importer.add_argument("path", type=Path)
    importer.add_argument("--service", default="agent-harness")
    evaluations = sub.add_parser("import-evals", help="join agent-evals results.json outcomes to traces")
    evaluations.add_argument("path", type=Path)
    prune = sub.add_parser("prune", help="delete telemetry older than the retention period")
    prune.add_argument("--older-than-days", type=float, required=True)
    args = parser.parse_args()
    if args.command in {None, "cockpit"}:
        from .terminal import CockpitError, run_cockpit
        options = {key: getattr(args, key) for key in ("port", "endpoint", "service", "python", "module", "capture_content", "new_window", "once", "target", "arguments", "local_agents", "metadata_only")} if args.command else {
            "endpoint": os.environ.get("OBSERVATORY_ENDPOINT"),
            "service": os.environ.get("OTEL_SERVICE_NAME", "my-agent"),
        }
        try:
            run_cockpit(db_path=args.db, **options)
        except CockpitError as exc:
            parser.exit(2, f"Observatory: {exc}\n")
    elif args.command == "serve":
        serve(args.db, args.host, args.port, args.token, args.watch_jsonl,
              args.watch_service, args.retention_days, args.local_agents, args.metadata_only)
    elif args.command == "run":
        from .auto import SetupError, run_agent
        try:
            run_agent(args.target, args.arguments, endpoint=args.endpoint, service=args.service,
                      module=args.module, capture_content=args.capture_content)
        except SetupError as exc:
            parser.exit(2, f"Observatory: {exc}\n")
        except KeyboardInterrupt:
            parser.exit(130)
    elif args.command == "import-jsonl":
        spans = []
        with args.path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        spans.append(normalize_native(json.loads(line), args.service))
                    except (ValueError, KeyError, TypeError) as exc:
                        parser.error(f"{args.path}:{number}: {exc}")
        print(f"Imported {Store(args.db).ingest(spans)} spans")
    elif args.command == "import-evals":
        payload = json.loads(args.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            parser.error("expected results.json with a results array")
        print(f"Imported {Store(args.db).ingest_outcomes(payload['results'], source=str(args.path))} outcomes")
    else:
        deleted = Store(args.db).prune(args.older_than_days)
        print(json.dumps(deleted, sort_keys=True))


if __name__ == "__main__":
    main()
