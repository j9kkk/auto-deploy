"""Repository layer: all SQL for users, sessions, tasks, runs and audit logs.

Keeping every statement in one module means the API handlers and the scheduler
never build SQL themselves, and the column list for a task lives in exactly one
place.  Rows are returned as plain dicts with bools and JSON already decoded.
"""

from __future__ import annotations

from typing import Any

from . import sshkey
from .db import Database, decode_run, decode_task, encode_env_vars
from .schedule import iso, utcnow
from .security import new_token

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

TASK_INSERT_COLUMNS = (
    "name", "description",
    "repo_url", "repo_branch", "repo_subdir", "git_depth", "git_username", "git_token",
    "credential_id",
    "schedule_type", "schedule_expression", "enabled", "webhook_secret",
    "deploy_method", "prepare_script", "deploy_script", "rollback_script",
    "artifact_paths", "target_dir", "keep_releases",
    "service_name", "docker_image", "docker_command", "docker_compose_file",
    "rsync_target", "rsync_options",
    "env_vars", "timeout_seconds", "notify_webhook", "skip_if_no_changes", "run_on_create",
)

TASK_UPDATE_COLUMNS = (
    "name", "description",
    "repo_url", "repo_branch", "repo_subdir", "git_depth", "git_username", "git_token",
    "credential_id",
    "schedule_type", "schedule_expression", "enabled", "webhook_secret",
    "deploy_method", "prepare_script", "deploy_script", "rollback_script",
    "artifact_paths", "target_dir", "keep_releases",
    "service_name", "docker_image", "docker_command", "docker_compose_file",
    "rsync_target", "rsync_options",
    "env_vars", "timeout_seconds", "notify_webhook", "skip_if_no_changes", "run_on_create",
)

