"""Local development and recovery commands; no command provisions AWS resources."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .engine import Engine, validate_state
from .errors import OrderFlowError
from .storage import SQLiteRepository


def parser():
    result = argparse.ArgumentParser(prog="orderflow", description="OrderFlow local reference application")
    commands = result.add_subparsers(dest="command", required=True)
    for command in ("serve", "seed", "demo", "drill", "process", "backup", "restore", "validate"):
        sub = commands.add_parser(command)
        sub.add_argument("--database", default="data/orderflow.db", help="Local SQLite workspace path")
        if command == "serve":
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, default=8000)
            sub.add_argument("--empty", action="store_true", help="Keep a new workspace free of sample orders")
        elif command == "process":
            sub.add_argument("--limit", type=int, default=20)
        elif command == "backup":
            sub.add_argument("destination", help="A new SQLite backup filename")
        elif command == "restore":
            sub.add_argument("source", help="Previously verified backup filename")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "restore" and not Path(arguments.database).is_file():
            raise ValueError("Restore requires an existing workspace; initialize it first.")
        repository = SQLiteRepository(arguments.database)
        engine = Engine(repository)
        if arguments.command == "serve":
            if not arguments.empty:
                engine.seed_demo()
            from .server import serve
            serve(engine, arguments.host, arguments.port)
            return 0
        if arguments.command in ("seed", "demo"):
            payload = engine.seed_demo() if arguments.command == "demo" else {"initialized": True, "products": len(engine.catalog()["products"])}
        elif arguments.command == "drill":
            payload = engine.run_drill("local-cli")
        elif arguments.command == "process":
            payload = engine.process(arguments.limit, "local-cli")
        elif arguments.command == "backup":
            payload = {"backup": str(repository.backup(arguments.destination)), "integrity": "ok"}
        elif arguments.command == "restore":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            safety = repository.path.parent / f"{repository.path.stem}-before-restore-{stamp}.db"
            repository.backup(safety)
            repository.restore(arguments.source)
            payload = {"restored": True, "safety_backup": str(safety), "integrity": "ok"}
        else:
            validate_state(repository.load())
            payload = {"valid": True, "orders": len(repository.load()["orders"])}
        print(json.dumps(payload, allow_nan=False, indent=2))
        return 0 if not (arguments.command == "drill" and not payload["passed"]) else 1
    except (ValueError, sqlite3.Error, OrderFlowError, OSError) as error:
        print(json.dumps({"error": str(error), "code": getattr(error, "code", "local_error")}))
        return 1
