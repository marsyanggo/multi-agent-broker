"""Connect to a broker and print received messages / task events as JSON.

Use to verify capability-filtered broadcast: spin this up as a non-MCP agent
on one box, send tasks from another, and watch which events arrive (or don't).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mab.mcp_server.broker_client import BrokerClient


async def _run(broker_url: str, api_key: str, poll_interval: float) -> None:
    client = BrokerClient(broker_url=broker_url, api_key=api_key)
    await client.start()
    assert client.agent is not None
    print(
        f"[watch] connected as {client.agent.name} (id={client.agent.id}) "
        f"caps={client.agent.capabilities}",
        file=sys.stderr,
        flush=True,
    )
    try:
        while True:
            await asyncio.sleep(poll_interval)
            for m in client.drain_messages():
                print(
                    json.dumps({"kind": "message", "data": m.model_dump(mode="json")}),
                    flush=True,
                )
            for t in client.drain_task_events():
                print(
                    json.dumps({"kind": "task_event", "data": t.model_dump(mode="json")}),
                    flush=True,
                )
    finally:
        await client.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mab-watch")
    parser.add_argument(
        "--broker-url",
        default=os.environ.get("MAB_BROKER_URL", "http://localhost:8420"),
    )
    parser.add_argument("--api-key", default=os.environ.get("MAB_API_KEY"))
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args()

    if not args.api_key:
        print("error: --api-key or MAB_API_KEY env required", file=sys.stderr)
        sys.exit(2)

    try:
        asyncio.run(_run(args.broker_url, args.api_key, args.poll_interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
