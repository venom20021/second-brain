"""SQLite database layer with FTS5 for full-text search.
All data stored locally — no cloud dependency.
"""
import sqlite3
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = os.environ.get("BRAIN_DB_PATH", "brain.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create all tables and FTS5 indexes."""
    with get_db() as conn:
        # Main content table — stores everything in one place
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL CHECK(item_type IN ('note', 'code', 'bookmark', 'task')),
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # FTS5 virtual table for full-text search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
                title,
                content,
                tags,
                content=items,
                content_rowid=id,
                tokenize='porter unicode61'
            )
        """)

        # Triggers to keep FTS in sync
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
                INSERT INTO items_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
            END
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
                INSERT INTO items_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
            END
        """)

        # Embeddings table — stores vector data as JSON (lightweight, no extra deps)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                item_id INTEGER PRIMARY KEY,
                vector TEXT NOT NULL,
                model_name TEXT NOT NULL DEFAULT 'all-MiniLM-L6-v2',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
            )
        """)

        # API keys table for authentication
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT 'unnamed',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_used_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0
            )
        """)

        print(f"✅ Database initialized at {DB_PATH}")


def insert_item(item_type: str, title: str, content: str, tags: list, metadata: dict) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO items (item_type, title, content, tags, metadata) VALUES (?, ?, ?, ?, ?)",
            (item_type, title, content, json.dumps(tags), json.dumps(metadata))
        )
        return cur.lastrowid


def update_item(item_id: int, **kwargs) -> bool:
    allowed = {"title", "content", "tags", "metadata", "item_type"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False

    # Serialize complex fields
    if "tags" in updates and isinstance(updates["tags"], list):
        updates["tags"] = json.dumps(updates["tags"])
    if "metadata" in updates and isinstance(updates["metadata"], dict):
        updates["metadata"] = json.dumps(updates["metadata"])

    updates["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [item_id]

    with get_db() as conn:
        cur = conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", values)
        return cur.rowcount > 0


def delete_item(item_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return cur.rowcount > 0


def get_item(item_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row:
            return dict(row)
        return None


def list_items(item_type: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        if item_type:
            rows = conn.execute(
                "SELECT * FROM items WHERE item_type = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (item_type, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM items ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]


def search_text(query: str, limit: int = 20) -> list[dict]:
    """Full-text search using FTS5 with ranking."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT items.*, rank
            FROM items_fts
            JOIN items ON items.id = items_fts.rowid
            WHERE items_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_embeddings() -> list[dict]:
    """Get all item IDs with their embedding vectors."""
    with get_db() as conn:
        rows = conn.execute("SELECT item_id, vector FROM embeddings").fetchall()
        return [{"item_id": r["item_id"], "vector": json.loads(r["vector"])} for r in rows]


def store_embedding(item_id: int, vector: list[float], model_name: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (item_id, vector, model_name) VALUES (?, ?, ?)",
            (item_id, json.dumps(vector), model_name)
        )


def generate_api_key(name: str = "unnamed") -> str:
    """Generate a new API key and store it."""
    key = "sb_" + secrets.token_hex(24)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key, name) VALUES (?, ?)",
            (key, name)
        )
    return key


def validate_api_key(key: str) -> dict | None:
    """Validate an API key. Returns key info if valid, None if invalid/revoked."""
    if not key:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key = ? AND revoked = 0",
            (key,)
        ).fetchone()
        if row:
            # Update last_used_at
            conn.execute(
                "UPDATE api_keys SET last_used_at = datetime('now') WHERE key = ?",
                (key,)
            )
            return dict(row)
    return None


def list_api_keys() -> list[dict]:
    """List all API keys (masked)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, last_used_at, revoked FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Show only first/last 4 chars of key
            key_row = conn.execute(
                "SELECT key FROM api_keys WHERE id = ?", (d["id"],)
            ).fetchone()
            if key_row:
                full_key = key_row["key"]
                d["key_preview"] = full_key[:7] + "..." + full_key[-4:]
            result.append(d)
        return result


def revoke_api_key(key_id: int) -> bool:
    """Revoke an API key by ID."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked = 1 WHERE id = ?",
            (key_id,)
        )
        return cur.rowcount > 0


def has_api_keys() -> bool:
    """Check if any active API keys exist."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM api_keys WHERE revoked = 0"
        ).fetchone()
        return row["c"] > 0


def get_stats() -> dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM items").fetchone()["c"]
        by_type = conn.execute(
            "SELECT item_type, COUNT(*) as c FROM items GROUP BY item_type"
        ).fetchall()
        embedded = conn.execute("SELECT COUNT(*) as c FROM embeddings").fetchone()["c"]
        return {
            "total_items": total,
            "by_type": {r["item_type"]: r["c"] for r in by_type},
            "embedded_items": embedded,
        }
