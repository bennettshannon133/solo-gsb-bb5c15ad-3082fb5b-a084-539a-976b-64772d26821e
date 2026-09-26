"""POST /fault-sensor-placement 的端到端测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """三层小树（电阻 / 额定电流不参与测点部署计算）：

    source --s1--> a --s2--> b
                   |
                   +--s3--> c

    候选测点：a 成本 2，b 成本 3，c 成本 3。状态共 4 个：normal、s1、s2、s3。
    各候选读数（1=连通，0=失电）：

    - a：normal=1, s1=0, s2=1, s3=1（只能识别 s1）
    - b：normal=1, s1=0, s2=0, s3=1
    - c：normal=1, s1=0, s2=1, s3=0

    {b,c} 签名 11/00/01/10 两两不同（成本 6）；含 a 的任何子集要么无法区分
    （{a,b}、{a,c} 中 normal 分别与 s3、s2 同签名），要么成本更高（{a,b,c}=8）。
    最便宜的 a 是陷阱：最优解为 {b,c}。
    """
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
        ],
        "candidates": [
            {"node": "a", "cost": 2},
            {"node": "b", "cost": 3},
            {"node": "c", "cost": 3},
        ],
    }
    payload.update(overrides)
    return payload


def chain_payload(candidates):
    """三段链：source --s1--> a --s2--> b --s3--> c。"""
    return {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s3", "parent": "b", "child": "c", "resistance": 0.1, "rated_current": 10.0},
        ],
        "candidates": candidates,
    }


# ---------- 择优与签名 ----------


def test_selects_min_cost_sensor_set():
    resp = client.post("/fault-sensor-placement", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "solved"
    # 最便宜的 a（成本 2）无法区分 normal 与 s2/s3；最优为 {b,c}（成本 6）
    assert data["selected_nodes"] == ["b", "c"]
    assert data["total_cost"] == 6
    assert data["indistinguishable_pairs"] is None


def test_signatures_follow_selected_nodes_order():
    data = client.post("/fault-sensor-placement", json=base_payload()).json()
    # 状态顺序：normal 在前，随后按线段输入顺序；签名按 selected_nodes [b, c] 逐位对应
    assert data["signatures"] == [
        {"state": "normal", "signature": "11"},
        {"state": "s1", "signature": "00"},
        {"state": "s2", "signature": "01"},
        {"state": "s3", "signature": "10"},
    ]


def test_candidate_input_order_does_not_matter():
    payload = base_payload()
    payload["candidates"] = [
        {"node": "c", "cost": 3},
        {"node": "a", "cost": 2},
        {"node": "b", "cost": 3},
    ]
    data = client.post("/fault-sensor-placement", json=payload).json()
    assert data["selected_nodes"] == ["b", "c"]
    assert data["total_cost"] == 6
    assert [s["signature"] for s in data["signatures"]] == ["11", "00", "01", "10"]


def test_single_sensor_sufficient_for_single_segment():
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
        ],
        "candidates": [{"node": "a", "cost": 5}],
    }
    data = client.post("/fault-sensor-placement", json=payload).json()
    assert data["status"] == "solved"
    assert data["selected_nodes"] == ["a"]
    assert data["total_cost"] == 5
    assert data["signatures"] == [
        {"state": "normal", "signature": "1"},
        {"state": "s1", "signature": "0"},
    ]


def test_chain_requires_all_path_nodes():
    # 链上每个节点都是叶子或唯一子节点：a、b、c 全部必选
    payload = chain_payload(
        [{"node": "a", "cost": 1}, {"node": "b", "cost": 1}, {"node": "c", "cost": 1}]
    )
    data = client.post("/fault-sensor-placement", json=payload).json()
    assert data["status"] == "solved"
    assert data["selected_nodes"] == ["a", "b", "c"]
    assert data["total_cost"] == 3
    assert data["signatures"] == [
        {"state": "normal", "signature": "111"},
        {"state": "s1", "signature": "000"},
        {"state": "s2", "signature": "100"},
        {"state": "s3", "signature": "110"},
    ]


def test_equal_cost_leaves_all_selected():
    # 星形树：每个叶子测点都只能识别自己的线段，全部必选
    payload = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s2", "parent": "source", "child": "b", "resistance": 0.1, "rated_current": 10.0},
        ],
        "candidates": [{"node": "a", "cost": 1}, {"node": "b", "cost": 1}],
    }
    data = client.post("/fault-sensor-placement", json=payload).json()
    assert data["status"] == "solved"
    assert data["selected_nodes"] == ["a", "b"]
    assert data["total_cost"] == 2
    assert [s["signature"] for s in data["signatures"]] == ["11", "01", "10"]


def test_resistance_and_rated_current_do_not_affect_placement():
    base = client.post("/fault-sensor-placement", json=base_payload()).json()
    payload = base_payload()
    for seg in payload["segments"]:
        seg["resistance"] = 9.9
        seg["rated_current"] = 0.5
    changed = client.post("/fault-sensor-placement", json=payload).json()
    assert changed == base


# ---------- 无法区分 ----------


def test_indistinguishable_when_leaf_not_candidate():
    # 叶子 c 不在候选中：normal 与 s3（仅 c 失电）无法区分
    payload = chain_payload([{"node": "a", "cost": 1}, {"node": "b", "cost": 1}])
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "indistinguishable"
    assert data["selected_nodes"] is None
    assert data["total_cost"] is None
    assert data["signatures"] is None
    assert data["indistinguishable_pairs"] == [["normal", "s3"]]


def test_indistinguishable_when_single_child_node_not_candidate():
    # b 不在候选中：s2 与 s3 断开时候选 a、c 的读数相同（a=1,c=0）
    payload = chain_payload([{"node": "a", "cost": 1}, {"node": "c", "cost": 1}])
    data = client.post("/fault-sensor-placement", json=payload).json()
    assert data["status"] == "indistinguishable"
    assert data["indistinguishable_pairs"] == [["s2", "s3"]]


def test_indistinguishable_reports_all_colliding_pairs():
    # 仅候选 a：normal、s2、s3 两两签名相同（a 只在 s1 断开时失电）
    payload = chain_payload([{"node": "a", "cost": 1}])
    data = client.post("/fault-sensor-placement", json=payload).json()
    assert data["status"] == "indistinguishable"
    assert data["indistinguishable_pairs"] == [
        ["normal", "s2"],
        ["normal", "s3"],
        ["s2", "s3"],
    ]


# ---------- 数量上限 ----------


def test_twelve_segments_and_candidates_accepted():
    segments = []
    candidates = []
    for i in range(12):
        segments.append(
            {
                "id": f"s{i + 1}",
                "parent": "source",
                "child": f"n{i + 1:02d}",
                "resistance": 0.1,
                "rated_current": 10.0,
            }
        )
        candidates.append({"node": f"n{i + 1:02d}", "cost": 1})
    resp = client.post("/fault-sensor-placement", json={"segments": segments, "candidates": candidates})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "solved"
    # 星形树：每个叶子测点必选
    assert data["selected_nodes"] == [f"n{i + 1:02d}" for i in range(12)]
    assert data["total_cost"] == 12
    signatures = data["signatures"]
    assert signatures[0] == {"state": "normal", "signature": "1" * 12}
    # 断开 s3 时仅 n03 失电（签名第 3 位为 0）
    assert signatures[3] == {"state": "s3", "signature": "110111111111"}


def test_thirteen_segments_rejected():
    payload = base_payload()
    payload["segments"] = payload["segments"] + [
        {"id": f"x{i}", "parent": "c", "child": f"x{i}", "resistance": 0.1, "rated_current": 10.0}
        for i in range(10)
    ]
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 422
    assert any("最多包含 12 条" in d["message"] for d in resp.json()["error"]["details"])


def test_thirteen_candidates_rejected():
    payload = base_payload()
    payload["candidates"] = [{"node": f"ghost{i}", "cost": 1} for i in range(13)]
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 422
    assert any("最多包含 12 条" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_candidates_rejected():
    resp = client.post("/fault-sensor-placement", json=base_payload(candidates=[]))
    assert resp.status_code == 422


def test_empty_segments_rejected():
    resp = client.post("/fault-sensor-placement", json=base_payload(segments=[]))
    assert resp.status_code == 422


# ---------- 候选测点校验（400 invalid_input） ----------


def test_duplicate_candidate_rejected():
    payload = base_payload()
    payload["candidates"].append({"node": "a", "cost": 1})
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("重复" in d["message"] for d in error["details"])


def test_unknown_candidate_node_rejected():
    payload = base_payload()
    payload["candidates"].append({"node": "ghost", "cost": 1})
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不存在于树中" in d["message"] for d in error["details"])


def test_source_candidate_rejected():
    payload = base_payload()
    payload["candidates"].append({"node": "source", "cost": 1})
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("不能为根节点" in d["message"] for d in error["details"])


# ---------- 字段校验（422 validation_error） ----------


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_cost_rejected(bad):
    payload = base_payload()
    payload["candidates"][0]["cost"] = bad
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 422
    assert any("安装成本必须为正整数" in d["message"] for d in resp.json()["error"]["details"])


def test_fractional_cost_rejected():
    payload = base_payload()
    payload["candidates"][0]["cost"] = 1.5
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 422


def test_missing_cost_rejected():
    payload = base_payload()
    del payload["candidates"][0]["cost"]
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 422
    assert any("安装成本" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_candidate_node_rejected():
    payload = base_payload()
    payload["candidates"][0]["node"] = ""
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 422


# ---------- 复用树校验与自动生成线段 id ----------


def test_invalid_tree_rejected():
    payload = base_payload()
    payload["segments"].append(
        {"id": "s9", "parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/fault-sensor-placement", json=payload)
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
        "candidates": [{"node": "a", "cost": 1}, {"node": "b", "cost": 1}],
    }
    resp = client.post("/fault-sensor-placement", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "solved"
    assert data["selected_nodes"] == ["a", "b"]
    assert data["signatures"] == [
        {"state": "normal", "signature": "11"},
        {"state": "seg-1", "signature": "00"},
        {"state": "seg-2", "signature": "10"},
    ]


def test_index_lists_fault_sensor_placement_endpoint():
    info = client.get("/").json()
    assert info["fault_sensor_placement"] == "POST /fault-sensor-placement"
