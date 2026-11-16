"""运行可靠性：同输入重试不重复计分；写入失败整体回滚；快照损坏回退旧版。

三条硬约束：
1. 同一附件重试（含同输入重跑、快照损坏后的修复重算）只保留一条运行记录，
   分数不变、记录数不增；
2. 新版本落库失败时数据集原文与运行记录一起回滚，旧版完整快照仍在；
3. 快照读取失败（sha256 不符）不返回半截报告，而是回退到最近一条完整旧版，
   坏记录保留未删，页面与导出显式标注回退来源。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import config
from app.errors import StorageFailure
from app.modules.m6_score import runstore, service
from app.store import Store, SnapshotError
from fixtures import dirty_cases, expected


def test_retry_same_upload_scores_once(settings, store):
    text = dirty_cases.frame(dirty_cases.count_rows(8))
    first = dirty_cases.run_text(settings, store, text, name="retry.csv")
    second = dirty_cases.run_text(settings, store, text, name="retry.csv")
    third = dirty_cases.run_text(settings, store, text, name="retry.csv")
    assert second["replay"] is True and third["replay"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert third["run"]["score"] == first["run"]["score"]
    assert store.count_runs() == 1
    assert store.latest_version("retry.csv") == 1


def test_counts_and_score_identical_after_retry(settings, store):
    first = service.run(settings, store, name="tickets_dirty.csv")
    second = service.run(settings, store, name="tickets_dirty.csv")
    for key in ("score", "grade", "rows_in", "rows_accepted", "rows_dup",
                "cells_total", "cells_missing", "cells_notation_unknown",
                "tested_cells"):
        assert first["manifest"]["counters"].get(key) == \
            second["manifest"]["counters"].get(key), key
    assert second["score"]["score"] == expected.DIRTY["score"]


def test_write_failure_rolls_back_and_keeps_old_version(settings, store, monkeypatch):
    first = dirty_cases.run_text(settings, store, dirty_cases.frame(
        dirty_cases.count_rows(6)), name="case.csv")
    old_dataset = store.get_dataset("case.csv")
    changed = dirty_cases.frame(dirty_cases.count_rows(6, agent=""))

    def boom(conn, run, summary):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Store, "_write_run", staticmethod(boom))
    with pytest.raises(StorageFailure) as info:
        dirty_cases.run_text(settings, store, changed, name="case.csv",
                             expected_version=1)
    assert info.value.code == "E_STORAGE"
    assert store.count_runs() == 1
    assert store.latest_version("case.csv") == 1
    # 数据集原文也没有被新版本覆盖：事务一起回滚
    after = store.get_dataset("case.csv")
    assert after["digest"] == old_dataset["digest"]
    assert after["content"] == old_dataset["content"]
    # 旧版快照仍可完整读取
    intact = store.load_result(first["run"]["run_id"])
    assert intact["run"]["score"] == first["run"]["score"]


def test_corrupt_snapshot_falls_back_to_previous_version(settings, store):
    first = dirty_cases.run_text(settings, store, dirty_cases.frame(
        dirty_cases.count_rows(6)), name="case.csv")
    second = dirty_cases.run_text(settings, store, dirty_cases.frame(
        dirty_cases.count_rows(6, agent="")), name="case.csv",
        expected_version=1)
    corrupt = second["run"]["run_id"]
    with store.connect() as conn:
        conn.execute("UPDATE runs SET summary_json='{broken' WHERE run_id=?",
                     (corrupt,))
    with pytest.raises(SnapshotError):
        store.load_result(corrupt)

    run, result, fallback = runstore.load(store, corrupt)
    assert run["run_id"] == first["run"]["run_id"]
    assert result["run"]["run_id"] == first["run"]["run_id"]
    assert result["score"]["score"] == first["run"]["score"]
    assert fallback["corrupt_run_id"] == corrupt
    assert fallback["fallback_run_id"] == first["run"]["run_id"]
    # 两条运行记录都还在，坏记录没有被删掉
    assert store.count_runs() == 2
    assert store.get_run(corrupt) is not None


def test_digest_mismatch_also_falls_back(settings, store):
    first = dirty_cases.run_text(settings, store, dirty_cases.frame(
        dirty_cases.count_rows(5)), name="case.csv")
    second = dirty_cases.run_text(settings, store, dirty_cases.frame(
        dirty_cases.count_rows(5, agent="")), name="case.csv",
        expected_version=1)
    # 只改 sha256，不改 JSON：内容仍可解析，但完整性校验必须拦住
    with store.connect() as conn:
        conn.execute("UPDATE runs SET snapshot_sha256=? WHERE run_id=?",
                     ("0" * 64, second["run"]["run_id"]))
    run, result, fallback = runstore.load(store, second["run"]["run_id"])
    assert run["run_id"] == first["run"]["run_id"]
    assert fallback["reason"] == "sha256 不匹配"


def test_latest_run_with_corrupt_snapshot_falls_back(settings, store):
    first = dirty_cases.run_text(settings, store, dirty_cases.frame(
        dirty_cases.count_rows(6)), name="a.csv")
    second = dirty_cases.run_text(settings, store, dirty_cases.frame(
        dirty_cases.count_rows(6)), name="b.csv")
    with store.connect() as conn:
        conn.execute("UPDATE runs SET summary_json='[]' WHERE run_id=?",
                     (second["run"]["run_id"],))
    run, result, fallback = runstore.load(store, None)
    assert run["run_id"] == first["run"]["run_id"]
    assert fallback["corrupt_run_id"] == second["run"]["run_id"]


def test_retry_repairs_corrupt_snapshot_without_new_record(settings, store):
    text = dirty_cases.frame(dirty_cases.count_rows(8))
    first = dirty_cases.run_text(settings, store, text, name="retry.csv")
    with store.connect() as conn:
        conn.execute("UPDATE runs SET summary_json='{broken' WHERE run_id=?",
                     (first["run"]["run_id"],))
    repaired = dirty_cases.run_text(settings, store, text, name="retry.csv")
    assert repaired.get("repaired") is True
    assert repaired["run"]["run_id"] == first["run"]["run_id"]
    assert repaired["run"]["version"] == first["run"]["version"]
    assert repaired["run"]["score"] == first["run"]["score"]
    assert store.count_runs() == 1
    # 修复后快照重新可读
    assert store.load_result(first["run"]["run_id"])["run"]["score"] == \
        first["run"]["score"]


def test_old_database_is_migrated_with_digests(tmp_path, settings):
    """老库没有 snapshot_sha256 列：升级后补算摘要，不把历史记录判成损坏。"""
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL,
            rules_version TEXT NOT NULL, version INTEGER NOT NULL,
            score TEXT NOT NULL, grade TEXT NOT NULL, rows_in INTEGER NOT NULL,
            rows_accepted INTEGER NOT NULL, rows_unique INTEGER NOT NULL,
            rows_dup INTEGER NOT NULL, rows_quarantine INTEGER NOT NULL,
            rows_blank INTEGER NOT NULL, cells_total INTEGER NOT NULL,
            cells_missing INTEGER NOT NULL, created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            seen_seq INTEGER NOT NULL, summary_json TEXT NOT NULL);
        CREATE TABLE datasets (
            name TEXT PRIMARY KEY, digest TEXT NOT NULL, encoding TEXT NOT NULL,
            declared_encoding TEXT, bytes INTEGER NOT NULL, content BLOB NOT NULL,
            updated_at TEXT NOT NULL);
    """)
    payload = {"run": {"run_id": "QS-legacy", "name": "legacy.csv",
                       "digest": "d", "rules_version": config.RULE_VERSION,
                       "version": 1, "score": "90.00", "grade": "A",
                       "rows_in": 1, "rows_accepted": 1, "rows_unique": 1,
                       "rows_dup": 0, "rows_quarantine": 0, "rows_blank": 0,
                       "cells_total": 10, "cells_missing": 0},
               "score": {"score": "90.00"}}
    conn.execute(
        """INSERT INTO runs VALUES ('QS-legacy','legacy.csv','d',?,1,'90.00','A',
           1,1,1,0,0,0,10,0,'t','t','t',1,?)""",
        (config.RULE_VERSION, json.dumps(payload)))
    conn.commit()
    conn.close()

    store = Store(str(path))
    loaded = store.load_result("QS-legacy")
    assert loaded["run"]["run_id"] == "QS-legacy"
    with store.connect() as check:
        row = check.execute(
            "SELECT snapshot_sha256 FROM runs WHERE run_id='QS-legacy'").fetchone()
    assert len(row["snapshot_sha256"]) == 64
