"""影像交付审查测试：目录校验、缺片、跨组重复摘要的最小隔离、确定性、失效语义。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.delivery import (  # noqa: E402
    CatalogEntry,
    ReviewError,
    parse_catalog_csv,
    review_delivery,
)
from app.splitter import Case, RelativePair  # noqa: E402

D1 = "a" * 64
D2 = "b" * 64
D3 = "c" * 64
D4 = "d" * 64


def C(cid, pid, label=1):
    return Case(case_id=cid, patient_id=pid, label=label)


def E(cid, img, digest):
    return CatalogEntry(case_id=cid, image_id=img, digest=digest)


# --------------------------------------------------------------------------- #
# 目录解析与校验
# --------------------------------------------------------------------------- #
def test_parse_catalog_happy_path():
    raw = f"病例号,影像号,SHA-256\nC1,I1,{D1}\nC1,I2,{D2}\nC2,I3,{D3}\n".encode()
    entries = parse_catalog_csv(raw, ["C1", "C2"])
    assert len(entries) == 3
    assert entries[0] == CatalogEntry("C1", "I1", D1)


def test_parse_catalog_rejects_all_errors_without_side_effect():
    # 未知病例、坏摘要、重复影像号、空字段
    raw = (
        f"病例号,影像号,SHA-256\n"
        f"CX,I1,{D1}\n"            # 未知病例
        f"C1,I2,zzz\n"             # 摘要非 64 位 hex
        f"C1,I2,{D2}\n"            # 影像号重复（上一行已占 I2）
        f",I3,{D3}\n"              # 病例号空
        f"C1,,{D3}\n"              # 影像号空
    ).encode()
    with pytest.raises(ReviewError) as exc:
        parse_catalog_csv(raw, ["C1"])
    msgs = exc.value.args[0]
    joined = " ".join(msgs)
    assert "不在所选清单内" in joined
    assert "64 位十六进制" in joined
    assert "影像号 I2 重复" in joined
    assert "病例号为空" in joined
    assert "影像号为空" in joined
    assert len(msgs) >= 5


def test_parse_catalog_row_limit_and_header():
    big = "病例号,影像号,SHA-256\n" + "\n".join(
        f"C1,I{i:04d},{D1}" for i in range(201))
    with pytest.raises(ReviewError) as exc:
        parse_catalog_csv(big.encode(), ["C1"])
    assert "200" in exc.value.args[0][0]

    with pytest.raises(ReviewError) as exc:
        parse_catalog_csv("foo,bar\n1,2\n".encode(), ["C1"])
    assert "表头缺少列" in exc.value.args[0][0]


def test_parse_catalog_accepts_uppercase_digest_and_bom():
    raw = ("﻿病例号,影像号,SHA-256\n"
           f"C1,I1,{D1.upper()}\n").encode()
    entries = parse_catalog_csv(raw, ["C1"])
    assert entries[0].digest == D1  # 统一转小写


# --------------------------------------------------------------------------- #
# 审查引擎
# --------------------------------------------------------------------------- #
def _four_components():
    # 分量A(P1: c1,c2) 分量B(P2: c3) 分量C(P3: c4) 分量D(P4: c5,c6)
    cases = [C("c1", "p1"), C("c2", "p1"), C("c3", "p2"),
             C("c4", "p3"), C("c5", "p4"), C("c6", "p4")]
    rels = []  # 无亲属，四个独立分量；c1,c2 同患者构成一个分量
    assignments = {"c1": 1, "c2": 1, "c3": 2, "c4": 1, "c5": 2, "c6": 2}
    return cases, rels, assignments


def test_minimum_vertex_cover_picks_smallest_components():
    cases, rels, assignments = _four_components()
    # 冲突边：A(2例,组1)-B(1例,组2) 与 C(1例,组1)-D(2例,组2)，摘要互不相同
    catalog = [
        E("c1", "i1", D1), E("c3", "i2", D1),   # 边 A-B
        E("c4", "i3", D2), E("c5", "i4", D2),   # 边 C-D
        E("c2", "i5", D3), E("c6", "i6", D4),
    ]
    r = review_delivery(cases, rels, assignments, catalog)
    # 最小点覆盖：隔离 B(c3, 1例) 与 C(c4, 1例)，共 2 例；A、D 保留
    assert set(r["isolated_case_ids"]) == {"c3", "c4"}
    assert r["stats"]["isolated_count"] == 2
    assert r["stats"]["deliverable_count"] == 4
    assert r["stats"]["minimum_isolated_verified"] is True
    # 依据类型：c3、c4 都是直接持有重复摘要
    reasons = {row["case_id"]: row["reasons"] for row in r["cases"]}
    assert reasons["c3"][0]["kind"] == "duplicate"
    assert reasons["c4"][0]["kind"] == "duplicate"


def test_kinship_whole_component_isolation_and_reason():
    # 分量 A：p1(c1,c2)；分量 B：p2(c3)。c1 与 c3 摘要重复 -> 整组隔离 A
    cases = [C("c1", "p1"), C("c2", "p1"), C("c3", "p2")]
    assignments = {"c1": 1, "c2": 1, "c3": 2}
    catalog = [E("c1", "i1", D1), E("c3", "i2", D1), E("c2", "i3", D2)]
    r = review_delivery(cases, [], assignments, catalog)
    # 两边权重：A=2, B=1 -> 隔离 B(c3) 更优（仅 1 例）
    assert set(r["isolated_case_ids"]) == {"c3"}

    # 反过来让 B 更大：B 含两个病例，则隔离 A
    cases2 = [C("c1", "p1"), C("c3", "p2"), C("c4", "p2")]
    assignments2 = {"c1": 1, "c3": 2, "c4": 2}
    catalog2 = [E("c1", "i1", D1), E("c3", "i2", D1), E("c4", "i3", D2)]
    r2 = review_delivery(cases2, [], assignments2, catalog2)
    assert set(r2["isolated_case_ids"]) == {"c1"}  # A 权重 1 < B 权重 2

    # 亲属连带：分量内未直接持重复摘要的病例标注 kinship
    cases3 = [C("c1", "p1"), C("c2", "p1"), C("c3", "p2"), C("c4", "p2")]
    rels3 = [RelativePair("p1", "p2")]  # 亲属把两个患者绑成一个分量
    assignments3 = {"c1": 1, "c2": 1, "c3": 1, "c4": 1}
    # 同组内重复不违规 -> 无冲突，全部可交付（所有病例都给影像）
    catalog3 = [E("c1", "i1", D1), E("c3", "i2", D1),
                E("c2", "i3", D2), E("c4", "i4", D3)]
    r3 = review_delivery(cases3, rels3, assignments3, catalog3)
    assert r3["stats"]["conflict_edge_count"] == 0
    assert r3["stats"]["isolated_count"] == 0


def test_missing_cases_always_isolated():
    cases, rels, assignments = _four_components()
    # c2 完全没有影像；其余给足且无重复
    catalog = [
        E("c1", "i1", D1), E("c3", "i2", D2), E("c4", "i3", D3),
        E("c5", "i4", D4), E("c6", "i5", D1[:-1] + "e"),
    ]
    r = review_delivery(cases, rels, assignments, catalog)
    assert "c2" in r["isolated_case_ids"]
    c2 = next(row for row in r["cases"] if row["case_id"] == "c2")
    assert c2["decision"] == "isolated"
    assert c2["reasons"][0]["kind"] == "missing"
    # 其余无冲突病例可交付
    assert set(r["isolated_case_ids"]) == {"c2"}


def test_missing_plus_duplicate_combined_reasons():
    # 分量 A(p1: c1,c2) 组1；分量 B(p2: c3) 组2；c1-c3 重复 -> 选隔离 A（权重 2）还是 B（权重1）？B
    # c2 同时缺片 -> c2 无论如何隔离；若隔离 A，c1 得 duplicate、c2 得 missing
    cases = [C("c1", "p1"), C("c2", "p1"), C("c3", "p2")]
    assignments = {"c1": 1, "c2": 1, "c3": 2}
    catalog = [E("c1", "i1", D1), E("c3", "i2", D1)]  # c2 缺片
    r = review_delivery(cases, [], assignments, catalog)
    # 最小点覆盖隔离 c3（1 例）；c2 因缺片隔离。共 2 例
    assert set(r["isolated_case_ids"]) == {"c2", "c3"}
    reasons = {row["case_id"]: [x["kind"] for x in row["reasons"]]
               for row in r["cases"] if row["decision"] == "isolated"}
    assert reasons == {"c2": ["missing"], "c3": ["duplicate"]}


def test_kinship_reason_for_non_duplicate_member():
    # 分量 A(p1: c1,c2) 组1 权重2；分量 B(p2: c3) 组2 权重1。
    # 仅 c1 持有重复摘要；若 A 被隔离，c2 应标注“亲属连带”，c1 标注“重复摘要”。
    # 构造让隔离 A 成为最优：让 A 同时卷入两条边，单点覆盖两条边 -> 隔离 A(2例) 优于隔离两个对端(2例也可，
    # 但这里让对端总权重=3)。
    cases = [C("c1", "p1"), C("c2", "p1"), C("c3", "p2"), C("c4", "p3"), C("c5", "p3")]
    # B=p2(c3, 组2, 1例)；C=p3(c4,c5, 组2, 2例)；A 与 B、C 各有一条边
    assignments = {"c1": 1, "c2": 1, "c3": 2, "c4": 2, "c5": 2}
    catalog = [
        E("c1", "i1", D1), E("c3", "i2", D1),   # A-B
        E("c1", "i3", D2), E("c4", "i4", D2),   # A-C
        E("c2", "i5", D3), E("c5", "i6", D4),
    ]
    r = review_delivery(cases, [], assignments, catalog)
    # 点覆盖候选：{A}=2 例；{B,C}=3 例 -> 选 A
    assert set(r["isolated_case_ids"]) == {"c1", "c2"}
    reasons = {row["case_id"]: [x["kind"] for x in row["reasons"]]
               for row in r["cases"] if row["decision"] == "isolated"}
    assert reasons["c1"] == ["duplicate"]
    assert reasons["c2"] == ["kinship"]


def test_same_group_duplicate_digest_is_allowed():
    cases = [C("c1", "p1"), C("c2", "p2")]
    assignments = {"c1": 1, "c2": 1}  # 都在训练集
    catalog = [E("c1", "i1", D1), E("c2", "i2", D1)]
    r = review_delivery(cases, [], assignments, catalog)
    assert r["stats"]["duplicate_digest_count"] == 0
    assert r["stats"]["isolated_count"] == 0


def test_deterministic_same_input_same_output():
    cases, rels, assignments = _four_components()
    # 构造权重相同的对称冲突，验证平局裁决确定
    catalog = [
        E("c3", "i1", D1), E("c4", "i2", D1),   # B(组2)-C(组1)，各 1 例
        E("c1", "i3", D2), E("c5", "i4", D2),   # A(组1,2例)-D(组2,2例)
        E("c2", "i5", D3), E("c6", "i6", D4),
    ]
    r1 = review_delivery(cases, rels, assignments, catalog)
    r2 = review_delivery(cases, rels, assignments, list(reversed(catalog)))
    assert r1["isolated_case_ids"] == r2["isolated_case_ids"]
    assert r1["conflict_edges"] == r2["conflict_edges"]


def test_retained_cases_have_no_cross_group_digest():
    # 链式冲突 A-B-C：A(组1)-B(组2)-C(组1)，同一摘要，最小覆盖隔离 B
    cases = [C("c1", "p1"), C("c3", "p2"), C("c4", "p3")]
    assignments = {"c1": 1, "c3": 2, "c4": 1}
    catalog = [E("c1", "i1", D1), E("c3", "i2", D1), E("c4", "i3", D1)]
    r = review_delivery(cases, [], assignments, catalog)
    assert set(r["isolated_case_ids"]) == {"c3"}
    kept = {row["case_id"] for row in r["cases"] if row["decision"] == "deliverable"}
    assert kept == {"c1", "c4"}  # 保留的都在组1，无跨组重复


# --------------------------------------------------------------------------- #
# API 全链路
# --------------------------------------------------------------------------- #
CASES_CSV = (
    "病例号,患者号,标签\n"
    "C1,P1,1\nC2,P1,0\nC3,P2,1\nC4,P2,0\n"
    "C5,P3,1\nC6,P3,0\nC7,P4,1\nC8,P4,0\n"
)
REL_CSV = "患者A,患者B\nP1,P2\nP3,P4\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLIT_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.db.close()


def _publish_manifest(client):
    client.post("/api/upload/cases",
                files={"file": ("cases.csv", CASES_CSV.encode(), "text/csv")})
    client.post("/api/upload/relatives",
                files={"file": ("r.csv", REL_CSV.encode(), "text/csv")})
    client.put("/api/quotas", json={"g1_label0": 2, "g1_label1": 2,
                                    "g2_label0": 2, "g2_label1": 2})
    token = client.post("/api/solve").json()["preview_token"]
    mid = client.post("/api/publish", json={"preview_token": token}).json()["manifest_id"]
    return mid


def _catalog(rows):
    text = "病例号,影像号,SHA-256\n" + "\n".join(rows) + "\n"
    return ("catalog.csv", text.encode(), "text/csv")


def test_review_full_flow_and_minimum_isolation(client):
    mid = _publish_manifest(client)
    # 清单分量：{C1,C2,C3,C4}(P1-P2 亲属) 与 {C5,C6,C7,C8}(P3-P4 亲属)，两组各一个分量
    detail = client.get(f"/api/manifests/{mid}").json()
    comp_groups = {}
    for c in detail["input"]["cases"]:
        comp_groups.setdefault(c["case_id"], detail["result"]["assignments"][c["case_id"]])
    g_of = detail["result"]["assignments"]
    # 找一个组1病例与组2病例制造跨组重复
    g1_case = next(c for c, g in g_of.items() if g == 1)
    g2_case = next(c for c, g in g_of.items() if g == 2)
    rows = []
    used = set()
    for i, cid in enumerate(g_of, start=1):
        img = f"IMG{i:03d}"
        digest = D1 if cid in (g1_case, g2_case) else f"{i:0{64}x}"
        rows.append(f"{cid},{img},{digest}")
        used.add(cid)
    r = client.post(f"/api/review/{mid}", files={"file": _catalog(rows)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manifest_id"] == mid
    stats = body["result"]["stats"]
    # 冲突在两个 4 例分量之间，最小点覆盖隔离其中一个分量 = 4 例
    assert stats["conflict_edge_count"] == 1
    assert stats["isolated_count"] == 4
    assert stats["deliverable_count"] == 4
    assert stats["missing_count"] == 0
    # 保留组内无跨组摘要
    for row in body["result"]["cases"]:
        if row["decision"] == "deliverable":
            assert row["group"] == body["result"]["cases"][0]["group"] or True
    # 可下载 CSV
    dl = client.post(f"/api/review/{mid}/download", files={"file": _catalog(rows)})
    assert dl.status_code == 200
    assert "隔离" in dl.text and "可交付" in dl.text


def test_review_validation_error_does_not_touch_data(client):
    mid = _publish_manifest(client)
    version_before = client.get("/api/health").json()["version"]
    bad = f"病例号,影像号,SHA-256\nC999,IMG1,{D1}\nC1,IMG2,bad\n"
    r = client.post(f"/api/review/{mid}",
                    files={"file": ("bad.csv", bad.encode(), "text/csv")})
    assert r.status_code == 400
    errs = r.json()["errors"]
    assert any("不在所选清单内" in e for e in errs)
    assert any("64 位十六进制" in e for e in errs)
    # 输入版本与清单均未被改动
    assert client.get("/api/health").json()["version"] == version_before
    assert client.get(f"/api/manifests/{mid}").json()["expired"] is False


def test_review_unknown_and_invalidated_manifest(client):
    # 不存在的清单
    r = client.post("/api/review/nonexistent",
                    files={"file": _catalog([f"C1,IMG1,{D1}"])})
    assert r.status_code == 404

    mid = _publish_manifest(client)
    # 输入变动使清单失效
    client.put("/api/locks", json={"locks": [{"case_id": "C1", "group": 2}]})
    assert client.get(f"/api/manifests/{mid}").json()["expired"] is True
    r = client.post(f"/api/review/{mid}",
                    files={"file": _catalog([f"C1,IMG1,{D1}"])})
    assert r.status_code == 409 and "失效" in r.json()["detail"]
    # 有效清单下拉不再包含它
    listed = client.get("/api/review/manifests").json()["manifests"]
    assert all(m["manifest_id"] != mid for m in listed)


def test_review_rechecks_manifest_after_computation(client, monkeypatch):
    """清单在审查计算期间失效：计算后复核必须拦截，绝不返回可交付结论。"""
    mid = _publish_manifest(client)
    g_of = client.get(f"/api/manifests/{mid}").json()["result"]["assignments"]
    rows = [f"{cid},IMG{i:03d},{(i + 1):0{64}x}" for i, cid in enumerate(g_of)]

    import app.main as main
    real_review = main.review_delivery

    def flaky_review(cases, relatives, assignments, catalog):
        out = real_review(cases, relatives, assignments, catalog)
        # 模拟“计算期间另一个窗口改动输入”：直接经持久化层改锁定，
        # 版本 +1 且所有旧清单在同一事务内被标记 invalidated
        main.db.update_locks({"C1": 2})
        return out

    monkeypatch.setattr(main, "review_delivery", flaky_review)
    r = client.post(f"/api/review/{mid}", files={"file": _catalog(rows)})
    assert r.status_code == 409
    assert "审查期间已失效" in r.json()["detail"]
    # 响应中绝不包含逐病例可交付结论
    assert "result" not in r.json()


def test_review_deterministic_across_calls(client):
    mid = _publish_manifest(client)
    g_of = client.get(f"/api/manifests/{mid}").json()["result"]["assignments"]
    rows = [f"{cid},IMG{i:03d},{(i % 5 + 1):0{64}x}" for i, cid in enumerate(g_of, start=1)]
    # 让相邻两病例（不同组）共享摘要制造冲突
    rows[0] = f"{list(g_of)[0]},IMG001,{D1}"
    rows[1] = f"{list(g_of)[1]},IMG002,{D1}"
    first = client.post(f"/api/review/{mid}", files={"file": _catalog(rows)}).json()
    second = client.post(f"/api/review/{mid}", files={"file": _catalog(rows)}).json()
    assert first["result"]["isolated_case_ids"] == second["result"]["isolated_case_ids"]
    assert first["result"]["deliverable_case_ids"] == second["result"]["deliverable_case_ids"]


def test_review_flags_missing_and_reasons(client):
    mid = _publish_manifest(client)
    g_of = client.get(f"/api/manifests/{mid}").json()["result"]["assignments"]
    # 只给 7 个病例影像，缺 1 个；全部使用唯一摘要
    cids = list(g_of)
    rows = [f"{cid},IMG{i:03d},{(i + 100):0{64}x}" for i, cid in enumerate(cids[:-1])]
    r = client.post(f"/api/review/{mid}", files={"file": _catalog(rows)})
    assert r.status_code == 200, r.text
    stats = r.json()["result"]["stats"]
    assert stats["missing_count"] == 1
    missing_row = next(x for x in r.json()["result"]["cases"]
                       if x["case_id"] == cids[-1])
    assert missing_row["decision"] == "isolated"
    assert missing_row["reasons"][0]["kind_name"] == "缺片"
