"""HTTP 层：可评范围与排除范围可见、可导出、可落回原始行号；回退显式标注。"""
from __future__ import annotations

import csv
import io
import json

from app import config
from fixtures import dirty_cases


def _upload(client, name: str, text: str):
    return client.post("/api/score/upload",
                       files={"file": (name, text.encode("utf-8"), "text/csv")})


def test_score_api_reports_assessable_scope(client):
    body = client.post("/api/score", json={"sample": "tickets_mixed_notation.csv"}).json()
    assessable = body["assessable"]
    assert assessable["cells_total"] == body["counts"]["cells_total"]
    assert assessable["cells_notation_unknown"] == 2
    assert assessable["cells_assessable"] == assessable["cells_total"] - 2
    assert assessable["notation_policy"] == config.NOTATION_POLICY
    assert body["notation"]["count"] == 2


def test_assessable_csv_states_scope_and_rule(client_with_run):
    response = client_with_run.get("/api/export/assessable.csv")
    assert response.status_code == 200
    text = response.content.decode("utf-8")
    assert "可评单元格" in text
    assert "排除·写法未定" in text
    assert config.NOTATION_POLICY in text


def test_notation_csv_traces_every_excluded_cell(client):
    client.post("/api/score", json={"sample": "tickets_mixed_notation.csv"})
    text = client.get("/api/export/notation.csv").content.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    header, data = rows[0], rows[1:]
    assert header == ["列名", "列标题", "行号", "原值", "原因码", "说明"]
    assert len(data) == 2
    for row in data:
        assert row[0] == "first_response_min"
        assert row[4] == "K_NOTATION_UNKNOWN"
        assert row[2].isdigit()
    assert {row[3] for row in data} == {"1,204", "1.204"}
    assert any("读作" in row[5] for row in data)


def test_report_page_lists_excluded_cells_with_linenos(client):
    client.post("/api/score", json={"sample": "tickets_mixed_notation.csv"})
    html = client.get("/").text
    assert "本批判定范围" in html
    assert "写法未定" in html
    assert "1,204" in html and "1.204" in html
    assert "K_NOTATION_UNKNOWN" in html


def test_report_csv_states_scope(client_with_run):
    text = client_with_run.get("/api/export/report.csv").content.decode("utf-8")
    assert "可评单元格" in text
    assert "写法未定单元格" in text
    assert config.NOTATION_POLICY in text


def test_manifest_json_carries_notation_policy_and_cells(client):
    client.post("/api/score", json={"sample": "tickets_mixed_notation.csv"})
    payload = client.get("/api/export/manifest.json").json()
    assert payload["notation"]["policy"] == config.NOTATION_POLICY
    assert payload["notation"]["count"] == 2
    assert payload["assessable"]["cells_notation_unknown"] == 2
    assert [item["raw"] for item in payload["notation"]["cells"]] == ["1,204", "1.204"]


def test_corrupt_snapshot_falls_back_over_http(client):
    first = client.post("/api/score", json={"sample": "tickets_dirty.csv"}).json()
    second = _upload(client, "tickets_dirty.csv",
                     dirty_cases.frame(dirty_cases.count_rows(6))).json()
    corrupt = second["run"]["run_id"]
    store = client.app.state.store
    with store.connect() as conn:
        conn.execute("UPDATE runs SET summary_json='{broken' WHERE run_id=?",
                     (corrupt,))

    page = client.get("/")
    assert page.status_code == 200
    assert "已回退到完整旧版" in page.text
    assert first["score"] in page.text

    latest = client.get("/api/score/latest").json()
    assert latest["run"]["run_id"] == first["run"]["run_id"]
    assert latest["fallback"]["corrupt_run_id"] == corrupt

    export = client.get("/api/export/manifest.json")
    assert export.status_code == 200
    payload = json.loads(export.content)
    assert payload["run_id"] == first["run"]["run_id"]
    assert payload["fallback"]["corrupt_run_id"] == corrupt
    assert store.count_runs() == 2


def test_retry_over_http_does_not_duplicate(client):
    text = dirty_cases.frame(dirty_cases.count_rows(7))
    first = _upload(client, "same.csv", text).json()
    for _ in range(3):
        again = _upload(client, "same.csv", text).json()
        assert again["replay"] is True
        assert again["run"]["run_id"] == first["run"]["run_id"]
    assert client.app.state.store.count_runs() == 1
