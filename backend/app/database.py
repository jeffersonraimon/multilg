import os
import sqlite3
import threading
from datetime import datetime, timezone
from uuid import uuid4

from .crypto import ConfigCipher


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self) -> None:
        self.path = os.getenv("LG_DATABASE_PATH", "/data/multilg.db")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.cipher = ConfigCipher()
        self.lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS looking_glasses (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    protocol TEXT NOT NULL CHECK(protocol IN ('http', 'telnet')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _public_config(protocol: str, config: dict) -> dict:
        safe = dict(config)
        if protocol == "telnet" and safe.get("password"):
            safe["password"] = ""
            safe["has_password"] = True
        return safe

    def _decode(self, row: sqlite3.Row, reveal: bool = False) -> dict:
        config = self.cipher.decrypt(row["config"])
        return {
            "id": row["id"],
            "name": row["name"],
            "protocol": row["protocol"],
            "enabled": bool(row["enabled"]),
            "config": config if reveal else self._public_config(row["protocol"], config),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list(self, reveal: bool = False) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM looking_glasses ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._decode(row, reveal) for row in rows]

    def get(self, item_id: str, reveal: bool = False) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM looking_glasses WHERE id = ?", (item_id,)
            ).fetchone()
        return self._decode(row, reveal) if row else None

    def get_by_name(self, name: str, reveal: bool = False) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM looking_glasses WHERE LOWER(name) = LOWER(?)", (name.strip(),)
            ).fetchone()
        return self._decode(row, reveal) if row else None

    def create(self, payload: dict) -> dict:
        item_id = str(uuid4())
        now = utc_now()
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO looking_glasses VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    payload["name"],
                    payload["protocol"],
                    int(payload["enabled"]),
                    self.cipher.encrypt(payload["config"]),
                    now,
                    now,
                ),
            )
        return self.get(item_id)  # type: ignore[return-value]

    def update(self, item_id: str, payload: dict) -> dict | None:
        current = self.get(item_id, reveal=True)
        if not current:
            return None
        config = payload["config"]
        if (
            payload["protocol"] == "telnet"
            and not config.get("password")
            and current["protocol"] == "telnet"
        ):
            config["password"] = current["config"].get("password", "")
        now = utc_now()
        with self.lock, self._connect() as conn:
            conn.execute(
                """UPDATE looking_glasses
                   SET name = ?, protocol = ?, enabled = ?, config = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    payload["name"],
                    payload["protocol"],
                    int(payload["enabled"]),
                    self.cipher.encrypt(config),
                    now,
                    item_id,
                ),
            )
        return self.get(item_id)

    def duplicate(self, item_id: str) -> dict | None:
        current = self.get(item_id, reveal=True)
        if not current:
            return None
        suffix = " (cópia)"
        name = current["name"][: 80 - len(suffix)] + suffix
        return self.create(
            {
                "name": name,
                "protocol": current["protocol"],
                "enabled": False,
                "config": current["config"],
            }
        )

    def delete(self, item_id: str) -> bool:
        with self.lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM looking_glasses WHERE id = ?", (item_id,))
        return cursor.rowcount > 0
