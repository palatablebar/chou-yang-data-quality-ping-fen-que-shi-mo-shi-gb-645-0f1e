"""SQLite 存储：数据集原文、运行记录与完整结果快照。

约定：
- 数据集按名字唯一：同名文件重新采信即覆盖原文，历史运行仍按 digest 保留；
- 运行记录按 (dataset, digest, rules_version) 幂等：同输入重跑不新增行；
- 新版本一次性提交：数据集原文与运行记录在同一个事务里落库，任何一步失败
  都整体回滚，旧版本的数据集与完整快照原样保留；
- 快照带 sha256：读取时逐条校验，最新一条读坏就回退到最近一条完整旧版，
  不把损坏的快照当成空结果，也不删任何运行记录；
- 分数与比率落 TEXT（Decimal 字符串），不落 float；
- 页面永远只展示一条运行记录，导出读的是同一条快照。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import NotFound


def _next_seq(conn) -> int:
    """下一查看序号：同一事务内取 MAX+1，运行记录之间不共享状态。

    注意它是独立函数而不是方法：事务里连着写数据集与运行记录时，
    若走 self.connect() 会开第二条连接，拿不到未提交的序号。
    """
    row = conn.execute("SELECT COALESCE(MAX(seen_seq), 0) + 1 AS n FROM runs").fetchone()
    return int(row["n"])


SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    encoding TEXT NOT NULL,
    declared_encoding TEXT,
    bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    version INTEGER NOT NULL,
    score TEXT NOT NULL,
    grade TEXT NOT NULL,
    rows_in INTEGER NOT NULL,
    rows_accepted INTEGER NOT NULL,
    rows_unique INTEGER NOT NULL,
    rows_dup INTEGER NOT NULL,
    rows_quarantine INTEGER NOT NULL,
    rows_blank INTEGER NOT NULL,
    cells_total INTEGER NOT NULL,
    cells_missing INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity ON runs(name, digest, rules_version);
CREATE INDEX IF NOT EXISTS idx_runs_version ON runs(name, version DESC);
"""

MIGRATIONS = (
    ("runs", "snapshot_sha256", "ALTER TABLE runs ADD COLUMN snapshot_sha256 TEXT NOT NULL DEFAULT ''"),
)


class SnapshotError(RuntimeError):
    """快照读取失败：JSON 损坏或 sha256 与落库时不一致。"""

    def __init__(self, run_id: str, reason: str):
        super().__init__(f"运行快照损坏：{run_id}（{reason}）")
        self.run_id = run_id
        self.reason = reason


def snapshot_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()



