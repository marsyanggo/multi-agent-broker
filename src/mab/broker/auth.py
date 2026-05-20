from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from mab.broker.db import Database
from mab.shared.models import Agent

API_KEY_PREFIX = "mab-ak-"


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(24)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_db(request: Request) -> Database:
    return request.app.state.db


async def get_current_agent(
    db: Annotated[Database, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Agent:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = authorization.split(" ", 1)[1].strip()
    agent = await db.get_agent_by_api_key_hash(hash_api_key(token))
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api key",
        )
    return agent
