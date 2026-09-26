"""POST /inspection-route 的端到端测试。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """四层小树（电阻 / 额定电流不参与巡检计算）：

    source --s1--> a --s2--> b
                   |
                   +--s3--> c --s4--> d

    巡检参数：s1 单程 5 分 / 风险 10，s2 单程 3 分 / 风险 6，
              s3 单程 4 分 / 风险 7，s4 单程 2 分 / 风险 5
    时限 20 分钟。可行连通子树：{s1} 10分/风险10、{s1,s2} 16分/风险16、
    {s1,s3} 18分/风险17；{s1,s2,s3} 24分、{s1,s3,s4} 22分、全树 28分均超时。
    """
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
            {"id": "s4", "parent": "c", "child": "d", "resistance": 0.1, "rated_current": 10.0},
        ],
        "segment_params": [
            {"segment_id": "s1", "travel_minutes": 5, "risk_score": 10.0},
            {"segment_id": "s2", "travel_minutes": 3, "risk_score": 6.0},
            {"segment_id": "s3", "travel_minutes": 4, "risk_score": 7.0},
            {"segment_id": "s4", "travel_minutes": 2, "risk_score": 5.0},
        ],
        "max_minutes": 20,
    }
    payload.update(overrides)
    return payload


# ---------- 择优与往返路径 ----------


def test_selects_highest_risk_feasible_subtree():
    resp = client.post("/inspection-route", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    # {s1,s3} 风险 17 最高且 18 分钟不超时限
    assert data["selected_segments"] == ["s1", "s3"]
    assert data["total_risk_score"] == pytest.approx(17.0)
    assert data["total_minutes"] == 18
    # 深度优先往返：a 的子节点按 segment_id 升序，s2 未入选，只走 s3
    assert data["path"] == ["source", "a", "c", "a", "source"]


def test_full_tree_when_time_allows():
    data = client.post("/inspection-route", json=base_payload(max_minutes=28)).json()
    assert data["selected_segments"] == ["s1", "s2", "s3", "s4"]
    assert data["total_risk_score"] == pytest.approx(28.0)
    assert data["total_minutes"] == 28
    # 子节点按 segment_id 升序：a 下先 s2(b) 后 s3(c)
    assert data["path"] == ["source", "a", "b", "a", "c", "d", "c", "a", "source"]


def test_ancestor_segments_included_for_deep_segment():
    # s4 风险 100：要巡检 s4 必须连带祖先段 s1、s3（22 分钟，恰在 22 分钟时限内）
    payload = base_payload(max_minutes=22)
    payload["segment_params"][3]["risk_score"] = 100.0
    data = client.post("/inspection-route", json=payload).json()
    assert data["selected_segments"] == ["s1", "s3", "s4"]
    assert data["total_risk_score"] == pytest.approx(117.0)
    assert data["total_minutes"] == 22
    assert data["path"] == ["source", "a", "c", "d", "c", "a", "source"]


def test_exact_time_limit_allowed():
    # {s1,s3} 恰好 18 分钟，恰好触限允许
    data = client.post("/inspection-route", json=base_payload(max_minutes=18)).json()
    assert data["selected_segments"] == ["s1", "s3"]
    assert data["total_minutes"] == 18


def test_one_minute_short_falls_back_to_cheaper_subtree():
    # 17 分钟装不下 {s1,s3}（18 分钟），退而选 {s1,s2}（16 分钟 / 风险 16）
    data = client.post("/inspection-route", json=base_payload(max_minutes=17)).json()
    assert data["selected_segments"] == ["s1", "s2"]
    assert data["total_risk_score"] == pytest.approx(16.0)
    assert data["total_minutes"] == 16
    assert data["path"] == ["source", "a", "b", "a", "source"]


def test_path_children_sorted_by_segment_id_not_input_order():
    # 打乱 segments 输入顺序，往返路径仍按 segment_id 升序访问子节点
    payload = base_payload(max_minutes=28)
    segs = payload["segments"]
    payload["segments"] = [segs[2], segs[0], segs[3], segs[1]]  # s3, s1, s4, s2
    data = client.post("/inspection-route", json=payload).json()
    assert data["selected_segments"] == ["s1", "s2", "s3", "s4"]
    assert data["path"] == ["source", "a", "b", "a", "c", "d", "c", "a", "source"]


def test_resistance_and_rated_current_do_not_affect_route():
    base = client.post("/inspection-route", json=base_payload()).json()
    payload = base_payload()
    for seg in payload["segments"]:
        seg["resistance"] = 9.9
        seg["rated_current"] = 0.5
    changed = client.post("/inspection-route", json=payload).json()
    assert changed == base


# ---------- 平局决胜 ----------


def test_tie_prefers_less_time():
    # {s1} 4 分钟 / 风险 5，{s2} 2 分钟 / 风险 5：同分选耗时更少的 {s2}
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s2", "parent": "source", "child": "b", "resistance": 0.1, "rated_current": 10.0},
        ],
        "segment_params": [
            {"segment_id": "s1", "travel_minutes": 2, "risk_score": 5.0},
            {"segment_id": "s2", "travel_minutes": 1, "risk_score": 5.0},
        ],
        "max_minutes": 4,
    }
    data = client.post("/inspection-route", json=payload).json()
    assert data["selected_segments"] == ["s2"]
    assert data["total_risk_score"] == pytest.approx(5.0)
    assert data["total_minutes"] == 2
    assert data["path"] == ["source", "b", "source"]


def test_tie_prefers_lexicographically_smaller_ids():
    # {s1} 与 {s2} 同为 2 分钟 / 风险 5：选排序后 id 列表字典序更小的 {s1}
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s2", "parent": "source", "child": "b", "resistance": 0.1, "rated_current": 10.0},
        ],
        "segment_params": [
            {"segment_id": "s1", "travel_minutes": 1, "risk_score": 5.0},
            {"segment_id": "s2", "travel_minutes": 1, "risk_score": 5.0},
        ],
        "max_minutes": 2,
    }
    data = client.post("/inspection-route", json=payload).json()
    assert data["selected_segments"] == ["s1"]
    assert data["total_minutes"] == 2
    assert data["path"] == ["source", "a", "source"]


def test_fractional_risk_scores_summed_and_rounded():
    # 0.1 + 0.2 的浮点和不精确，展示值按 6 位小数舍入为 0.3
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.1, "rated_current": 10.0},
        ],
        "segment_params": [
            {"segment_id": "s1", "travel_minutes": 1, "risk_score": 0.1},
            {"segment_id": "s2", "travel_minutes": 1, "risk_score": 0.2},
        ],
        "max_minutes": 4,
    }
    data = client.post("/inspection-route", json=payload).json()
    assert data["selected_segments"] == ["s1", "s2"]
    assert data["total_risk_score"] == pytest.approx(0.3)
    assert data["total_minutes"] == 4


# ---------- 空路线 ----------


def test_empty_route_when_time_insufficient():
    # 最便宜的 s1 往返也要 10 分钟，9 分钟时限只能空巡
    data = client.post("/inspection-route", json=base_payload(max_minutes=9)).json()
    assert data["selected_segments"] == []
    assert data["total_risk_score"] == pytest.approx(0.0)
    assert data["total_minutes"] == 0
    assert data["path"] == ["source"]


def test_zero_max_minutes_yields_empty_route():
    data = client.post("/inspection-route", json=base_payload(max_minutes=0)).json()
    assert data["selected_segments"] == []
    assert data["total_minutes"] == 0
    assert data["path"] == ["source"]


def test_negative_risk_scores_yield_empty_route():
    # 全部风险分为负：空路线（风险 0）优于任何非空子树
    payload = base_payload(max_minutes=28)
    for param in payload["segment_params"]:
        param["risk_score"] = -1.0
    data = client.post("/inspection-route", json=payload).json()
    assert data["selected_segments"] == []
    assert data["total_risk_score"] == pytest.approx(0.0)
    assert data["path"] == ["source"]


# ---------- 段数上限 ----------


def test_twelve_segments_accepted():
    segments = []
    params = []
    for i in range(12):
        segments.append(
            {
                "id": f"s{i + 1}",
                "parent": "source" if i == 0 else f"n{i}",
                "child": f"n{i + 1}",
                "resistance": 0.1,
                "rated_current": 10.0,
            }
        )
        params.append({"segment_id": f"s{i + 1}", "travel_minutes": 1, "risk_score": 1.0})
    payload = {"segments": segments, "segment_params": params, "max_minutes": 24}
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["selected_segments"] == sorted(f"s{i + 1}" for i in range(12))
    assert data["total_risk_score"] == pytest.approx(12.0)
    assert data["total_minutes"] == 24
    # 链式树的往返路径：source -> n1 -> ... -> n12 -> n11 -> ... -> n1 -> source
    assert len(data["path"]) == 25
    assert data["path"][0] == data["path"][-1] == "source"


def test_thirteen_segments_rejected():
    payload = base_payload()
    payload["segments"] = payload["segments"] + [
        {"id": f"x{i}", "parent": "d", "child": f"x{i}", "resistance": 0.1, "rated_current": 10.0}
        for i in range(9)
    ]
    payload["segment_params"] = payload["segment_params"] + [
        {"segment_id": f"x{i}", "travel_minutes": 1, "risk_score": 1.0} for i in range(9)
    ]
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 422
    assert any("最多包含 12 条" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_segments_rejected():
    resp = client.post("/inspection-route", json=base_payload(segments=[], segment_params=[]))
    assert resp.status_code == 422


# ---------- 巡检参数校验（400 invalid_input） ----------


def test_duplicate_param_rejected():
    payload = base_payload()
    payload["segment_params"].append(
        {"segment_id": "s1", "travel_minutes": 1, "risk_score": 1.0}
    )
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("重复定义" in d["message"] for d in error["details"])


def test_unknown_param_segment_rejected():
    payload = base_payload()
    payload["segment_params"].append(
        {"segment_id": "ghost", "travel_minutes": 1, "risk_score": 1.0}
    )
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在于树中" in d["message"] for d in error["details"])


def test_missing_param_rejected():
    payload = base_payload()
    payload["segment_params"] = [p for p in payload["segment_params"] if p["segment_id"] != "s4"]
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("s4" in d["message"] and "缺少巡检参数" in d["message"] for d in error["details"])


def test_empty_params_rejected():
    resp = client.post("/inspection-route", json=base_payload(segment_params=[]))
    assert resp.status_code == 422


# ---------- 字段校验（422 validation_error） ----------


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_travel_minutes_rejected(bad):
    payload = base_payload()
    payload["segment_params"][0]["travel_minutes"] = bad
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 422
    assert any("单程耗时必须为正整数" in d["message"] for d in resp.json()["error"]["details"])


def test_fractional_travel_minutes_rejected():
    payload = base_payload()
    payload["segment_params"][0]["travel_minutes"] = 1.5
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 422


def test_negative_max_minutes_rejected():
    resp = client.post("/inspection-route", json=base_payload(max_minutes=-1))
    assert resp.status_code == 422
    assert any("巡检时限不能为负" in d["message"] for d in resp.json()["error"]["details"])


def test_fractional_max_minutes_rejected():
    resp = client.post("/inspection-route", json=base_payload(max_minutes=2.5))
    assert resp.status_code == 422


def test_missing_max_minutes_rejected():
    payload = base_payload()
    del payload["max_minutes"]
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 422
    assert any("巡检时限" in d["message"] for d in resp.json()["error"]["details"])


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_risk_score_rejected(bad):
    # httpx 的 json= 不允许非有限浮点，改用原始请求体发送 NaN / Infinity 字面量
    payload = base_payload()
    body = json.dumps(payload).replace('"risk_score": 10.0', f'"risk_score": {bad}', 1)
    resp = client.post(
        "/inspection-route",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert any("风险分必须为有限数值" in d["message"] for d in resp.json()["error"]["details"])


# ---------- 复用树校验与自动生成线段 id ----------


def test_invalid_tree_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"id": "s9", "parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    payload["segment_params"].append(
        {"segment_id": "s9", "travel_minutes": 1, "risk_score": 1.0}
    )
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不连通" in d["message"] for d in error["details"])


def test_segment_ids_auto_generated():
    payload = {
        "segments": [
            {"parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
            {"parent": "a", "child": "b", "resistance": 0.1, "rated_current": 10.0},
        ],
        "segment_params": [
            {"segment_id": "seg-1", "travel_minutes": 1, "risk_score": 1.0},
            {"segment_id": "seg-2", "travel_minutes": 1, "risk_score": 2.0},
        ],
        "max_minutes": 4,
    }
    resp = client.post("/inspection-route", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["selected_segments"] == ["seg-1", "seg-2"]
    assert data["total_risk_score"] == pytest.approx(3.0)
    assert data["total_minutes"] == 4
    assert data["path"] == ["source", "a", "b", "a", "source"]


def test_index_lists_inspection_endpoint():
    info = client.get("/").json()
    assert info["inspection_route"] == "POST /inspection-route"
