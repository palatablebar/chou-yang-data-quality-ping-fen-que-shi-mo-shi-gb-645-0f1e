"""运行记录写入与版本闸门：幂等复用、版本自增、陈旧提交拒绝、快照回退。

同名数据集每次内容变化就是一次新版本；客户端手里的版本比库里旧，
说明它读到的是过期页面，直接 409，不允许旧页面把新结果覆盖掉。

写入是原子的：数据集原文与运行记录同一事务提交，任一步失败整体回滚，
旧版本原样保留。读取是逐条校验的：最新快照损坏时回退到最近一条完整旧版，
并在返回里标明回退来源；没有任何完整快照才报错，绝不把坏快照当空报告。
"""
from __future__ import annotations

from decimal import Decimal

import datetime as dt
import sqlite3

from ... import config
from ...errors import Conflict, NotFound, StorageFailure
from ...store import SnapshotError


def stale_check(store, name: str, expected_version: int | None) -> int:
    latest = store.latest_version(name)
    if expected_version is None:
        return latest
    if expected_version < latest:
        raise Conflict(
            f"提交版本 {expected_version} 落后于当前版本 {latest}", code="E_STALE_RUN",
            detail={"expected_version": expected_version, "latest_version": latest})
    return latest


def replay(store, existing: dict) -> dict:
    """同输入重跑：刷新最近查看并返回原快照，不新增记录、不重复计分。"""
    store.touch_run(existing["run_id"], _now())
    try:
        payload = store.load_result(existing["run_id"])
    except SnapshotError:
        return None
    if payload is None:
        return None
    payload["replay"] = True
    payload["run"] = _run_row(existing)
    return payload


def repair(store, run_id: str, result: dict) -> None:
    """快照损坏时的同记录修复：原 run_id / 版本不变，只重写快照与摘要。"""
    store.replace_snapshot(run_id, result)


def require_run(store, run_id: str | None) -> dict:
    if not run_id:
        latest = store.latest_run()
        if latest is None:
            raise NotFound("还没有运行记录", code="E_RUN_NOT_FOUND")
        return latest
    run = store.get_run(run_id)
    if run is None:
        raise NotFound(f"运行记录不存在：{run_id}", code="E_RUN_NOT_FOUND")
    return run


def load(store, run_id: str | None = None, *, name: str | None = None
         ) -> tuple[dict | None, dict | None, dict | None]:
    """读一条运行记录与完整快照；最新快照损坏时回退到完整旧版。

    返回 (run, result, fallback)。fallback 非空时说明请求的那条读不出来，
    实际展示的是哪条旧版、原因是什么——页面与导出据此显式标注。
    """
    if run_id:
        run = require_run(store, run_id)
    else:
        run = store.latest_run()
        if run is None:
            return None, None, None
    try:
        return run, store.load_result(run["run_id"]), None
    except SnapshotError as exc:
        older = store.latest_intact_run(name=run["name"], exclude=run["run_id"]) \
            or store.latest_intact_run(exclude=run["run_id"])
        if older is None:
            raise
        return older, store.load_result(older["run_id"]), {
            "corrupt_run_id": run["run_id"],
            "corrupt_version": int(run["version"]),
            "reason": exc.reason,
            "fallback_run_id": older["run_id"],
            "fallback_version": int(older["version"]),
            "note": f"最新快照 {run['run_id']} 读取失败，已回退到完整旧版 "
                    f"{older['run_id']}（v{int(older['version'])}）；运行记录未删除",
        }


def _run_row(run: dict) -> dict:
    return {
        "run_id": run["run_id"], "name": run["name"], "digest": run["digest"],
        "rules_version": run["rules_version"], "version": int(run["version"]),
        "score": str(run["score"]), "grade": run["grade"],
        "rows_in": int(run["rows_in"]), "rows_accepted": int(run["rows_accepted"]),
        "rows_unique": int(run["rows_unique"]), "rows_dup": int(run["rows_dup"]),
        "rows_quarantine": int(run["rows_quarantine"]),
        "rows_blank": int(run["rows_blank"]),
        "cells_total": int(run["cells_total"]),
        "cells_missing": int(run["cells_missing"]),
        "created_at": run["created_at"], "finished_at": run["finished_at"],
    }


def persist(store, result: dict, dataset: dict) -> dict:
    """新版本落库：数据集原文与运行记录一个事务，失败不留半截状态。"""
    run = result["run"]
    try:
        store.commit_version(dataset=dataset, run={
            "run_id": run["run_id"], "name": run["name"], "digest": run["digest"],
            "rules_version": run["rules_version"], "version": run["version"],
            "score": Decimal(run["score"]), "grade": run["grade"],
            "rows_in": run["rows_in"], "rows_accepted": run["rows_accepted"],
            "rows_unique": run["rows_unique"], "rows_dup": run["rows_dup"],
            "rows_quarantine": run["rows_quarantine"], "rows_blank": run["rows_blank"],
            "cells_total": run["cells_total"], "cells_missing": run["cells_missing"],
            "created_at": run["created_at"], "finished_at": run["finished_at"],
        }, summary=result)
    except sqlite3.Error as exc:
        raise StorageFailure(
            f"运行记录写入失败，已整体回滚，旧版本保持完整：{exc}",
            code="E_STORAGE",
            detail={"run_id": run["run_id"], "version": run["version"]}) from exc
    return result


def summary(run: dict) -> dict:
    return _run_row(run)


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def thresholds() -> dict:
    return {item["key"]: {"cap": item["cap"], "tolerance": item["tolerance"]}
            for item in config.WEIGHTS}
