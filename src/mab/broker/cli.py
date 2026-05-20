from __future__ import annotations

import argparse
import asyncio
import sys

from mab.broker.config import settings
from mab.broker.db import Database
from mab.broker.keygen import NameConflict, gen_key


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from mab.broker.app import app

    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None) or settings.port
    uvicorn.run(app, host=host, port=port)


async def _gen_key(args: argparse.Namespace) -> None:
    db = Database(settings.db_path)
    await db.connect()
    try:
        caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
        try:
            key, agent = await gen_key(
                db,
                name=args.name,
                machine_id=args.machine_id or "",
                capabilities=caps,
                auto_suffix=args.auto_suffix,
            )
        except NameConflict as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)

        print(f"Registered agent: {agent.name} (id={agent.id})")
        print("API key (save now — it cannot be recovered):")
        print(f"  {key}")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mab-broker")
    sub = parser.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="Start the broker server")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)

    gen_p = sub.add_parser("gen-key", help="Generate API key + register agent")
    gen_p.add_argument("--name", required=True)
    gen_p.add_argument("--machine-id", default=None)
    gen_p.add_argument("--capabilities", default="", help="comma-separated tags")
    gen_p.add_argument("--auto-suffix", action="store_true")

    args = parser.parse_args()
    if args.cmd is None or args.cmd == "serve":
        _serve(args)
    elif args.cmd == "gen-key":
        asyncio.run(_gen_key(args))
