"""聊天历史存储（SQLite）。

为什么不存进 mda 数据库？
  mda 是只读的（角色 mda_viewer 属于 mda_readonly 组），写不进去，
  而且调查数据库不该混进应用自己的数据。
  所以另开一个 SQLite 文件：~/.mda_db_mcp/history.db

存什么：
  conversations  一次会话（标题、语言、时间、累计 token）
  messages       每条消息。助手消息除了正文，还存下：
                   steps  —— 调用了哪些工具、SQL 是什么（给客户演示时要能回放）
                   thinking —— 模型的思考过程
                   usage  —— token 用量，用来核对 effort 设置的实际开销
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config_store import CONFIG_DIR

DB_FILE = CONFIG_DIR / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    language    TEXT NOT NULL DEFAULT 'zh',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,          -- 'user' | 'assistant'
    content         TEXT NOT NULL DEFAULT '',
    thinking        TEXT NOT NULL DEFAULT '',
    steps           TEXT NOT NULL DEFAULT '[]',   -- JSON: 工具调用过程
    usage           TEXT NOT NULL DEFAULT '{}',   -- JSON: token 用量
    error           TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conv
    ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(updated_at DESC);
"""


def _lock_down() -> None:
    """把库文件权限收紧到 600。

    主库文件容易记得，但 WAL 模式还会生成 -wal 和 -shm 两个旁支文件，
    它们由 SQLite 按 umask 创建（通常是 644），里面装着同样的聊天内容。
    只 chmod 主库等于没锁门，所以三个都要处理。
    """
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(DB_FILE) + suffix)
        try:
            if f.exists():
                os.chmod(f, 0o600)
        except OSError:
            pass   # 权限设不上不该让整个功能挂掉


def _connect() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL 模式：网页在轮询状态的同时写历史不会互相锁住
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _lock_down()
    return conn


def create_conversation(title: str = "", language: str = "zh") -> str:
    conv_id = uuid.uuid4().hex[:16]
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, language, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (conv_id, title[:200], language, now, now),
        )
    return conv_id


def ensure_conversation(conv_id: str | None, title: str, language: str) -> str:
    """有 id 就沿用，没有就新建。标题用第一句提问自动生成。"""
    if conv_id:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
        if row:
            return conv_id
    return create_conversation(title=title, language=language)


def add_message(
    conversation_id: str,
    role: str,
    content: str,
    thinking: str = "",
    steps: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, thinking,"
            " steps, usage, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id, role, content, thinking,
                json.dumps(steps or [], ensure_ascii=False),
                json.dumps(usage or {}, ensure_ascii=False),
                error, now,
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )


def list_conversations(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.language, c.created_at, c.updated_at,
                   (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id)
                       AS message_count
            FROM conversations c
            -- 空会话（点了新建但没提问）不展示，否则列表会堆一堆垃圾
            WHERE message_count > 0
            ORDER BY c.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        conv = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if conv is None:
            return None
        msgs = conn.execute(
            "SELECT role, content, thinking, steps, usage, error, created_at"
            " FROM messages WHERE conversation_id = ? ORDER BY id",
            (conv_id,),
        ).fetchall()

    return {
        **dict(conv),
        "messages": [
            {
                **dict(m),
                "steps": json.loads(m["steps"] or "[]"),
                "usage": json.loads(m["usage"] or "{}"),
            }
            for m in msgs
        ],
    }


def llm_history(conv_id: str, max_turns: int = 8) -> list[dict[str, str]]:
    """取出最近几轮问答，作为上下文喂给 LLM。

    只取正文，不取工具调用过程和思考内容——那些加起来能有几万 token，
    重新塞回去既贵又没必要。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages"
            " WHERE conversation_id = ? AND content != '' AND error = ''"
            " ORDER BY id DESC LIMIT ?",
            (conv_id, max_turns * 2),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def rename_conversation(conv_id: str, title: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?", (title[:200], conv_id)
        )
    return cur.rowcount > 0


def delete_conversation(conv_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    return cur.rowcount > 0


def delete_all() -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM conversations")
    return cur.rowcount


def stats() -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT (SELECT count(*) FROM conversations) AS conversations,"
            " (SELECT count(*) FROM messages) AS messages"
        ).fetchone()
    return {
        **dict(row),
        "path": str(DB_FILE),
        "size_bytes": DB_FILE.stat().st_size if DB_FILE.exists() else 0,
    }