class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._backfill_digests(conn)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self, conn) -> None:
        for table, column, statement in MIGRATIONS:
            names = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in names:
                conn.execute(statement)

    def _backfill_digests(self, conn) -> None:
        """老库升级：已有快照补算 sha256，避免升级后全部判成损坏。"""
        rows = conn.execute(
            "SELECT run_id, summary_json FROM runs WHERE snapshot_sha256=''").fetchall()
        for row in rows:
            conn.execute("UPDATE runs SET snapshot_sha256=? WHERE run_id=?",
                         (snapshot_digest(row["summary_json"]), row["run_id"]))

    # ---------- 数据集 ----------
    def save_dataset(self, *, name: str, digest: str, encoding: str,
                     declared_encoding: str | None, data: bytes, stamp: str) -> None:
        """存原始字节：GB18030 文件重算摘要必须与首次采信逐位相同。"""
        with self.connect() as conn:
            self._write_dataset(conn, name=name, digest=digest, encoding=encoding,
                                declared_encoding=declared_encoding, data=data,
                                stamp=stamp)

    @staticmethod
    def _write_dataset(conn, *, name: str, digest: str, encoding: str,
                       declared_encoding: str | None, data: bytes, stamp: str) -> None:
        conn.execute(
            """INSERT INTO datasets (name,digest,encoding,declared_encoding,bytes,
                    content,updated_at) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 digest=excluded.digest, encoding=excluded.encoding,
                 declared_encoding=excluded.declared_encoding,
                 bytes=excluded.bytes, content=excluded.content,
                 updated_at=excluded.updated_at""",
            (name, digest, encoding, declared_encoding, len(data), data, stamp))

    def get_dataset(self, name: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def list_datasets(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name,digest,encoding,bytes,updated_at FROM datasets ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 运行记录 ----------
    def latest_version(self, name: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM runs WHERE name=?", (name,)).fetchone()
        return int(row["v"] or 0)

    def find_run(self, name: str, digest: str, rules_version: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE name=? AND digest=? AND rules_version=?",
                (name, digest, rules_version)).fetchone()
        return dict(row) if row else None

    def save_run(self, run: dict, summary: dict) -> None:
        with self.connect() as conn:
            self._write_run(conn, run, summary)

    @staticmethod
    def _write_run(conn, run: dict, summary: dict) -> None:
        text = json.dumps(summary, ensure_ascii=False)
        conn.execute(
            """INSERT INTO runs (run_id,name,digest,rules_version,version,score,grade,
                    rows_in,rows_accepted,rows_unique,rows_dup,rows_quarantine,
                    rows_blank,cells_total,cells_missing,created_at,finished_at,
                    last_seen_at,seen_seq,summary_json,snapshot_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run["run_id"], run["name"], run["digest"], run["rules_version"],
             int(run["version"]), str(run["score"]), run["grade"],
             int(run["rows_in"]), int(run["rows_accepted"]), int(run["rows_unique"]),
             int(run["rows_dup"]), int(run["rows_quarantine"]),
             int(run["rows_blank"]), int(run["cells_total"]),
             int(run["cells_missing"]), run["created_at"], run["finished_at"],
             run["created_at"], _next_seq(conn), text, snapshot_digest(text)))

    def commit_version(self, *, dataset: dict, run: dict, summary: dict) -> None:
        """新版本提交：数据集原文与运行记录同一事务，失败则整体不落。

        数据集与运行记录要么一起出现，要么都保持旧版；不会出现「原文换成
        新版、运行记录还停在旧版」的半截状态。
        """
        with self.connect() as conn:
            self._write_dataset(conn, name=dataset["name"], digest=dataset["digest"],
                                encoding=dataset["encoding"],
                                declared_encoding=dataset.get("declared_encoding"),
                                data=dataset["data"], stamp=dataset["stamp"])
            self._write_run(conn, run, summary)

    def touch_dataset(self, dataset: dict) -> None:
        """幂等重试只刷新数据集原文与时间，不新增运行记录。"""
        self.save_dataset(name=dataset["name"], digest=dataset["digest"],
                          encoding=dataset["encoding"],
                          declared_encoding=dataset.get("declared_encoding"),
                          data=dataset["data"], stamp=dataset["stamp"])

    def touch_run(self, run_id: str, stamp: str) -> None:
        """命中幂等复用时刷新「最近一次查看」，导航回各页仍停在这条记录上。"""
        with self.connect() as conn:
            conn.execute("UPDATE runs SET last_seen_at=?, seen_seq=? WHERE run_id=?",
                         (stamp, _next_seq(conn), run_id))

    def latest_run(self, name: str | None = None) -> dict | None:
        with self.connect() as conn:
            if name:
                row = conn.execute(
                    "SELECT * FROM runs WHERE name=? ORDER BY version DESC LIMIT 1",
                    (name,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def load_result(self, run_id: str) -> dict | None:
        """读快照：校验 sha256 后才返回；损坏时抛 SnapshotError，不回空。

        抛错而不是返回 None，是为了让调用方能区分「运行记录不存在」与
        「记录在但快照坏了」，后者必须回退到完整旧版而不是显示空报告。
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT summary_json, snapshot_sha256 FROM runs WHERE run_id=?",
                (run_id,)).fetchone()
        if row is None:
            return None
        text = row["summary_json"]
        expected = row["snapshot_sha256"] or snapshot_digest(text)
        if snapshot_digest(text) != expected:
            raise SnapshotError(run_id, "sha256 不匹配")
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 校验已先拦
            raise SnapshotError(run_id, f"JSON 解析失败：{exc}") from exc

    def latest_intact_run(self, *, name: str | None = None,
                          exclude: str | None = None) -> dict | None:
        """找最近一条快照完整的运行记录。

        给了数据集名就只看同一数据集，并按版本倒序（回退到「上一版」而不是
        「碰巧最近被查看的另一份数据」）；不给名字才跨数据集按查看顺序找。
        """
        with self.connect() as conn:
            if name:
                rows = conn.execute(
                    "SELECT run_id FROM runs WHERE name=? ORDER BY version DESC",
                    (name,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id FROM runs ORDER BY seen_seq DESC").fetchall()
        for row in rows:
            if exclude and row["run_id"] == exclude:
                continue
            try:
                if self.load_result(row["run_id"]) is not None:
                    return self.get_run(row["run_id"])
            except SnapshotError:
                continue
        return None

    def replace_snapshot(self, run_id: str, summary: dict) -> None:
        """同一 run_id 重写快照与摘要：记录数、版本与 run_id 都不变。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT name, digest, rules_version, version FROM runs WHERE run_id=?",
                (run_id,)).fetchone()
            if row is None:
                raise NotFound(f"运行记录不存在：{run_id}")
            text = json.dumps(summary, ensure_ascii=False)
            conn.execute(
                """UPDATE runs SET summary_json=?, snapshot_sha256=?,
                        score=?, grade=?, rows_in=?, rows_accepted=?,
                        rows_unique=?, rows_dup=?, rows_quarantine=?,
                        rows_blank=?, cells_total=?, cells_missing=?
                   WHERE run_id=?""",
                (text, snapshot_digest(text), str(summary["run"]["score"]),
                 summary["run"]["grade"], int(summary["run"]["rows_in"]),
                 int(summary["run"]["rows_accepted"]),
                 int(summary["run"]["rows_unique"]), int(summary["run"]["rows_dup"]),
                 int(summary["run"]["rows_quarantine"]),
                 int(summary["run"]["rows_blank"]),
                 int(summary["run"]["cells_total"]),
                 int(summary["run"]["cells_missing"]), run_id))

    def count_runs(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])
