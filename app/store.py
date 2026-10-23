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
    "compose_content",
    "rsync_target", "rsync_options",
    "host_id", "source_mode", "data_sources",
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
    "compose_content",
    "rsync_target", "rsync_options",
    "host_id", "source_mode", "data_sources",
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

    def host_usage_count(self, credential_id: int) -> int:
        """有多少主机正在用该 SSH 私钥登录。

        主机与任务是两类独立引用：一份私钥常常只用于登录服务器（不碰仓库），
        漏掉这一边会让删除提示与删除保护都失真。
        """
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM hosts WHERE credential_id = ?",
                (credential_id,),
                default=0,
            )
            or 0
        )

    def host_usage_counts(self) -> dict[int, int]:
        """全部凭据被主机引用的数量，一条 GROUP BY 取回。"""
        rows = self.db.query(
            "SELECT credential_id, COUNT(*) AS n FROM hosts "
            "WHERE credential_id IS NOT NULL GROUP BY credential_id"
        )
        return {int(row["credential_id"]): int(row["n"]) for row in rows}

    def hosts_using(self, credential_id: int) -> list[dict[str, Any]]:
        """引用该凭据的主机（删除确认里要点名，避免用户盲删）。

        带上 port / username：凭据页的「测试主机登录」直接用它们回填表单，
        用户不必手抄登录账号。
        """
        return self.db.query(
            "SELECT id, name, address, port, username FROM hosts WHERE credential_id = ? "
            "ORDER BY name COLLATE NOCASE",
            (credential_id,),
        )

    def record_test(self, credential_id: int, *, ok: bool, error: str = "") -> None:
        self.db.execute_rowcount(
            """
            UPDATE credentials
               SET last_tested_at = ?, last_test_ok = ?, last_test_error = ?
             WHERE id = ?
            """,
            (iso(utcnow()), 1 if ok else 0, error[:500], credential_id),
        )

    def record_host_test(self, credential_id: int, *, ok: bool, error: str = "") -> None:
        """记录「用该私钥登录主机」的结果。

        与仓库测试分列存放：同一把钥匙可能只用于仓库（或只用于主机），
        合并成一列会让另一侧的结论被覆盖掉。
        """
        self.db.execute_rowcount(
            """
            UPDATE credentials
               SET last_host_test_at = ?, last_host_test_ok = ?, last_host_test_error = ?
             WHERE id = ?
            """,
            (iso(utcnow()), 1 if ok else 0, error[:500], credential_id),
        )

    def clear_tests(self, credential_id: int) -> None:
        """私钥/口令变更后两类测试结论都失效。"""
        self.db.execute_rowcount(
            """
            UPDATE credentials
               SET last_tested_at = NULL, last_test_ok = NULL,
                   last_test_error = '凭据已修改，请重新测试',
                   last_host_test_at = NULL, last_host_test_ok = NULL,
                   last_host_test_error = '凭据已修改，请重新测试'
             WHERE id = ?
            """,
            (credential_id,),
        )


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

HOST_STATUSES = ("unknown", "online", "offline", "error")


