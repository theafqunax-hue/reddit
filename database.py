import sqlite3
import os

from config import DATA_DIR, DATABASE_PATH

os.makedirs(DATA_DIR, exist_ok=True)


def connect():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = connect()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            subreddit TEXT PRIMARY KEY,
            post_limit INTEGER NOT NULL DEFAULT 100,
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            subreddit TEXT NOT NULL,
            title TEXT,
            permalink TEXT,
            created_utc REAL,
            sent INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Migration for old databases
    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(posts)"
        ).fetchall()
    }

    if "sent" not in columns:
        conn.execute(
            "ALTER TABLE posts ADD COLUMN sent INTEGER NOT NULL DEFAULT 0"
        )

    if "last_error" not in columns:
        conn.execute(
            "ALTER TABLE posts ADD COLUMN last_error TEXT"
        )

    conn.commit()
    conn.close()


def set_setting(key, value):
    conn = connect()

    conn.execute("""
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (key, str(value)))

    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = connect()

    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    ).fetchone()

    conn.close()

    if row is None:
        return default

    return row["value"]


def add_community(subreddit, post_limit):
    conn = connect()

    conn.execute("""
        INSERT INTO communities (
            subreddit,
            post_limit,
            enabled
        )
        VALUES (?, ?, 1)

        ON CONFLICT(subreddit)
        DO UPDATE SET
            post_limit = excluded.post_limit,
            enabled = 1
    """, (subreddit, post_limit))

    conn.commit()
    conn.close()


def remove_community(subreddit):
    conn = connect()

    cursor = conn.execute(
        "DELETE FROM communities WHERE subreddit = ?",
        (subreddit,)
    )

    conn.commit()

    removed = cursor.rowcount > 0

    conn.close()

    return removed


def get_communities():
    conn = connect()

    rows = conn.execute("""
        SELECT
            subreddit,
            post_limit,
            enabled
        FROM communities
        WHERE enabled = 1
        ORDER BY subreddit
    """).fetchall()

    conn.close()

    return [dict(row) for row in rows]


def post_exists(post_id):
    conn = connect()

    row = conn.execute(
        "SELECT 1 FROM posts WHERE post_id = ?",
        (post_id,)
    ).fetchone()

    conn.close()

    return row is not None


def save_post(post, sent=False, error=None):
    conn = connect()

    conn.execute("""
        INSERT INTO posts (
            post_id,
            subreddit,
            title,
            permalink,
            created_utc,
            sent,
            last_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(post_id)
        DO UPDATE SET
            sent = excluded.sent,
            last_error = excluded.last_error
    """, (
        post["id"],
        post["subreddit"],
        post["title"],
        post["permalink"],
        post["created_utc"],
        1 if sent else 0,
        error
    ))

    conn.commit()
    conn.close()


def mark_sent(post_id):
    conn = connect()

    conn.execute("""
        UPDATE posts
        SET sent = 1,
            last_error = NULL,
            sent_at = CURRENT_TIMESTAMP
        WHERE post_id = ?
    """, (post_id,))

    conn.commit()
    conn.close()


def mark_failed(post_id, error):
    conn = connect()

    conn.execute("""
        UPDATE posts
        SET sent = 0,
            last_error = ?
        WHERE post_id = ?
    """, (str(error)[:2000], post_id))

    conn.commit()
    conn.close()