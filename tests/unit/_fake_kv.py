from typing import Any


class FakeKv:
    """In-memory kv repo (was in test_rate_lane.py before lane removal)."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.rows.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.rows[key] = value

    async def delete(self, key: str) -> bool:
        return self.rows.pop(key, None) is not None

    async def delete_prefix(self, prefix: str) -> int:
        removed = 0
        for k in [k for k in self.rows if k.startswith(prefix)]:
            del self.rows[k]
            removed += 1
        return removed
