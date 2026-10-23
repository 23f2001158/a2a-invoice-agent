
import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).with_name("tasks.db")


def get_connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                context_id TEXT NOT NULL,
                state TEXT NOT NULL,
                task_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(principal, task_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_principal
            ON tasks(principal)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                task_id TEXT NOT NULL,
                PRIMARY KEY(principal, message_id)
            )
        """)


def save_task(task_id, principal, context_id, state, task):
    task_json = json.dumps(task, separators=(",", ":"), sort_keys=True)

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO tasks
                (task_id, principal, context_id, state, task_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                state = excluded.state,
                task_json = excluded.task_json
            WHERE tasks.principal = excluded.principal
        """, (task_id, principal, context_id, state, task_json))


def get_task(task_id, principal):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT task_json FROM tasks
            WHERE task_id = ? AND principal = ?
        """, (task_id, principal)).fetchone()

    if row is None:
        return None

    return json.loads(row["task_json"])


def list_tasks(principal):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT task_json FROM tasks
            WHERE principal = ?
            ORDER BY created_at DESC
        """, (principal,)).fetchall()

    return [json.loads(row["task_json"]) for row in rows]


init_db()



def get_message_record(principal, message_id):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT message_hash, task_id FROM messages
            WHERE principal = ? AND message_id = ?
        """, (principal, message_id)).fetchone()

        if row is None:
            return None

        task = get_task(row["task_id"], principal)
        if task is None:
            return None

        return {
            "message_hash": row["message_hash"],
            "task": task
        }


def save_message_record(principal, message_id, message_hash, task_id, task):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO messages
                (principal, message_id, message_hash, task_id)
            VALUES (?, ?, ?, ?)
        """, (principal, message_id, message_hash, task_id))
