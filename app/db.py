"""SQLite persistence layer.

A single connection guarded by a lock is used deliberately: the write volume of
this service (a handful of rows per deploy) is far below where SQLite contention
matters, and it keeps the concurrency story trivial to reason about.  WAL mode
lets the API read run history while a deploy is writing its log rows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import config

SCHEMA_VERSION = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT    NOT NULL,
    display_name        TEXT    NOT NULL DEFAULT '',
    is_admin            INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    last_login_at       TEXT,
    password_changed_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT    NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    last_seen_at TEXT,
    ip           TEXT    NOT NULL DEFAULT '',
    user_agent   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS credentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    kind            TEXT    NOT NULL DEFAULT 'https_token',
    username        TEXT    NOT NULL DEFAULT '',
    secret          TEXT    NOT NULL DEFAULT '',
    -- 仅 SSH 私钥使用：密钥口令（可空）
    passphrase      TEXT    NOT NULL DEFAULT '',
    description     TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    -- 最近一次仓库连通性测试结果，供界面显示凭据是否可用
    last_tested_at  TEXT,
    last_test_ok    INTEGER,
    last_test_error TEXT    NOT NULL DEFAULT '',
    -- SSH 私钥专有：最近一次「登录主机」测试结果。与仓库测试分开记，
    -- 否则一次主机登录失败会把可用的仓库凭据显示成不可用（反之亦然）。
    last_host_test_at    TEXT,
    last_host_test_ok    INTEGER,
    last_host_test_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    name                     TEXT    NOT NULL UNIQUE,
    description              TEXT    NOT NULL DEFAULT '',

    repo_url                 TEXT    NOT NULL,
    repo_branch              TEXT    NOT NULL DEFAULT 'main',
    repo_subdir              TEXT    NOT NULL DEFAULT '',
    git_depth                INTEGER NOT NULL DEFAULT 1,
    git_username             TEXT    NOT NULL DEFAULT '',
    git_token                TEXT    NOT NULL DEFAULT '',
    -- 引用的全局凭据；为空时回退用任务自带的 git_username/git_token
    credential_id            INTEGER REFERENCES credentials(id) ON DELETE SET NULL,

    schedule_type            TEXT    NOT NULL DEFAULT 'interval',
    schedule_expression      TEXT    NOT NULL DEFAULT '1h',
    enabled                  INTEGER NOT NULL DEFAULT 1,
    -- Webhook 触发开关：0 = 禁用，1 = 启用
    webhook_enabled          INTEGER NOT NULL DEFAULT 1,
    -- Webhook 触发令牌：拼在触发地址路径里，持有者即可触发一次部署；
    -- 为空表示待生成（v4 迁移或首次保存时补齐）。
    webhook_secret           TEXT    NOT NULL DEFAULT '',

    deploy_method            TEXT    NOT NULL DEFAULT 'script',
    prepare_script           TEXT    NOT NULL DEFAULT '',
    deploy_script            TEXT    NOT NULL DEFAULT '',
    rollback_script          TEXT    NOT NULL DEFAULT '',
    artifact_paths           TEXT    NOT NULL DEFAULT '',
    target_dir               TEXT    NOT NULL DEFAULT '',
    keep_releases            INTEGER NOT NULL DEFAULT 5,
    service_name             TEXT    NOT NULL DEFAULT '',
    docker_image             TEXT    NOT NULL DEFAULT '',
    docker_command           TEXT    NOT NULL DEFAULT '',
    docker_compose_file      TEXT    NOT NULL DEFAULT '',
    -- 「Compose 内容直填」方式的 compose 文件正文；该方式不依赖仓库。
    compose_content          TEXT    NOT NULL DEFAULT '',
    rsync_target             TEXT    NOT NULL DEFAULT '',
    rsync_options            TEXT    NOT NULL DEFAULT '-az --delete',
    -- 目标主机；为空表示在本机（控制机）执行，保持原有行为。
    host_id                  INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
    -- 部署源：git = 从仓库拉取；snapshot = 从任务快照包还原。
    source_mode              TEXT    NOT NULL DEFAULT 'git',
    -- 要纳入快照的数据源声明（JSON 数组）；为空则仅用自动探测结果。
    data_sources             TEXT    NOT NULL DEFAULT '[]',

    env_vars                 TEXT    NOT NULL DEFAULT '{}',
    timeout_seconds          INTEGER NOT NULL DEFAULT 1800,
    notify_webhook           TEXT    NOT NULL DEFAULT '',
    skip_if_no_changes       INTEGER NOT NULL DEFAULT 1,
    run_on_create            INTEGER NOT NULL DEFAULT 0,

    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL,
    next_run_at              TEXT,
    last_run_at              TEXT,
    last_run_id              INTEGER,
    last_status              TEXT    NOT NULL DEFAULT '',
    run_count                INTEGER NOT NULL DEFAULT 0,
    success_count            INTEGER NOT NULL DEFAULT 0,
    failure_count            INTEGER NOT NULL DEFAULT 0,
    total_duration_ms        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_next_run ON tasks(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    task_name           TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'queued',
    trigger             TEXT    NOT NULL DEFAULT 'manual',
    deploy_method       TEXT    NOT NULL DEFAULT '',
    queued_at           TEXT    NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    exit_code           INTEGER,
    commit_before       TEXT    NOT NULL DEFAULT '',
    commit_after        TEXT    NOT NULL DEFAULT '',
    commit_message      TEXT    NOT NULL DEFAULT '',
    commit_author       TEXT    NOT NULL DEFAULT '',
    commit_time         TEXT    NOT NULL DEFAULT '',
    branch              TEXT    NOT NULL DEFAULT '',
    changed_files       INTEGER NOT NULL DEFAULT 0,
    changes_detected    INTEGER NOT NULL DEFAULT 1,
    release_dir         TEXT    NOT NULL DEFAULT '',
    artifact_path       TEXT    NOT NULL DEFAULT '',
    log_path            TEXT    NOT NULL DEFAULT '',
    log_bytes           INTEGER NOT NULL DEFAULT 0,
    error               TEXT    NOT NULL DEFAULT '',
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    pid                 INTEGER,
    -- 本次运行的目标主机快照：主机被改名或删除后，历史记录仍可追溯。
    host_id             INTEGER,
    host_name           TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_queued ON runs(queued_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL DEFAULT '',
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '',
    ip      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(id DESC);

CREATE TABLE IF NOT EXISTS backups (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    filename           TEXT    NOT NULL UNIQUE,
    filepath           TEXT    NOT NULL,
    size_bytes         INTEGER NOT NULL DEFAULT 0,
    sha256             TEXT    NOT NULL DEFAULT '',
    trigger            TEXT    NOT NULL DEFAULT 'manual',
    contains_logs      INTEGER NOT NULL DEFAULT 0,
    contains_artifacts INTEGER NOT NULL DEFAULT 0,
    -- 是否包含容器挂载的持久化业务数据（persistent/）；旧备份为 0。
    contains_persistent INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT 'success',
    error              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backups_created ON backups(id DESC);

CREATE TABLE IF NOT EXISTS hosts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL UNIQUE,
    address        TEXT    NOT NULL,
    port           INTEGER NOT NULL DEFAULT 22,
    username       TEXT    NOT NULL,
    credential_id  INTEGER REFERENCES credentials(id) ON DELETE SET NULL,
    base_dir       TEXT    NOT NULL DEFAULT '/opt/autodeploy',
    -- 首次连接成功后钉住的 SSH 主机指纹；之后指纹变化一律拒绝连接。
    host_key       TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'unknown',
    last_check_at  TEXT,
    last_error     TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS host_metrics (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT    NOT NULL,
    cpu_percent        REAL    NOT NULL,
    memory_percent     REAL    NOT NULL,
    disk_read_rate     REAL    NOT NULL DEFAULT 0.0,
    disk_write_rate    REAL    NOT NULL DEFAULT 0.0,
    net_rx_rate        REAL    NOT NULL DEFAULT 0.0,
    net_tx_rate        REAL    NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_host_metrics_ts ON host_metrics(ts);

CREATE TABLE IF NOT EXISTS container_metrics (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT    NOT NULL,
    container_id       TEXT    NOT NULL,
    container_name     TEXT    NOT NULL,
    compose_project    TEXT    NOT NULL DEFAULT '',
    cpu_percent        REAL    NOT NULL DEFAULT 0.0,
    memory_bytes       REAL    NOT NULL DEFAULT 0.0,
    memory_percent     REAL    NOT NULL DEFAULT 0.0,
    disk_read_rate     REAL    NOT NULL DEFAULT 0.0,
    disk_write_rate    REAL    NOT NULL DEFAULT 0.0,
    net_rx_rate        REAL    NOT NULL DEFAULT 0.0,
    net_tx_rate        REAL    NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_container_metrics_ts ON container_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_container_metrics_cid_ts ON container_metrics(container_id, ts);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


class Database:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.DB_PATH
        self._lock = threading.RLock()
        self._conn = _connect(self.path)

    # -- helpers ----------------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialise a write transaction."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.close()
            return rows

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()))

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a statement, returning ``lastrowid``."""
        with self.write() as conn:
            cursor = conn.execute(sql, tuple(params))
            rowid = cursor.lastrowid
            cursor.close()
            return int(rowid or 0)

    def execute_many(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        with self.write() as conn:
            conn.executemany(sql, [tuple(p) for p in params])

    def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.write() as conn:
            cursor = conn.execute(sql, tuple(params))
            count = cursor.rowcount
            cursor.close()
            return int(count)

    def commit(self) -> None:
        with self.write():
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- schema -----------------------------------------------------------
    def init_schema(self) -> None:
        with self.write() as conn:
            conn.executescript(SCHEMA)
            current = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            else:
                self._migrate(conn, int(current["version"]))

    def _migrate(self, conn: sqlite3.Connection, current: int) -> None:
        """Apply additive migrations for databases created by older builds."""
        if current >= SCHEMA_VERSION:
            return
        if current < 1:
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
            )
        if current < 2:
            self._migrate_v1_to_v2(conn)
        if current < 3:
            self._migrate_v2_to_v3(conn)
        if current < 4:
            self._migrate_v3_to_v4(conn)
        if current < 5:
            self._migrate_v4_to_v5(conn)
        if current < 6:
            self._migrate_v5_to_v6(conn)
        if current < 7:
            self._migrate_v6_to_v7(conn)
        if current < 8:
            self._migrate_v7_to_v8(conn)
        if current < 9:
            self._migrate_v8_to_v9(conn)
        if current < 10:
            self._migrate_v9_to_v10(conn)
        if current < 11:
            self._migrate_v10_to_v11(conn)
        if current < 12:
            self._migrate_v11_to_v12(conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version, applied_at) VALUES (?, datetime('now'))",
            (SCHEMA_VERSION,),
        )

    def _migrate_v9_to_v10(self, conn: sqlite3.Connection) -> None:
        """v10: 任务可直填 compose 内容，部署不再依赖仓库。

        ``compose_content`` 存 compose 文件正文；只有「Compose 内容直填」
        方式使用它，其它方式的该列保持空串。既有任务全部是仓库拉取形态，
        默认空串即可，无需回填。
        """
        self._add_column(conn, "tasks", "compose_content", "TEXT NOT NULL DEFAULT ''")

    def _migrate_v10_to_v11(self, conn: sqlite3.Connection) -> None:
        """v11: 备份记录标记是否包含容器挂载的持久化业务数据。

        0.4.1 已发布为 v10，因此这一列必须单独占用一个新版本号：写进 v10
        的函数里，已装 0.4.1 的实例会因为版本相等而整段跳过，列永远不存在。

        旧备份（0.4.1 及更早）不含 persistent/，默认 0 —— 恢复流程据此
        判断不应清空现有数据。
        """
        self._add_column(
            conn, "backups", "contains_persistent", "INTEGER NOT NULL DEFAULT 0"
        )

    def _migrate_v11_to_v12(self, conn: sqlite3.Connection) -> None:
        """v12: 任务支持分别配置定时触发与 Webhook 触发是否启用。

        ``webhook_enabled`` 控制 Webhook 触发开关，默认 1（启用）；既有任务保持开启。
        """
        self._add_column(
            conn, "tasks", "webhook_enabled", "INTEGER NOT NULL DEFAULT 1"
        )

    def _migrate_v7_to_v8(self, conn: sqlite3.Connection) -> None:
        """v8: 新增远程主机表 hosts，并给任务/运行补主机维度。

        主机是「部署到哪里」的一等对象，通过 SSH 连接。凭据复用 credentials
        表的 ssh_key 类型，不新增秘密存储；host_key 存放首次连接后钉住的
        主机指纹，用于拒绝指纹变化（中间人）。

        同一迁移里补三件与主机/快照相关的东西（都是加列或建表，可重复执行）：
        * tasks.host_id / source_mode / data_sources
        * runs.host_id / host_name（历史运行据此保留目标主机名）
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hosts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT    NOT NULL UNIQUE,
                address        TEXT    NOT NULL,
                port           INTEGER NOT NULL DEFAULT 22,
                username       TEXT    NOT NULL,
                credential_id  INTEGER REFERENCES credentials(id) ON DELETE SET NULL,
                base_dir       TEXT    NOT NULL DEFAULT '/opt/autodeploy',
                host_key       TEXT    NOT NULL DEFAULT '',
                status         TEXT    NOT NULL DEFAULT 'unknown',
                last_check_at  TEXT,
                last_error     TEXT    NOT NULL DEFAULT '',
                created_at     TEXT    NOT NULL,
                updated_at     TEXT    NOT NULL
            )
            """
        )
        self._add_column(
            conn, "tasks", "host_id",
            "INTEGER REFERENCES hosts(id) ON DELETE SET NULL",
        )
        self._add_column(conn, "tasks", "source_mode", "TEXT NOT NULL DEFAULT 'git'")
        self._add_column(conn, "tasks", "data_sources", "TEXT NOT NULL DEFAULT '[]'")
        self._add_column(conn, "runs", "host_id", "INTEGER")
        self._add_column(conn, "runs", "host_name", "TEXT NOT NULL DEFAULT ''")

    def _migrate_v8_to_v9(self, conn: sqlite3.Connection) -> None:
        """v9: 凭据补「登录主机」测试结果，与仓库测试分开记录。

        SSH 私钥有两条互不相干的用途（仓库 Deploy Key / 主机登录），共用
        一列测试结论会让界面把「主机登录成功但仓库没配」显示成不可用。
        """
        self._add_column(conn, "credentials", "last_host_test_at", "TEXT")
        self._add_column(conn, "credentials", "last_host_test_ok", "INTEGER")
        self._add_column(
            conn, "credentials", "last_host_test_error", "TEXT NOT NULL DEFAULT ''"
        )

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        """幂等地补一列；SQLite 的 ADD COLUMN 不能带 IF NOT EXISTS。"""
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in columns:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """v2: 任务名唯一约束。

        SQLite 无法直接给已有列加 UNIQUE，只能整表重建。重建前先把重名任务
        改名为「原名-2」「原名-3」…，保证迁移在任何存量数据上都能成功。
        """
        duplicates = conn.execute(
            """
            SELECT name, COUNT(*) AS total FROM tasks
            GROUP BY name COLLATE NOCASE HAVING COUNT(*) > 1
            """
        ).fetchall()
        for row in duplicates:
            name = row["name"]
            tasks = conn.execute(
                "SELECT id, name FROM tasks WHERE name = ? COLLATE NOCASE ORDER BY id",
                (name,),
            ).fetchall()
            for index, task in enumerate(tasks[1:], start=2):
                candidate = f"{name}-{index}"
                suffix = index
                while conn.execute(
                    "SELECT 1 FROM tasks WHERE name = ? COLLATE NOCASE", (candidate,)
                ).fetchone():
                    suffix += 1
                    candidate = f"{name}-{suffix}"
                conn.execute(
                    "UPDATE tasks SET name = ?, updated_at = datetime('now') WHERE id = ?",
                    (candidate, task["id"]),
                )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks_new (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                name                     TEXT    NOT NULL UNIQUE,
                description              TEXT    NOT NULL DEFAULT '',
                repo_url                 TEXT    NOT NULL,
                repo_branch              TEXT    NOT NULL DEFAULT 'main',
                repo_subdir              TEXT    NOT NULL DEFAULT '',
                git_depth                INTEGER NOT NULL DEFAULT 1,
                git_username             TEXT    NOT NULL DEFAULT '',
                git_token                TEXT    NOT NULL DEFAULT '',
                schedule_type            TEXT    NOT NULL DEFAULT 'interval',
                schedule_expression      TEXT    NOT NULL DEFAULT '1h',
                enabled                  INTEGER NOT NULL DEFAULT 1,
                deploy_method            TEXT    NOT NULL DEFAULT 'script',
                prepare_script           TEXT    NOT NULL DEFAULT '',
                deploy_script            TEXT    NOT NULL DEFAULT '',
                rollback_script          TEXT    NOT NULL DEFAULT '',
                artifact_paths           TEXT    NOT NULL DEFAULT '',
                target_dir               TEXT    NOT NULL DEFAULT '',
                keep_releases            INTEGER NOT NULL DEFAULT 5,
                service_name             TEXT    NOT NULL DEFAULT '',
                docker_image             TEXT    NOT NULL DEFAULT '',
                docker_command           TEXT    NOT NULL DEFAULT '',
                docker_compose_file      TEXT    NOT NULL DEFAULT '',
                rsync_target             TEXT    NOT NULL DEFAULT '',
                rsync_options            TEXT    NOT NULL DEFAULT '-az --delete',
                env_vars                 TEXT    NOT NULL DEFAULT '{}',
                timeout_seconds          INTEGER NOT NULL DEFAULT 1800,
                notify_webhook           TEXT    NOT NULL DEFAULT '',
                skip_if_no_changes       INTEGER NOT NULL DEFAULT 1,
                run_on_create            INTEGER NOT NULL DEFAULT 0,
                created_at               TEXT    NOT NULL,
                updated_at               TEXT    NOT NULL,
                next_run_at              TEXT,
                last_run_at              TEXT,
                last_run_id              INTEGER,
                last_status              TEXT    NOT NULL DEFAULT '',
                run_count                INTEGER NOT NULL DEFAULT 0,
                success_count            INTEGER NOT NULL DEFAULT 0,
                failure_count            INTEGER NOT NULL DEFAULT 0,
                total_duration_ms        INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO tasks_new
                SELECT * FROM tasks;
            DROP TABLE tasks;
            ALTER TABLE tasks_new RENAME TO tasks;
            CREATE INDEX IF NOT EXISTS idx_tasks_next_run ON tasks(enabled, next_run_at);
            """
        )

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        """v3: 新增全局凭据表，并给任务增加 credential_id 外键。

        credentials 表本身由 SCHEMA 的 CREATE TABLE IF NOT EXISTS 建立；
        这里只需给已存在的 tasks 表补列（SQLite 支持 ADD COLUMN）。
        任务原有的 git_username/git_token 保持不变，作为未引用凭据时的回退。
        """
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "credential_id" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN credential_id INTEGER "
                "REFERENCES credentials(id) ON DELETE SET NULL"
            )

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        """v4: 任务增加 Webhook 触发令牌。

        新库由 SCHEMA 直接建列；这里只给老库补列，并为所有还没有令牌的
        存量任务生成一个，保证每个任务开箱即有一个可用的触发地址。
        """
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "webhook_secret" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN webhook_secret TEXT NOT NULL DEFAULT ''"
            )

    def _migrate_v4_to_v5(self, conn: sqlite3.Connection) -> None:
        """v5: 新增数据备份记录表 backups。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS backups (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                filename           TEXT    NOT NULL UNIQUE,
                filepath           TEXT    NOT NULL,
                size_bytes         INTEGER NOT NULL DEFAULT 0,
                sha256             TEXT    NOT NULL DEFAULT '',
                trigger            TEXT    NOT NULL DEFAULT 'manual',
                contains_logs      INTEGER NOT NULL DEFAULT 0,
                contains_artifacts INTEGER NOT NULL DEFAULT 0,
                contains_persistent INTEGER NOT NULL DEFAULT 0,
                status             TEXT    NOT NULL DEFAULT 'success',
                error              TEXT    NOT NULL DEFAULT '',
                created_at         TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_backups_created ON backups(id DESC);
            """
        )

    def _migrate_v5_to_v6(self, conn: sqlite3.Connection) -> None:
        """v6: 新增轻量主机时序采样表 host_metrics。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS host_metrics (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                 TEXT    NOT NULL,
                cpu_percent        REAL    NOT NULL,
                memory_percent     REAL    NOT NULL,
                disk_read_rate     REAL    NOT NULL DEFAULT 0.0,
                disk_write_rate    REAL    NOT NULL DEFAULT 0.0,
                net_rx_rate        REAL    NOT NULL DEFAULT 0.0,
                net_tx_rate        REAL    NOT NULL DEFAULT 0.0
            );
            CREATE INDEX IF NOT EXISTS idx_host_metrics_ts ON host_metrics(ts);
            """
        )
        from .security import new_token

        for row in conn.execute(
            "SELECT id FROM tasks WHERE webhook_secret = ''"
        ).fetchall():
            conn.execute(
                "UPDATE tasks SET webhook_secret = ? WHERE id = ?",
                (new_token(), row["id"]),
            )

    def _migrate_v6_to_v7(self, conn: sqlite3.Connection) -> None:
        """v7: 新增容器维度时序采样表 container_metrics。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS container_metrics (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                 TEXT    NOT NULL,
                container_id       TEXT    NOT NULL,
                container_name     TEXT    NOT NULL,
                compose_project    TEXT    NOT NULL DEFAULT '',
                cpu_percent        REAL    NOT NULL DEFAULT 0.0,
                memory_bytes       REAL    NOT NULL DEFAULT 0.0,
                memory_percent     REAL    NOT NULL DEFAULT 0.0,
                disk_read_rate     REAL    NOT NULL DEFAULT 0.0,
                disk_write_rate    REAL    NOT NULL DEFAULT 0.0,
                net_rx_rate        REAL    NOT NULL DEFAULT 0.0,
                net_tx_rate        REAL    NOT NULL DEFAULT 0.0
            );
            CREATE INDEX IF NOT EXISTS idx_container_metrics_ts ON container_metrics(ts);
            CREATE INDEX IF NOT EXISTS idx_container_metrics_cid_ts ON container_metrics(container_id, ts);
            """
        )

    def schema_version(self) -> int:
        try:
            return int(self.scalar("SELECT MAX(version) FROM schema_version", default=0) or 0)
        except sqlite3.Error:
            return 0


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

TASK_JSON_FIELDS = ("env_vars",)


def decode_task(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert a raw task row into API shape (ints as bools, JSON decoded)."""
    if row is None:
        return None
    task = dict(row)
    task["enabled"] = bool(task.get("enabled"))
    task["webhook_enabled"] = bool(task.get("webhook_enabled", 1))
    task["skip_if_no_changes"] = bool(task.get("skip_if_no_changes"))
    task["run_on_create"] = bool(task.get("run_on_create"))
    raw_env = task.get("env_vars") or "{}"
    try:
        parsed = json.loads(raw_env)
        task["env_vars"] = parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        task["env_vars"] = {}
    task["has_token"] = bool(task.get("git_token"))
    # Never leak the stored credential; the UI only needs to know one is set.
    task.pop("git_token", None)
    # webhook_secret 与 git_token 不同：触发地址要在界面上完整展示与复制，
    # 因此随已登录接口返回（导出与备份由各自出口负责剥离）。
    return task


def decode_run(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    run = dict(row)
    run["changes_detected"] = bool(run.get("changes_detected"))
    run["cancel_requested"] = bool(run.get("cancel_requested"))
    return run


def encode_env_vars(env: Any) -> str:
    if isinstance(env, str):
        stripped = env.strip()
        if not stripped:
            return "{}"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = _parse_key_value_lines(stripped)
        env = parsed
    if not isinstance(env, dict):
        return "{}"
    cleaned = {str(k): str(v) for k, v in env.items() if str(k).strip()}
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)


def _parse_key_value_lines(text: str) -> dict[str, str]:
    """Accept ``KEY=value`` lines as a convenience for the UI textarea."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result
