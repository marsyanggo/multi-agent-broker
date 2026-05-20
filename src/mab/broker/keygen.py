from __future__ import annotations

from mab.broker.auth import generate_api_key, hash_api_key
from mab.broker.db import Database
from mab.shared.models import Agent


class NameConflict(Exception):
    pass


async def gen_key(
    db: Database,
    *,
    name: str,
    machine_id: str = "",
    capabilities: list[str] | None = None,
    auto_suffix: bool = False,
) -> tuple[str, Agent]:
    final_name = name
    if await db.get_agent_by_name(final_name) is not None:
        if not auto_suffix:
            raise NameConflict(
                f"agent name '{name}' already exists "
                "(use --auto-suffix to auto-rename)"
            )
        i = 2
        while await db.get_agent_by_name(f"{name}-{i}") is not None:
            i += 1
        final_name = f"{name}-{i}"

    raw_key = generate_api_key()
    agent = await db.register_agent(
        name=final_name,
        machine_id=machine_id,
        capabilities=capabilities or [],
        api_key_hash=hash_api_key(raw_key),
        status="offline",
    )
    return raw_key, agent