class HostRepository:
    """远程主机（SSH 目标）。

    主机是「部署到哪里」的一等对象。凭据引用 credentials 表的 ssh_key 记录，
    因此秘密只在一处落库；host_key 保存首次连接后钉住的主机指纹，用于检测
    指纹变化（潜在的中间人替换）。
    """

    def __init__(self, database: Database) -> None:
        self.db = database

    def create(self, data: dict[str, Any]) -> int:
        now = iso(utcnow())
        return self.db.execute(
            """
            INSERT INTO hosts
                (name, address, port, username, credential_id, base_dir,
                 host_key, status, last_check_at, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("name", ""), data.get("address", ""),
                int(data.get("port") or 22), data.get("username", ""),
                data.get("credential_id"), data.get("base_dir") or "/opt/autodeploy",
                data.get("host_key") or "", data.get("status") or "unknown",
                data.get("last_check_at"), data.get("last_error") or "",
                now, now,
            ),
        )

    def update(self, host_id: int, data: dict[str, Any]) -> bool:
        columns: list[str] = []
        values: list[Any] = []
        for key in ("name", "address", "port", "username", "credential_id", "base_dir"):
            if key in data:
                columns.append(f"{key} = ?")
                values.append(data[key])
        if not columns:
            return False
        values += [iso(utcnow()), host_id]
        sql = f"UPDATE hosts SET {', '.join(columns)}, updated_at = ? WHERE id = ?"
        return self.db.execute_rowcount(sql, values) > 0

    def get(self, host_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM hosts WHERE id = ?", (host_id,))

    def list_all(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM hosts ORDER BY name COLLATE NOCASE")

    def name_taken(self, name: str, *, exclude_id: int | None = None) -> bool:
        if not str(name or "").strip():
            return False
        if exclude_id is None:
            row = self.db.query_one(
                "SELECT 1 FROM hosts WHERE name = ? COLLATE NOCASE", (str(name),)
            )
        else:
            row = self.db.query_one(
                "SELECT 1 FROM hosts WHERE name = ? COLLATE NOCASE AND id != ?",
                (str(name), exclude_id),
            )
        return row is not None

    def delete(self, host_id: int) -> bool:
        return self.db.execute_rowcount("DELETE FROM hosts WHERE id = ?", (host_id,)) > 0

    def usage_counts(self) -> dict[int, int]:
        """每个主机被多少任务引用，一条 GROUP BY 取回供列表页避免 N+1。"""
        rows = self.db.query(
            "SELECT host_id, COUNT(*) AS n FROM tasks "
            "WHERE host_id IS NOT NULL GROUP BY host_id"
        )
        return {int(row["host_id"]): int(row["n"]) for row in rows}

    def record_check(
        self, host_id: int, *, status: str, error: str = "", host_key: str | None = None
    ) -> None:
        """记录一次连通性检查。host_key 非空时同时钉住指纹。"""
        if host_key is None:
            self.db.execute_rowcount(
                "UPDATE hosts SET status = ?, last_check_at = ?, last_error = ? WHERE id = ?",
                (status, iso(utcnow()), error[:500], host_id),
            )
        else:
            self.db.execute_rowcount(
                "UPDATE hosts SET status = ?, last_check_at = ?, last_error = ?, host_key = ? "
                "WHERE id = ?",
                (status, iso(utcnow()), error[:500], host_key, host_id),
            )

    def snapshot_for_run(self, task: dict[str, Any]) -> tuple[int | None, str]:
        """取任务目标主机的 (id, 名称) 快照，供运行记录留存。

        主机可能在运行途中改名或被删除；名称在建立运行记录的瞬间写死，
        历史才不会随之后的配置变更而失真。
        """
        host_id = task.get("host_id")
        if not host_id:
            return None, ""
        row = self.get(int(host_id))
        if row is None:
            return int(host_id), f"（已删除的主机 #{host_id}）"
        return int(host_id), str(row.get("name") or "")


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
        host_id: int | None = None,
        host_name: str = "",
    ) -> int:
        """建立一条排队中的运行记录。

        ``host_name`` 是**快照**：主机之后改名或被删除，历史运行仍能显示
        当时部署到了哪台机器。未显式给出时按 task 上的 host_id 补全。
        """
        now = iso(utcnow())
        if host_id is None and task.get("host_id"):
            host_id = int(task["host_id"])
        return self.db.execute(
            """
            INSERT INTO runs (task_id, task_name, status, trigger, deploy_method,
                              queued_at, commit_before, branch, log_path,
                              host_id, host_name)
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
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
                host_id,
                host_name,
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
# Backups
# ---------------------------------------------------------------------------

class BackupRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(
        self,
        *,
        filename: str,
        filepath: str,
        size_bytes: int = 0,
        sha256: str = "",
        trigger: str = "manual",
        contains_logs: bool = False,
        contains_artifacts: bool = False,
        contains_persistent: bool = False,
        status: str = "success",
        error: str = "",
        created_at: str | None = None,
    ) -> int:
        return self.db.execute(
            """
            INSERT INTO backups (
                filename, filepath, size_bytes, sha256, trigger,
                contains_logs, contains_artifacts, contains_persistent,
                status, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filename,
                filepath,
                size_bytes,
                sha256,
                trigger,
                1 if contains_logs else 0,
                1 if contains_artifacts else 0,
                1 if contains_persistent else 0,
                status,
                error,
                created_at or iso(utcnow()),
            ),
        )

    def update(
        self,
        backup_id: int,
        *,
        size_bytes: int | None = None,
        sha256: str | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if size_bytes is not None:
            sets.append("size_bytes = ?")
            params.append(size_bytes)
        if sha256 is not None:
            sets.append("sha256 = ?")
            params.append(sha256)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if not sets:
            return False
        params.append(backup_id)
        return self.db.execute_rowcount(f"UPDATE backups SET {', '.join(sets)} WHERE id = ?", params) > 0

    def get(self, backup_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM backups WHERE id = ?", (backup_id,))

    def get_by_filename(self, filename: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM backups WHERE filename = ?", (filename,))

    def list_all(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM backups ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM backups", default=0) or 0)

    def delete(self, backup_id: int) -> bool:
        return self.db.execute_rowcount("DELETE FROM backups WHERE id = ?", (backup_id,)) > 0

    def prune_older_than(self, keep_count: int) -> list[dict[str, Any]]:
        """Find backups older than the newest `keep_count` items, delete them and return them."""
        if keep_count <= 0:
            return []
        stale_rows = self.db.query(
            """
            SELECT * FROM backups
            WHERE id NOT IN (SELECT id FROM backups ORDER BY id DESC LIMIT ?)
            ORDER BY id ASC
            """,
            (keep_count,),
        )
        if not stale_rows:
            return []
        ids = [row["id"] for row in stale_rows]
        placeholders = ",".join("?" for _ in ids)
        self.db.execute_rowcount(f"DELETE FROM backups WHERE id IN ({placeholders})", ids)
        return stale_rows


# ---------------------------------------------------------------------------
# Host Metrics (Lightweight Time Series)
# ---------------------------------------------------------------------------

class MetricsRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def record(self, snapshot: dict[str, Any]) -> int:
        return self.db.execute(
            """
            INSERT INTO host_metrics (
                ts, cpu_percent, memory_percent,
                disk_read_rate, disk_write_rate,
                net_rx_rate, net_tx_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.get("timestamp") or iso(utcnow()),
                float(snapshot.get("cpu_percent") or 0.0),
                float(snapshot.get("memory_percent") or 0.0),
                float(snapshot.get("disk_read_rate") or 0.0),
                float(snapshot.get("disk_write_rate") or 0.0),
                float(snapshot.get("net_rx_rate") or 0.0),
                float(snapshot.get("net_tx_rate") or 0.0),
            ),
        )

    def query_range(self, since_iso: str, limit: int = 300) -> list[dict[str, Any]]:
        """Query host metrics since a given timestamp, ordered chronologically.

        The window is capped at its *newest* ``limit`` rows: ordering ascending
        before the LIMIT would return the oldest samples of a 7-day window and
        silently hide everything after the first few hours.
        """
        rows = self.db.query(
            """
            SELECT ts AS timestamp, cpu_percent, memory_percent,
                   disk_read_rate, disk_write_rate, net_rx_rate, net_tx_rate
            FROM host_metrics
            WHERE ts >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (since_iso, limit),
        )
        rows.reverse()
        return rows

    def prune_older_than(self, days: int = 7) -> int:
        """Purge time series samples older than N days."""
        from datetime import timedelta
        cutoff = iso(utcnow() - timedelta(days=days))
        return self.db.execute_rowcount("DELETE FROM host_metrics WHERE ts < ?", (cutoff,))


class ContainerMetricsRepository:
    """Per-container time series, sampled on every docker stats refresh."""

    def __init__(self, database: Database) -> None:
        self.db = database

    def record_many(self, samples: list[dict[str, Any]]) -> None:
        if not samples:
            return
        self.db.execute_many(
            """
            INSERT INTO container_metrics (
                ts, container_id, container_name, compose_project,
                cpu_percent, memory_bytes, memory_percent,
                disk_read_rate, disk_write_rate, net_rx_rate, net_tx_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.get("timestamp") or iso(utcnow()),
                    s.get("container_id") or "",
                    s.get("container_name") or "",
                    s.get("compose_project") or "",
                    float(s.get("cpu_percent") or 0.0),
                    float(s.get("memory_bytes") or 0.0),
                    float(s.get("memory_percent") or 0.0),
                    float(s.get("disk_read_rate") or 0.0),
                    float(s.get("disk_write_rate") or 0.0),
                    float(s.get("net_rx_rate") or 0.0),
                    float(s.get("net_tx_rate") or 0.0),
                )
                for s in samples
            ],
        )

    def query_bucketed(self, since_iso: str, bucket_seconds: int) -> list[dict[str, Any]]:
        """Average per-container samples into fixed buckets aligned to the epoch.

        Buckets are computed in SQL so every container shares identical
        timestamps; the chart crosshair looks points up by timestamp.
        """
        bucket = max(1, int(bucket_seconds))
        rows = self.db.query(
            """
            SELECT CAST(strftime('%s', ts) / ? AS INTEGER) * ? AS bucket_ts,
                   container_id,
                   MAX(container_name)         AS container_name,
                   MAX(compose_project)        AS compose_project,
                   AVG(cpu_percent)            AS cpu_percent,
                   AVG(memory_bytes)           AS memory_bytes,
                   AVG(memory_percent)         AS memory_percent,
                   AVG(disk_read_rate)         AS disk_read_rate,
                   AVG(disk_write_rate)        AS disk_write_rate,
                   AVG(net_rx_rate)            AS net_rx_rate,
                   AVG(net_tx_rate)            AS net_tx_rate
            FROM container_metrics
            WHERE ts >= ?
            GROUP BY bucket_ts, container_id
            ORDER BY bucket_ts ASC
            """,
            (bucket, bucket, since_iso),
        )
        from datetime import datetime, timezone

        for row in rows:
            row["timestamp"] = datetime.fromtimestamp(
                int(row.pop("bucket_ts")), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            for key in (
                "cpu_percent", "memory_percent", "disk_read_rate",
                "disk_write_rate", "net_rx_rate", "net_tx_rate",
            ):
                row[key] = round(float(row.get(key) or 0.0), 2)
            row["memory_bytes"] = int(float(row.get("memory_bytes") or 0))
        return rows

    def prune_older_than(self, days: int = 7) -> int:
        from datetime import timedelta
        cutoff = iso(utcnow() - timedelta(days=days))
        return self.db.execute_rowcount("DELETE FROM container_metrics WHERE ts < ?", (cutoff,))


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
        self.hosts = HostRepository(self.db)
        self.tasks = TaskRepository(self.db)
        self.runs = RunRepository(self.db)
        self.audit = AuditRepository(self.db)
        self.backups = BackupRepository(self.db)
        self.metrics = MetricsRepository(self.db)
        self.container_metrics = ContainerMetricsRepository(self.db)

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
