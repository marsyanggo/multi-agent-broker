from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from mab.shared.capabilities import matches_capabilities
from mab.shared.models import (
    Agent,
    AgentStatus,
    ContentType,
    Message,
    Task,
    TaskPriority,
    TaskStatus,
    short_uuid,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    machine_id TEXT NOT NULL,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'online',
    current_task TEXT,
    registered_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    api_key_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    reply_to TEXT,
    created_at TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent, delivered);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    assigned_to TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'normal',
    result TEXT,
    notes TEXT NOT NULL DEFAULT '[]',
    required_all TEXT NOT NULL DEFAULT '[]',
    required_any TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to, status);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _row_to_agent(row: aiosqlite.Row) -> Agent:
    return Agent(
        id=row["id"],
        name=row["name"],
        machine_id=row["machine_id"],
        capabilities=json.loads(row["capabilities"]),
        status=row["status"],
        current_task=row["current_task"],
        registered_at=_parse_dt(row["registered_at"]),
        last_heartbeat=_parse_dt(row["last_heartbeat"]),
    )


def _row_to_message(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        from_agent=row["from_agent"],
        to_agent=row["to_agent"],
        content=row["content"],
        content_type=row["content_type"],
        reply_to=row["reply_to"],
        created_at=_parse_dt(row["created_at"]),
        delivered=bool(row["delivered"]),
        delivered_at=_parse_dt(row["delivered_at"]),
    )


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        created_by=row["created_by"],
        assigned_to=row["assigned_to"],
        status=row["status"],
        priority=row["priority"],
        result=row["result"],
        notes=json.loads(row["notes"]),
        required_all=json.loads(row["required_all"]),
        required_any=json.loads(row["required_any"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        completed_at=_parse_dt(row["completed_at"]),
    )


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self.init_schema()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def init_schema(self) -> None:
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        async with self.conn.execute("PRAGMA table_info(tasks)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "required_all" not in cols:
            await self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN required_all TEXT NOT NULL DEFAULT '[]'"
            )
        if "required_any" not in cols:
            await self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN required_any TEXT NOT NULL DEFAULT '[]'"
            )

    # --- Agents ---

    async def register_agent(
        self,
        *,
        name: str,
        machine_id: str,
        capabilities: list[str],
        api_key_hash: str,
        status: AgentStatus = "offline",
    ) -> Agent:
        agent_id = short_uuid()
        now = utc_now()
        await self.conn.execute(
            """
            INSERT INTO agents
                (id, name, machine_id, capabilities, status, current_task,
                 registered_at, last_heartbeat, api_key_hash)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                agent_id,
                name,
                machine_id,
                json.dumps(capabilities),
                status,
                _iso(now),
                _iso(now),
                api_key_hash,
            ),
        )
        await self.conn.commit()
        return Agent(
            id=agent_id,
            name=name,
            machine_id=machine_id,
            capabilities=capabilities,
            status=status,
            current_task=None,
            registered_at=now,
            last_heartbeat=now,
        )

    async def get_agent(self, agent_id: str) -> Agent | None:
        async with self.conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_agent(row) if row else None

    async def get_agent_by_name(self, name: str) -> Agent | None:
        async with self.conn.execute(
            "SELECT * FROM agents WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_agent(row) if row else None

    async def get_agent_by_api_key_hash(self, api_key_hash: str) -> Agent | None:
        async with self.conn.execute(
            "SELECT * FROM agents WHERE api_key_hash = ?", (api_key_hash,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_agent(row) if row else None

    async def list_agents(
        self, *, status: AgentStatus | None = None
    ) -> list[Agent]:
        if status:
            cursor = self.conn.execute(
                "SELECT * FROM agents WHERE status = ? ORDER BY name", (status,)
            )
        else:
            cursor = self.conn.execute("SELECT * FROM agents ORDER BY name")
        async with cursor as cur:
            rows = await cur.fetchall()
        return [_row_to_agent(r) for r in rows]

    async def update_heartbeat(self, agent_id: str) -> None:
        await self.conn.execute(
            "UPDATE agents SET last_heartbeat = ? WHERE id = ?",
            (_iso(utc_now()), agent_id),
        )
        await self.conn.commit()

    async def set_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        await self.conn.execute(
            "UPDATE agents SET status = ? WHERE id = ?", (status, agent_id)
        )
        await self.conn.commit()

    async def set_current_task(
        self, agent_id: str, task_id: str | None
    ) -> None:
        await self.conn.execute(
            "UPDATE agents SET current_task = ? WHERE id = ?",
            (task_id, agent_id),
        )
        await self.conn.commit()

    async def delete_agent(self, agent_id: str) -> None:
        await self.conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await self.conn.commit()

    async def update_agent_capabilities(
        self, agent_id: str, capabilities: list[str]
    ) -> Agent | None:
        await self.conn.execute(
            "UPDATE agents SET capabilities = ? WHERE id = ?",
            (json.dumps(capabilities), agent_id),
        )
        await self.conn.commit()
        return await self.get_agent(agent_id)

    async def find_matching_agents(
        self,
        *,
        required_all: list[str],
        required_any: list[str],
        status: AgentStatus | None = None,
    ) -> list[Agent]:
        agents = await self.list_agents(status=status)
        return [
            a
            for a in agents
            if matches_capabilities(a.capabilities, required_all, required_any)
        ]

    # --- Messages ---

    async def create_message(
        self,
        *,
        from_agent: str,
        to_agent: str,
        content: str,
        content_type: ContentType = "text/plain",
        reply_to: str | None = None,
    ) -> Message:
        msg_id = short_uuid()
        now = utc_now()
        await self.conn.execute(
            """
            INSERT INTO messages
                (id, from_agent, to_agent, content, content_type, reply_to,
                 created_at, delivered)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (msg_id, from_agent, to_agent, content, content_type, reply_to, _iso(now)),
        )
        await self.conn.commit()
        return Message(
            id=msg_id,
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            content_type=content_type,
            reply_to=reply_to,
            created_at=now,
            delivered=False,
        )

    async def get_messages_for(
        self,
        agent_id: str,
        *,
        only_undelivered: bool = False,
        from_agent: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Message]:
        clauses = ["to_agent = ?"]
        vals: list[Any] = [agent_id]
        if only_undelivered:
            clauses.append("delivered = 0")
        if from_agent:
            clauses.append("from_agent = ?")
            vals.append(from_agent)
        if since:
            clauses.append("created_at > ?")
            vals.append(_iso(since))
        vals.append(limit)
        sql = (
            f"SELECT * FROM messages WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at ASC LIMIT ?"
        )
        async with self.conn.execute(sql, vals) as cur:
            rows = await cur.fetchall()
        return [_row_to_message(r) for r in rows]

    async def mark_delivered(self, message_id: str) -> None:
        await self.conn.execute(
            "UPDATE messages SET delivered = 1, delivered_at = ? WHERE id = ?",
            (_iso(utc_now()), message_id),
        )
        await self.conn.commit()

    async def cleanup_old_messages(self, ttl_days: int = 7) -> int:
        cutoff = utc_now() - timedelta(days=ttl_days)
        cur = await self.conn.execute(
            "DELETE FROM messages WHERE created_at < ?", (_iso(cutoff),)
        )
        deleted = cur.rowcount
        await cur.close()
        await self.conn.commit()
        return deleted

    # --- Tasks ---

    async def create_task(
        self,
        *,
        title: str,
        description: str = "",
        created_by: str,
        assigned_to: str | None = None,
        priority: TaskPriority = "normal",
        required_all: list[str] | None = None,
        required_any: list[str] | None = None,
    ) -> Task:
        task_id = short_uuid()
        now = utc_now()
        status: TaskStatus = "assigned" if assigned_to else "pending"
        required_all = required_all or []
        required_any = required_any or []
        await self.conn.execute(
            """
            INSERT INTO tasks
                (id, title, description, created_by, assigned_to, status,
                 priority, notes, required_all, required_any,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)
            """,
            (
                task_id,
                title,
                description,
                created_by,
                assigned_to,
                status,
                priority,
                json.dumps(required_all),
                json.dumps(required_any),
                _iso(now),
                _iso(now),
            ),
        )
        await self.conn.commit()
        return Task(
            id=task_id,
            title=title,
            description=description,
            created_by=created_by,
            assigned_to=assigned_to,
            status=status,
            priority=priority,
            required_all=required_all,
            required_any=required_any,
            created_at=now,
            updated_at=now,
        )

    async def claim_task(self, *, task_id: str, agent_id: str) -> Task | None:
        # Returns the claimed task, or None if it was already claimed / missing.
        now = utc_now()
        async with self.conn.execute(
            """
            UPDATE tasks
            SET assigned_to = ?, status = 'assigned', updated_at = ?
            WHERE id = ? AND status = 'pending'
            RETURNING *
            """,
            (agent_id, _iso(now), task_id),
        ) as cur:
            row = await cur.fetchone()
        await self.conn.commit()
        return _row_to_task(row) if row else None

    async def get_task(self, task_id: str) -> Task | None:
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        result: str | None = None,
        note: str | None = None,
    ) -> Task | None:
        task = await self.get_task(task_id)
        if task is None:
            return None

        new_status = status or task.status
        new_result = result if result is not None else task.result
        new_notes = task.notes + ([note] if note else [])
        now = utc_now()
        completed_at = task.completed_at
        if new_status in ("completed", "failed") and completed_at is None:
            completed_at = now

        await self.conn.execute(
            """
            UPDATE tasks
            SET status = ?, result = ?, notes = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                new_result,
                json.dumps(new_notes),
                _iso(now),
                _iso(completed_at) if completed_at else None,
                task_id,
            ),
        )
        await self.conn.commit()
        return await self.get_task(task_id)

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        assigned_to: str | None = None,
        created_by: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        clauses: list[str] = []
        vals: list[Any] = []
        if status:
            clauses.append("status = ?")
            vals.append(status)
        if assigned_to:
            clauses.append("assigned_to = ?")
            vals.append(assigned_to)
        if created_by:
            clauses.append("created_by = ?")
            vals.append(created_by)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        vals.append(limit)
        sql = f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?"
        async with self.conn.execute(sql, vals) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]