# Statuses a run can be in.  ``queued`` and ``running`` are the live ones.
RUN_STATUSES = ("queued", "running", "success", "failed", "cancelled", "skipped")
ACTIVE_RUN_STATUSES = ("queued", "running")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM users", default=0) or 0)

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        )

    def get(self, user_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def list_all(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM users ORDER BY id")

    def create(
        self,
        username: str,
        password_hash: str,
        *,
        display_name: str = "",
        is_admin: bool = True,
    ) -> int:
        now = iso(utcnow())
        return self.db.execute(
            """
            INSERT INTO users (username, password_hash, display_name, is_admin,
                               created_at, updated_at, password_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (username, password_hash, display_name, 1 if is_admin else 0, now, now, now),
        )

    def update_password(self, user_id: int, password_hash: str) -> int:
        now = iso(utcnow())
        return self.db.execute_rowcount(
            "UPDATE users SET password_hash = ?, password_changed_at = ?, updated_at = ? WHERE id = ?",
            (password_hash, now, now, user_id),
        )

    def update_username(self, user_id: int, username: str) -> int:
        return self.db.execute_rowcount(
            "UPDATE users SET username = ?, updated_at = ? WHERE id = ?",
            (username, iso(utcnow()), user_id),
        )

    def touch_login(self, user_id: int) -> None:
        self.db.execute_rowcount(
            "UPDATE users SET last_login_at = ? WHERE id = ?", (iso(utcnow()), user_id)
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class SessionRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(
        self,
        token_hash: str,
        user_id: int,
        expires_at: str,
        *,
        ip: str = "",
        user_agent: str = "",
    ) -> int:
        now = iso(utcnow())
        return self.db.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, now, expires_at, now, ip, user_agent[:400]),
        )

    def find_valid(self, token_hash: str) -> dict[str, Any] | None:
        """Return the session joined with its user, if unexpired."""
        row = self.db.query_one(
            """
            SELECT s.id AS session_id, s.user_id, s.expires_at, s.created_at AS session_created_at,
                   u.id, u.username, u.display_name, u.is_admin, u.password_changed_at, u.last_login_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, iso(utcnow())),
        )
        return row

    def touch(self, session_id: int) -> None:
        self.db.execute_rowcount(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?", (iso(utcnow()), session_id)
        )

    def delete(self, token_hash: str) -> int:
        return self.db.execute_rowcount(
            "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
        )

    def delete_for_user(self, user_id: int, *, keep_token_hash: str | None = None) -> int:
        if keep_token_hash:
            return self.db.execute_rowcount(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (user_id, keep_token_hash),
            )
        return self.db.execute_rowcount("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_expired(self) -> int:
        return self.db.execute_rowcount(
            "DELETE FROM sessions WHERE expires_at <= ?", (iso(utcnow()),)
        )

    def list_active(self) -> list[dict[str, Any]]:
        return self.db.query(
            """
            SELECT s.id, s.user_id, s.created_at, s.expires_at, s.last_seen_at, s.ip, s.user_agent,
                   u.username
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.expires_at > ?
            ORDER BY s.id DESC
            """,
            (iso(utcnow()),),
        )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

CREDENTIAL_KINDS = ("https_token", "ssh_key")


def decode_credential(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """凭据对外表示：永不包含 secret 明文，只暴露是否已设置。"""
    if row is None:
        return None
    item = dict(row)
    secret = item.pop("secret", "") or ""
    item["has_secret"] = bool(secret)
    item["secret_length"] = len(secret)
    # 私钥口令同样是机密：只暴露“是否设置”，绝不回传明文。
    passphrase = item.pop("passphrase", "") or ""
    item["has_passphrase"] = bool(passphrase)
    # SSH 私钥的公开信息：指纹用于辨认是哪把钥匙，公钥供用户重新配置
    # GitHub Deploy Key 时复制。两者都不属于机密，可以安全返回前端。
    if item.get("kind") == "ssh_key" and secret:
        item["fingerprint"] = ssh_key_fingerprint(secret)
        item["public_key"] = sshkey.public_key_line(secret)
    return item


def ssh_key_fingerprint(private_key: str) -> str:
    """计算 SSH 公钥指纹，避免把私钥内容泄露给前端。

    实现见 :mod:`app.sshkey`：必须对公钥块取摘要，直接用私钥文件内容哈希
    会得到与 ``ssh-keygen -lf`` / GitHub 完全不同的值。
    """
    return sshkey.fingerprint(private_key)


class CredentialRepository:
    """全局 Git 凭据（HTTPS 令牌 / SSH 私钥）。

    凭据集中存放的意义在于：多处复用的令牌只需维护一份，轮换时改一处即可，
    并且可以在界面上主动测试是否仍然有效。
    """

    def __init__(self, database: Database) -> None:
        self.db = database

    def create(self, data: dict[str, Any]) -> int:
        now = iso(utcnow())
        return self.db.execute(
            """
            INSERT INTO credentials (name, kind, username, secret, passphrase, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("name", ""), data.get("kind", "https_token"),
                data.get("username", ""), data.get("secret", ""),
                data.get("passphrase", ""),
                data.get("description", ""), now, now,
            ),
        )

    def update(self, credential_id: int, data: dict[str, Any]) -> bool:
        columns: list[str] = []
        values: list[Any] = []
        for key in ("name", "kind", "username", "description", "passphrase"):
            if key in data:
                columns.append(f"{key} = ?")
                values.append(data[key])
        # 只有显式给出 secret 时才覆盖，留空表示保持原值。
        if data.get("secret"):
            columns.append("secret = ?")
            values.append(data["secret"])
        if not columns:
            return False
        values += [iso(utcnow()), credential_id]
        sql = f"UPDATE credentials SET {', '.join(columns)}, updated_at = ? WHERE id = ?"
        return self.db.execute_rowcount(sql, values) > 0

    def get(self, credential_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM credentials WHERE id = ?", (credential_id,))

    def get_decoded(self, credential_id: int) -> dict[str, Any] | None:
        return decode_credential(self.get(credential_id))

    def list_all(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM credentials ORDER BY name COLLATE NOCASE")

    def list_decoded(self) -> list[dict[str, Any]]:
        return [decode_credential(row) or {} for row in self.list_all()]

    def name_taken(self, name: str, *, exclude_id: int | None = None) -> bool:
        if not str(name or "").strip():
            return False
        if exclude_id is None:
            row = self.db.query_one(
                "SELECT 1 FROM credentials WHERE name = ? COLLATE NOCASE", (str(name),)
            )
        else:
            row = self.db.query_one(
                "SELECT 1 FROM credentials WHERE name = ? COLLATE NOCASE AND id != ?",
                (str(name), exclude_id),
            )
        return row is not None

    def delete(self, credential_id: int) -> bool:
        return self.db.execute_rowcount(
            "DELETE FROM credentials WHERE id = ?", (credential_id,)
        ) > 0

    def usage_count(self, credential_id: int) -> int:
        """有多少任务正在引用该凭据（删除前提示用）。"""
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM tasks WHERE credential_id = ?",
                (credential_id,),
                default=0,
            )
            or 0
        )

    def usage_counts(self) -> dict[int, int]:
        """全部凭据的引用数，一条 GROUP BY 取回，供列表页避免 N+1。"""
        rows = self.db.query(
            "SELECT credential_id, COUNT(*) AS n FROM tasks "
            "WHERE credential_id IS NOT NULL GROUP BY credential_id"
        )
        return {int(row["credential_id"]): int(row["n"]) for row in rows}

    def record_test(self, credential_id: int, *, ok: bool, error: str = "") -> None:
        self.db.execute_rowcount(
            """
            UPDATE credentials
               SET last_tested_at = ?, last_test_ok = ?, last_test_error = ?
             WHERE id = ?
            """,
            (iso(utcnow()), 1 if ok else 0, error[:500], credential_id),
        )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TaskNameConflict(ValueError):
    """任务名已被占用；API 可精准转换为字段校验错误。"""

    def __init__(self) -> None:
        super().__init__("任务名已被使用（不区分大小写），请换一个名称")


class TaskRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def _payload(self, data: dict[str, Any]) -> dict[str, Any]:
        """Normalise an incoming task dict to storable values."""
        payload = dict(data)
        if "env_vars" in payload:
            payload["env_vars"] = encode_env_vars(payload["env_vars"])
        for flag in ("enabled", "skip_if_no_changes", "run_on_create"):
            if flag in payload and isinstance(payload[flag], bool):
                payload[flag] = 1 if payload[flag] else 0
        return payload

    def create(self, data: dict[str, Any]) -> int:
        payload = self._payload(data)
        # Webhook 触发令牌由仓储层兜底生成：调用方（校验层）不产出该字段，
        # 每个任务都必须有一个可用触发地址，缺失会导致迁移后才补齐。
        if not str(payload.get("webhook_secret") or ""):
            payload["webhook_secret"] = new_token()
        now = iso(utcnow())
        columns = [c for c in TASK_INSERT_COLUMNS if c in payload]
        values = [payload[c] for c in columns]
        columns += ["created_at", "updated_at"]
        values += [now, now]
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})"
        # 与所有仓储写入共享同一把 RLock，检查与写入不可被并发请求穿插。
        # 不增加启动迁移，也不擅自改名已有的大小写冲突记录。
        with self.db._lock:
            if self.name_taken(payload.get("name", "")):
                raise TaskNameConflict()
            return self.db.execute(sql, values)

    def update(self, task_id: int, data: dict[str, Any]) -> bool:
        payload = self._payload(data)
        columns = [c for c in TASK_UPDATE_COLUMNS if c in payload]
        if not columns:
            return False
        assignments = ", ".join(f"{c} = ?" for c in columns)
        values = [payload[c] for c in columns]
        values += [iso(utcnow()), task_id]
        sql = f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?"
        with self.db._lock:
            if "name" in payload:
                existing = self.get(task_id)
                # 存量重名任务未改名时仍可保存其他配置。
                if existing is not None and payload["name"] != existing["name"]:
                    if self.name_taken(payload["name"], exclude_id=task_id):
                        raise TaskNameConflict()
            return self.db.execute_rowcount(sql, values) > 0

    def get(self, task_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def name_taken(self, name: str, *, exclude_id: int | None = None) -> bool:
        """任务名是否已被占用（大小写不敏感；``exclude_id`` 用于改名场景）。"""
        if not str(name or "").strip():
            return False
        if exclude_id is None:
            row = self.db.query_one(
                "SELECT 1 FROM tasks WHERE name = ? COLLATE NOCASE", (str(name),)
            )
        else:
            row = self.db.query_one(
                "SELECT 1 FROM tasks WHERE name = ? COLLATE NOCASE AND id != ?",
                (str(name), exclude_id),
            )
        return row is not None

    def get_decoded(self, task_id: int) -> dict[str, Any] | None:
        return decode_task(self.get(task_id))

    def get_internal(self, task_id: int) -> dict[str, Any] | None:
        """Row including the git token, for the runner only."""
        return self.get(task_id)

    def list_all(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM tasks ORDER BY id")

    def list_decoded(self) -> list[dict[str, Any]]:
        return [decode_task(row) or {} for row in self.list_all()]

    def list_enabled(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM tasks WHERE enabled = 1 ORDER BY id")

    def due(self, now_iso: str) -> list[dict[str, Any]]:
        """Scheduled tasks whose next fire time has passed."""
        return self.db.query(
            """
            SELECT * FROM tasks
            WHERE enabled = 1
              AND next_run_at IS NOT NULL
              AND next_run_at <= ?
              AND schedule_type IN ('interval', 'cron')
            ORDER BY next_run_at ASC
            """,
            (now_iso,),
        )

    def delete(self, task_id: int) -> bool:
        return self.db.execute_rowcount("DELETE FROM tasks WHERE id = ?", (task_id,)) > 0

    def set_next_run(self, task_id: int, next_run_at: str | None) -> None:
        self.db.execute_rowcount(
            "UPDATE tasks SET next_run_at = ?, updated_at = ? WHERE id = ?",
            (next_run_at, iso(utcnow()), task_id),
        )

    def set_enabled(self, task_id: int, enabled: bool, next_run_at: str | None) -> None:
        self.db.execute_rowcount(
            "UPDATE tasks SET enabled = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, next_run_at, iso(utcnow()), task_id),
        )

    def record_run_started(self, task_id: int) -> None:
        self.db.execute_rowcount(
            "UPDATE tasks SET last_run_at = ?, last_run_id = NULL, updated_at = ? WHERE id = ?",
            (iso(utcnow()), iso(utcnow()), task_id),
        )

    def record_run_finished(
        self, task_id: int, *, run_id: int, status: str, duration_ms: int
    ) -> None:
        """Update the denormalised counters the dashboard reads."""
        success = 1 if status == "success" else 0
        failure = 1 if status in ("failed", "cancelled") else 0
        self.db.execute_rowcount(
            """
            UPDATE tasks
               SET last_run_id = ?,
                   last_status = ?,
                   last_run_at = ?,
                   run_count = run_count + 1,
                   success_count = success_count + ?,
                   failure_count = failure_count + ?,
                   total_duration_ms = total_duration_ms + ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (
                run_id, status, iso(utcnow()), success, failure, duration_ms,
                iso(utcnow()), task_id,
            ),
        )

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM tasks", default=0) or 0)

    def clear_runtime_state(self) -> None:
        """Drop next-run times after a restart so nothing fires twice."""
        self.db.execute_rowcount("UPDATE tasks SET next_run_at = NULL")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

class RunRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(
        self,
        task: dict[str, Any],
        *,
        trigger: str = "manual",
        commit_before: str = "",
    ) -> int:
        now = iso(utcnow())
        return self.db.execute(
            """
            INSERT INTO runs (task_id, task_name, status, trigger, deploy_method,
                              queued_at, commit_before, branch, log_path)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)
            """,
            (
                task["id"],
                task.get("name", ""),
                trigger,
                task.get("deploy_method", ""),
                now,
                commit_before,
                task.get("repo_branch", ""),
                "",
            ),
        )

    def get(self, run_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def get_decoded(self, run_id: int) -> dict[str, Any] | None:
        return decode_run(self.get(run_id))

    def list_for_task(
        self, task_id: int, *, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        return [
            decode_run(row) or {}
            for row in self.db.query(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            )
        ]

    def latest_by_task(self) -> dict[int, dict[str, Any]]:
        """每条任务取最近一次运行，供任务列表批量装饰，避免 N+1 查询。"""
        rows = self.db.query(
            "SELECT * FROM runs WHERE id IN "
            "(SELECT MAX(id) FROM runs GROUP BY task_id)"
        )
        return {
            int(row["task_id"]): decode_run(row) or {}
            for row in rows
            if row["task_id"] is not None
        }

    def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        task_id: int | None = None,
        search: str = "",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if search:
            clauses.append("(task_name LIKE ? OR commit_message LIKE ? OR error LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = self.db.query(
            f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT ? OFFSET ?", params
        )
        return [decode_run(row) or {} for row in rows]

    def count(
        self, *, status: str = "", task_id: int | None = None, search: str = ""
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if search:
            clauses.append("(task_name LIKE ? OR commit_message LIKE ? OR error LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(self.db.scalar(f"SELECT COUNT(*) FROM runs {where}", params, default=0) or 0)

    def active(self) -> list[dict[str, Any]]:
        return [
            decode_run(row) or {}
            for row in self.db.query(
                "SELECT * FROM runs WHERE status IN ('queued', 'running') ORDER BY id ASC"
            )
        ]

    def count_active(self) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')", default=0
            )
            or 0
        )

    def has_active_for_task(self, task_id: int) -> bool:
        return bool(
            self.db.scalar(
                "SELECT COUNT(*) FROM runs WHERE task_id = ? AND status IN ('queued', 'running')",
                (task_id,),
                default=0,
            )
        )

    def log_paths_for_task(self, task_id: int) -> list[str]:
        """该任务所有运行记录的日志路径。

        删除任务前必须先取出来：runs 随任务级联删除，之后这些路径就无从
        查找，日志文件会永久留在磁盘上成为孤儿。
        """
        return [
            str(row["log_path"])
            for row in self.db.query(
                "SELECT log_path FROM runs WHERE task_id = ? AND log_path != ''",
                (task_id,),
            )
            if row.get("log_path")
        ]

    def mark_running(self, run_id: int, *, pid: int | None = None) -> None:
        self.db.execute_rowcount(
            "UPDATE runs SET status = 'running', started_at = ?, pid = ? WHERE id = ?",
            (iso(utcnow()), pid, run_id),
        )

    def mark_finished(
        self,
        run_id: int,
        *,
        status: str,
        exit_code: int | None,
        error: str = "",
        duration_ms: int = 0,
    ) -> None:
        self.db.execute_rowcount(
            """
            UPDATE runs
               SET status = ?, finished_at = ?, exit_code = ?, error = ?,
                   duration_ms = ?
             WHERE id = ?
            """,
            (status, iso(utcnow()), exit_code, error[:2000], duration_ms, run_id),
        )

    def update_checkout_info(
        self,
        run_id: int,
        *,
        commit_after: str = "",
        commit_message: str = "",
        commit_author: str = "",
        commit_time: str = "",
        branch: str = "",
        changed_files: int = 0,
        changes_detected: bool = True,
    ) -> None:
        self.db.execute_rowcount(
            """
            UPDATE runs
               SET commit_after = ?, commit_message = ?, commit_author = ?, commit_time = ?,
                   branch = ?, changed_files = ?, changes_detected = ?
             WHERE id = ?
            """,
            (
                commit_after, commit_message[:1000], commit_author[:200], commit_time,
                branch, changed_files, 1 if changes_detected else 0, run_id,
            ),
        )

    def update_outputs(
        self, run_id: int, *, release_dir: str = "", artifact_path: str = ""
    ) -> None:
        self.db.execute_rowcount(
            "UPDATE runs SET release_dir = ?, artifact_path = ? WHERE id = ?",
            (release_dir, artifact_path, run_id),
        )

    def set_log(self, run_id: int, log_path: str, log_bytes: int) -> None:
        self.db.execute_rowcount(
            "UPDATE runs SET log_path = ?, log_bytes = ? WHERE id = ?",
            (log_path, log_bytes, run_id),
        )

    def request_cancel(self, run_id: int) -> bool:
        return self.db.execute_rowcount(
            "UPDATE runs SET cancel_requested = 1 WHERE id = ? AND status IN ('queued', 'running')",
            (run_id,),
        ) > 0

    def is_cancel_requested(self, run_id: int) -> bool:
        return bool(
            self.db.scalar("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,), default=0)
        )

    def finish_stale(self, *, reason: str) -> int:
        """Fail runs left active by an unclean shutdown."""
        now = iso(utcnow())
        return self.db.execute_rowcount(
            """
            UPDATE runs
               SET status = 'failed', finished_at = ?, error = ?
             WHERE status IN ('queued', 'running')
            """,
            (now, reason),
        )

    # -- statistics --------------------------------------------------------
    def stats_overview(self) -> dict[str, Any]:
        row = self.db.query_one(
            """
            SELECT COUNT(*)                                    AS total,
                   COALESCE(SUM(status = 'success'), 0)        AS success,
                   COALESCE(SUM(status = 'failed'), 0)         AS failed,
                   COALESCE(SUM(status = 'cancelled'), 0)      AS cancelled,
                   COALESCE(SUM(status = 'skipped'), 0)        AS skipped,
                   COALESCE(SUM(status IN ('queued','running')), 0) AS active,
                   COALESCE(SUM(duration_ms), 0)               AS total_duration_ms,
                   COALESCE(AVG(NULLIF(duration_ms, 0)), 0)    AS avg_duration_ms
            FROM runs
            """
        ) or {}
        total = int(row.get("total") or 0)
        success = int(row.get("success") or 0)
        failed = int(row.get("failed") or 0)
        finished = success + failed
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "cancelled": int(row.get("cancelled") or 0),
            "skipped": int(row.get("skipped") or 0),
            "active": int(row.get("active") or 0),
            "total_duration_ms": int(row.get("total_duration_ms") or 0),
            "avg_duration_ms": int(row.get("avg_duration_ms") or 0),
            "success_rate": round(success * 100.0 / finished, 1) if finished else 0.0,
        }

    def stats_daily(self, days: int = 14) -> list[dict[str, Any]]:
        from datetime import datetime, timedelta

        from .schedule import from_iso, local_date

        # 按系统时区的日历日分桶（用户看到的日期），而非存储用的 UTC 日期。
        today = datetime.fromisoformat(local_date(utcnow()))
        buckets = {
            (today - timedelta(days=offset)).date().isoformat()
            for offset in range(days)
        }
        cutoff = iso((utcnow() - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0))
        rows = self.db.query(
            "SELECT queued_at, status FROM runs WHERE queued_at >= ?",
            (cutoff,),
        )
        empty = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
        by_day: dict[str, dict[str, Any]] = {}
        for row in rows:
            day = local_date(from_iso(row["queued_at"]))
            if day not in buckets:
                continue
            stat = by_day.setdefault(day, {"day": day, **empty})
            stat["total"] += 1
            status = row["status"]
            if status in ("success", "failed", "skipped"):
                stat[status] += 1
        return [
            by_day.get(day, {"day": day, **empty})
            for day in sorted(buckets)
        ]

    def stats_by_task(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.query(
            """
            SELECT t.id, t.name, t.enabled, t.last_status, t.last_run_at, t.next_run_at,
                   t.run_count, t.success_count, t.failure_count,
                   COALESCE(SUM(r.duration_ms), 0) AS total_duration_ms
            FROM tasks t
            LEFT JOIN runs r ON r.task_id = t.id
            GROUP BY t.id
            ORDER BY t.run_count DESC, t.id ASC
            LIMIT ?
            """,
            (limit,),
        )

    def purge_old(
        self, *, older_than_iso: str | None, keep_count: int
    ) -> tuple[int, list[str]]:
        """Delete stale runs.

        Returns ``(deleted_run_count, log_paths_to_remove)``.  Active runs are
        never deleted, no matter how old they look.
        """
        victims: list[dict[str, Any]] = []
        if older_than_iso:
            victims.extend(
                self.db.query(
                    "SELECT id, log_path FROM runs WHERE queued_at < ? AND status NOT IN ('queued','running')",
                    (older_than_iso,),
                )
            )
        if keep_count > 0:
            victims.extend(
                self.db.query(
                    """
                    SELECT id, log_path FROM runs
                    WHERE status NOT IN ('queued','running')
                      AND id NOT IN (
                          SELECT id FROM runs
                          WHERE status NOT IN ('queued','running')
                          ORDER BY id DESC LIMIT ?
                      )
                    """,
                    (keep_count,),
                )
            )
        seen: set[int] = set()
        logs: list[str] = []
        ids: list[int] = []
        for victim in victims:
            if victim["id"] in seen:
                continue
            seen.add(int(victim["id"]))
            ids.append(int(victim["id"]))
            if victim.get("log_path"):
                logs.append(victim["log_path"])
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            deleted = self.db.execute_rowcount(
                f"DELETE FROM runs WHERE id IN ({placeholders}) AND status NOT IN ('queued','running')",
                ids,
            )
            return deleted, logs
        return 0, logs


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def record(
        self, action: str, *, actor: str = "", target: str = "", detail: str = "", ip: str = ""
    ) -> int:
        return self.db.execute(
            "INSERT INTO audit_logs (ts, actor, action, target, detail, ip) VALUES (?, ?, ?, ?, ?, ?)",
            (iso(utcnow()), actor, action, target, detail[:2000], ip),
        )

    def list_recent(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def purge_old(self, keep: int = 2000) -> int:
        return self.db.execute_rowcount(
            """
            DELETE FROM audit_logs
            WHERE id NOT IN (SELECT id FROM audit_logs ORDER BY id DESC LIMIT ?)
            """,
            (keep,),
        )


# ---------------------------------------------------------------------------
# Date helpers for the charts
# ---------------------------------------------------------------------------


class Store:
    """Aggregate handle giving access to every repository."""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or Database()
        self.db.init_schema()
        self.users = UserRepository(self.db)
        self.sessions = SessionRepository(self.db)
        self.credentials = CredentialRepository(self.db)
        self.tasks = TaskRepository(self.db)
        self.runs = RunRepository(self.db)
        self.audit = AuditRepository(self.db)

    def close(self) -> None:
        self.db.close()

    def export_snapshot(self) -> dict[str, Any]:
        """Everything needed for a backup, with secrets stripped."""
        tasks = []
        for row in self.tasks.list_all():
            task = decode_task(row) or {}
            # Webhook 触发令牌同样属于机密，不写入备份文件。
            task.pop("webhook_secret", None)
            tasks.append(task)
        return {
            "exported_at": iso(utcnow()),
            "tasks": tasks,
            "runs": self.runs.list_recent(limit=2000),
        }
