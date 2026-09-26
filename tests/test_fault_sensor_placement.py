"""POST /fault-sensor-placement 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# source --s1--> a --s2--> b
#                 |
#                 +--s3--> c --s4--> d
#
# 断开 s1 全树失电；s2 仅 b 失电；s3 失 c、d；s4 仅 d 失电。
# 必选测点（叶节点 + 单链节点）：b（叶）、c（唯一子段 s4）、d（叶）；
# a 为分支节点（s2、s3 两个子段），可选但不必选。
SEGMENTS = [
    {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
    {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
    {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
    {"id": "s4", "parent": "c", "child": "d", "resistance": 0.1, "rated_current": 10.0},
]


def payload(**overrides):
    data = {
        "segments": SEGMENTS,
        "candidates": [
            {"node": "a", "cost": 1},
            {"node": "b", "cost": 5},
            {"node": "c", "cost": 3},
            {"node": "d", "cost": 4},
        ],
    }
    data.update(overrides)
    return data


# ---------- 可行：最低成本测点集合 ----------


def test_selects_mandatory_nodes_with_lowest_cost():
    resp = client.post("/fault-sensor-placement", json=payload())
    assert resp.status_code == 200
    data = resp.json()

    # 必选 b、c、d；分支节点 a 虽成本最低但不增加区分度，不入选
    assert data["feasible"] is True
    assert data["selected_nodes"] == ["b", "c", "d"]
    assert data["total_cost"] == 12
    assert data["indistinguishable_pairs"] is None


def test_signatures_cover_normal_and_each_single_segment_outage():
    data = client.post("/fault-sensor-placement", json=payload()).json()
    signatures = data["signatures"]

    # 正常状态在前，断线状态按线段输入顺序
    assert [(s["state"], s["segment_id"]) for s in signatures] == [
        ("normal", None),
        ("fault_segment", "s1"),
        ("fault_segment", "s2"),
        ("fault_segment", "s3"),
        ("fault_segment", "s4"),
    ]
    for s in signatures:
        assert s["nodes"] == ["b", "c", "d"]

    connected = {key: sig["connected"] for sig in signatures for key in [(sig["state"], sig["segment_id"])]}
    assert connected[("normal", None)] == [True, True, True]
    assert connected[("fault_segment", "s1")] == [False, False, False]
    assert connected[("fault_segment", "s2")] == [False, True, True]
    assert connected[("fault_segment", "s3")] == [True, False, False]
    assert connected[("fault_segment", "s4")] == [True, True, False]

    # 五个签名两两不同
    vectors = [tuple(sig["connected"]) for sig in signatures]
    assert len(set(vectors)) == len(vectors)


def test_single_segment_tree():
    data = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0}
        ],
        "candidates": [{"node": "a", "cost": 7}],
    }
    resp = client.post("/fault-sensor-placement", json=data)
    assert resp.status_code == 200
    result = resp.json()
    assert result["feasible"] is True
    assert result["selected_nodes"] == ["a"]
    assert result["total_cost"] == 7
    assert result["signatures"][0]["nodes"] == ["a"]
    assert result["signatures"][0]["connected"] == [True]
    assert result["signatures"][1]["segment_id"] == "s1"
    assert result["signatures"][1]["connected"] == [False]


def test_selected_nodes_sorted_regardless_of_input_order():
    p = payload(candidates=[{"node": n, "cost": c} for n, c in [("d", 4), ("a", 1), ("c", 3), ("b", 5)]])
    data = client.post("/fault-sensor-placement", json=p).json()
    assert data["selected_nodes"] == ["b", "c", "d"]
    assert all(sig["nodes"] == ["b", "c", "d"] for sig in data["signatures"])


def test_branching_node_with_cheapest_cost_not_selected():
    # 分支节点 a 成本再低也不入选：最小成本可行集唯一为必选节点集合
    p = payload(
        candidates=[
            {"node": "a", "cost": 1},
            {"node": "b", "cost": 100},
            {"node": "c", "cost": 100},
            {"node": "d", "cost": 100},
        ]
    )
    data = client.post("/fault-sensor-placement", json=p).json()
    assert data["feasible"] is True
    assert data["selected_nodes"] == ["b", "c", "d"]
    assert data["total_cost"] == 300


def test_extra_unneeded_candidates_ignored():
    # 候选含全部节点；结果与仅含必选节点时一致（分支节点 a 不入选）
    p = payload(candidates=[{"node": "b", "cost": 5}, {"node": "c", "cost": 3}, {"node": "d", "cost": 4}])
    data = client.post("/fault-sensor-placement", json=p).json()
    assert data["selected_nodes"] == ["b", "c", "d"]
    assert data["total_cost"] == 12


def test_deep_branching_tree_only_leaves_required():
    # source--s1-->n1 --s2--> n2 --s4--> n4
    #               |              +--s5--> n5
    #               +--s3--> n3
    # n1、n2 各有两个子段（分支节点，可选）；n3、n4、n5 为叶（必选）。
    # 即使分支节点成本更低，最低成本可行集仍只含三个叶节点。
    segments = [
        {"id": "s1", "parent": "source", "child": "n1", "resistance": 0.1, "rated_current": 10.0},
        {"id": "s2", "parent": "n1", "child": "n2", "resistance": 0.1, "rated_current": 10.0},
        {"id": "s3", "parent": "n1", "child": "n3", "resistance": 0.1, "rated_current": 10.0},
        {"id": "s4", "parent": "n2", "child": "n4", "resistance": 0.1, "rated_current": 10.0},
        {"id": "s5", "parent": "n2", "child": "n5", "resistance": 0.1, "rated_current": 10.0},
    ]
    candidates = [
        {"node": "n1", "cost": 1},
        {"node": "n2", "cost": 1},
        {"node": "n3", "cost": 10},
        {"node": "n4", "cost": 10},
        {"node": "n5", "cost": 10},
    ]
    resp = client.post(
        "/fault-sensor-placement", json={"segments": segments, "candidates": candidates}
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["feasible"] is True
    assert result["selected_nodes"] == ["n3", "n4", "n5"]
    assert result["total_cost"] == 30

    # 六个状态（正常 + 5 段断线）在 [n3,n4,n5] 上的签名两两不同
    vectors = [tuple(sig["connected"]) for sig in result["signatures"]]
    assert len(vectors) == 6 and len(set(vectors)) == 6
    assert [(sig["state"], sig["segment_id"]) for sig in result["signatures"]] == [
        ("normal", None),
        ("fault_segment", "s1"),
        ("fault_segment", "s2"),
        ("fault_segment", "s3"),
        ("fault_segment", "s4"),
        ("fault_segment", "s5"),
    ]
    # s2 断开失 n4、n5（n3 在另一支路仍连通）；s4 仅失 n4
    by_seg = {sig["segment_id"]: sig["connected"] for sig in result["signatures"][1:]}
    assert by_seg["s2"] == [True, False, False]
    assert by_seg["s4"] == [True, False, True]
    assert by_seg["s5"] == [True, True, False]


# ---------- 不可行：必选测点缺失 ----------


def test_infeasible_when_mandatory_nodes_not_offered_star_tree():
    # 星形三支路：a、b、c 均为叶节点，全部必选；只提供 a 一个候选
    data = {
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s2", "parent": "source", "child": "b", "resistance": 0.1, "rated_current": 10.0},
            {"id": "s3", "parent": "source", "child": "c", "resistance": 0.1, "rated_current": 10.0},
        ],
        "candidates": [{"node": "a", "cost": 1}],
    }
    resp = client.post("/fault-sensor-placement", json=data)
    assert resp.status_code == 200
    result = resp.json()
    assert result["feasible"] is False
    assert result["selected_nodes"] is None
    assert result["total_cost"] is None
    assert result["signatures"] is None

    # a 仅在 s1 断开时失电：normal、s2、s3 的全候选签名均为 [连通]
    pairs = result["indistinguishable_pairs"]
    # 按状态描述符字典序升序：fault_segment:* 排在 normal 之前
    assert {(p["state_a"], p["state_b"]) for p in pairs} == {
        ("fault_segment:s2", "normal"),
        ("fault_segment:s3", "normal"),
        ("fault_segment:s2", "fault_segment:s3"),
    }
    # 按状态描述符字典序升序
    assert [(p["state_a"], p["state_b"]) for p in pairs] == sorted(
        (p["state_a"], p["state_b"]) for p in pairs
    )
    # 附字段段 id 与描述一致
    for p in pairs:
        assert p["segment_id_a"] == (None if p["state_a"] == "normal" else p["state_a"].split(":")[1])
        assert p["segment_id_b"] == (None if p["state_b"] == "normal" else p["state_b"].split(":")[1])


def test_infeasible_when_unary_node_missing():
    # c 为单链节点（s3 下仅 s4），不提供 c 时 s1 与 s3 在 b、d 上的签名相同（均失 b、d）
    p = payload(candidates=[{"node": "a", "cost": 1}, {"node": "b", "cost": 5}, {"node": "d", "cost": 4}])
    result = client.post("/fault-sensor-placement", json=p).json()
    assert result["feasible"] is False
    pairs = {(p["state_a"], p["state_b"]) for p in result["indistinguishable_pairs"]}
    # 无候选在 T_s4={d} 的 annulus... 实际：候选 [a,b,d] 下 s4 仅失 d，s3 也失 d（c 无候选）
    assert ("fault_segment:s3", "fault_segment:s4") in pairs


# ---------- 请求模型校验（422） ----------


def test_thirteen_segments_rejected():
    segments = [
        {"id": f"s{i + 1}", "parent": "source" if i == 0 else f"n{i}", "child": f"n{i + 1}",
         "resistance": 0.1, "rated_current": 10.0}
        for i in range(13)
    ]
    candidates = [{"node": f"n{i + 1}", "cost": 1} for i in range(12)]
    resp = client.post("/fault-sensor-placement", json={"segments": segments, "candidates": candidates})
    assert resp.status_code == 422
    assert any("线段列表最多包含 12 条" in d["message"] for d in resp.json()["error"]["details"])


def test_empty_segments_rejected():
    resp = client.post(
        "/fault-sensor-placement",
        json={"segments": [], "candidates": [{"node": "a", "cost": 1}]},
    )
    assert resp.status_code == 422


def test_empty_candidates_rejected():
    resp = client.post("/fault-sensor-placement", json={"segments": SEGMENTS, "candidates": []})
    assert resp.status_code == 422


def test_thirteen_candidates_rejected():
    segments = [
        {"id": f"s{i}", "parent": "source" if i == 0 else f"n{i - 1}", "child": f"n{i}",
         "resistance": 0.1, "rated_current": 10.0}
        for i in range(1, 13)
    ]
    # 长度上限由请求模型拦截（422），先于节点重复等业务校验（400）
    candidates = [{"node": f"n{i}", "cost": 1} for i in range(1, 13)]
    candidates.append({"node": "n1", "cost": 2})
    resp = client.post(
        "/fault-sensor-placement", json={"segments": segments, "candidates": candidates}
    )
    assert resp.status_code == 422
    assert any("候选测点列表最多包含 12 条" in d["message"] for d in resp.json()["error"]["details"])


def test_non_positive_cost_rejected():
    for bad in (0, -1):
        p = payload(candidates=[{"node": "a", "cost": bad}, {"node": "b", "cost": 1},
                                {"node": "c", "cost": 1}, {"node": "d", "cost": 1}])
        resp = client.post("/fault-sensor-placement", json=p)
        assert resp.status_code == 422
        assert any("安装成本必须为正整数" in d["message"] for d in resp.json()["error"]["details"])


def test_fractional_cost_rejected():
    p = payload(candidates=[{"node": n, "cost": 1} for n in ("a", "b", "c", "d")])
    p["candidates"][0]["cost"] = 1.5
    resp = client.post("/fault-sensor-placement", json=p)
    assert resp.status_code == 422


def test_missing_candidate_node_rejected():
    p = payload(candidates=[{"cost": 1}, {"node": "b", "cost": 1}])
    resp = client.post("/fault-sensor-placement", json=p)
    assert resp.status_code == 422


# ---------- 业务校验（400 invalid_input） ----------


def test_duplicate_candidate_node_rejected():
    p = payload(
        candidates=[{"node": "a", "cost": 1}, {"node": "a", "cost": 2},
                    {"node": "b", "cost": 1}, {"node": "c", "cost": 1}, {"node": "d", "cost": 1}]
    )
    resp = client.post("/fault-sensor-placement", json=p)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("候选测点节点重复" in d["message"] for d in error["details"])


def test_source_candidate_rejected():
    p = payload(
        candidates=[{"node": "source", "cost": 1}, {"node": "b", "cost": 1},
                    {"node": "c", "cost": 1}, {"node": "d", "cost": 1}]
    )
    resp = client.post("/fault-sensor-placement", json=p)
    assert resp.status_code == 400
    assert any("不能为根节点" in d["message"] for d in resp.json()["error"]["details"])


def test_unknown_candidate_node_rejected():
    p = payload(candidates=[{"node": "ghost", "cost": 1}, {"node": "b", "cost": 1}])
    resp = client.post("/fault-sensor-placement", json=p)
    assert resp.status_code == 400
    assert any("不存在于树中" in d["message"] for d in resp.json()["error"]["details"])


def test_invalid_tree_rejected():
    segments = SEGMENTS + [
        {"id": "s9", "parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    ]
    resp = client.post(
        "/fault-sensor-placement",
        json={"segments": segments, "candidates": [{"node": "b", "cost": 1}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


def test_auto_generated_segment_ids():
    data = {
        "segments": [
            {"parent": "source", "child": "a", "resistance": 0.1, "rated_current": 10.0},
            {"parent": "source", "child": "b", "resistance": 0.1, "rated_current": 10.0},
        ],
        "candidates": [{"node": "a", "cost": 2}, {"node": "b", "cost": 3}],
    }
    resp = client.post("/fault-sensor-placement", json=data)
    assert resp.status_code == 200
    result = resp.json()
    assert result["feasible"] is True
    assert result["selected_nodes"] == ["a", "b"]
    assert result["total_cost"] == 5
    assert [s["segment_id"] for s in result["signatures"][1:]] == ["seg-1", "seg-2"]


def test_resistance_and_rated_current_do_not_affect_placement():
    base = client.post("/fault-sensor-placement", json=payload()).json()
    changed_segments = [dict(seg, resistance=9.9, rated_current=0.5) for seg in SEGMENTS]
    changed = client.post(
        "/fault-sensor-placement", json=payload(segments=changed_segments)
    ).json()
    assert changed == base


def test_index_lists_fault_sensor_placement_endpoint():
    info = client.get("/").json()
    assert info["fault_sensor_placement"] == "POST /fault-sensor-placement"
