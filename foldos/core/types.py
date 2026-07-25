from __future__ import annotations

import os
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Scope:
    tenant: str
    agent: str
    session: str
    thread: str = "main"

    def key(self) -> str:
        return f"{self.tenant}/{self.agent}/{self.session}/{self.thread}"


@dataclass
class Event:
    kind: str
    payload: dict[str, Any]
    actor: str = "agent"
    causation_id: str | None = None
    id: str = ""
    ts: str = ""
    seq: int = 0
    hash: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{int(time.time() * 1000):013d}-{os.urandom(8).hex()}"
        if not self.ts:
            self.ts = datetime.now(UTC).isoformat()

    def clone(self, **changes: Any) -> Event:
        return replace(self, payload=deepcopy(self.payload), **deepcopy(changes))

    def stamp(self, *, seq: int, hash: str) -> Event:
        return self.clone(seq=seq, hash=hash)


class ConcurrencyError(Exception):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"expected {expected}, got {actual}")


class PolicyViolation(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ChainBroken(Exception):
    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"chain broken at seq {seq}")
