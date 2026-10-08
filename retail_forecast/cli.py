"""Small explicit commands for serving, importing, and reproducible experiments."""

import argparse
import json
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="shelfcast")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Run the local application")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8000)
    server.add_argument("--data-dir", default="data")
    importer = commands.add_parser("import-m5", help="Import locally downloaded M5 CSV files")
    importer.add_argument("folder", type=Path)
    importer.add_argument("--stores", nargs="+", help="e.g. CA_1 TX_1 WI_1; default all stores")
    importer.add_argument("--max-items", type=int, default=12, help="Maximum items per store")
    importer.add_argument("--output", default="data/dataset.json")
    forecast = commands.add_parser("forecast", help="Export a reproducible forecast and backtests")
    forecast.add_argument("--store", default="CA_1")
    forecast.add_argument("--item", default="FOODS_1_001")
    forecast.add_argument("--model", choices=["seasonal", "xgboost"], default="seasonal")
    forecast.add_argument("--cutoff", type=int)
    forecast.add_argument("--data-dir", default="data")
    forecast.add_argument("--output", default="data/forecast.json")
    demo = commands.add_parser("release-demo", help="Record explicit rejected / approved workflow examples")
    demo.add_argument("--data-dir", default="data")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import serve
            serve(args.host, args.port, args.data_dir)
        elif args.command == "import-m5":
            from .data import import_m5
            dataset = import_m5(args.folder, stores=args.stores, max_items=args.max_items)
            write_json(args.output, dataset)
            print(f"Imported {len(dataset['series'])} M5 series, {dataset['total_days']} days -> {args.output}")
        elif args.command == "forecast":
            from .storage import LocalRepository
            repo = LocalRepository(args.data_dir)
            cutoff = args.cutoff if args.cutoff is not None else repo.catalog()["default_cutoff"]
            result = repo.forecast(args.store, args.item, cutoff, args.model)
            write_json(args.output, {k: v for k, v in result.items() if not k.startswith("_")})
            print(f"Saved {args.model} forecast and chronological backtests -> {args.output}")
        else:
            from .storage import LocalRepository
            result = LocalRepository(args.data_dir).demo_releases()
            for release in result["releases"][-2:]:
                print(f"{release['status']}: {release['model']} — {release['reason']}")
    except (ValueError, LookupError, FileNotFoundError) as exc:
        parser.error(str(exc))
