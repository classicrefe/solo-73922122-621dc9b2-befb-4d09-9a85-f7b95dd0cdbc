"""影像交付审查核心：缺片隔离 + 跨组重复摘要的最小隔离集求解。

输入：
- 一份**已发布清单**的快照（病例、同患者/亲属关系、训练/评估分组）；
- 一份影像目录（每行 ``病例号,影像号,SHA-256``）。

隔离规则（均为硬规则）：
1. 缺片：清单中的病例在目录里没有任何一张影像，必须隔离。
2. 跨组重复摘要：同一 SHA-256 摘要同时出现在训练集（组 1）与评估集（组 2）的
   病例上时，重复双方的病例不能同时保留。
3. 同患者与亲属关系（含传递闭包）连通的病例构成一个连通分量；按清单分组规则，
   同一分量内所有病例本就同组。对重复摘要做处理时，必须按整个分量一起隔离，
   不能只隔离分量中的个别病例。
4. 在“保留病例不存在任何跨组重复摘要”的前提下，使被隔离的病例总数最少——
   即对“分量冲突图”求**最小权重点覆盖**（权重=分量内病例数），用 z3 Optimize
   求最优解，并以分量 ID 字典序做唯一的平局裁决，保证同输入同输出。

本模块是纯函数式、只读的：不修改任何输入与清单，便于对同一份数据重复审查并
得到完全一致的建议。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set, Tuple

import z3

from .splitter import Case, RelativePair, build_components

GROUP_NAMES = {1: "训练集", 2: "评估集"}
MAX_CATALOG_ROWS = 200
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReviewError(Exception):
    """影像目录本身不合法（在进入隔离求解之前校验）。"""


@dataclass(frozen=True)
class CatalogEntry:
    case_id: str
    image_id: str
    digest: str


@dataclass
class ConflictEdge:
    """两个分量之间因跨组重复摘要产生的冲突。"""

    edge_id: str
    component_a: str
    component_b: str
    group_a: int
    group_b: int
    digest: str
    # 触发该冲突的代表性病例（各取一个，便于页面解释）
    case_a: str
    case_b: str


@dataclass
class ComponentInfo:
    component_id: str
    case_ids: List[str]
    group: int
    patients: List[str]
    case_count: int


# --------------------------------------------------------------------------- #
# 目录解析与校验
# --------------------------------------------------------------------------- #
def parse_catalog_csv(raw: bytes, known_case_ids: Sequence[str]) -> List[CatalogEntry]:
    """解析并整体校验影像目录 CSV。

    任何一条不合法都抛出 :class:`ReviewError`（错误信息列表），调用方据此整体
    拒绝，不产生任何审查结果，也不会改动既有数据。
    """
    import csv
    import io

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ReviewError([f"文件编码无法识别（需要 UTF-8 或 GB18030）：{exc}"])

    rows = [row for row in csv.reader(io.StringIO(text))]
    if not rows:
        raise ReviewError(["影像目录文件为空。"])

    header = [h.strip().lstrip("﻿").lower().replace(" ", "") for h in rows[0]]
    expected = ["病例号", "影像号", "sha-256"]
    aliases = {
        "病例号": "病例号", "case_id": "病例号", "caseid": "病例号",
        "影像号": "影像号", "image_id": "影像号", "imageid": "影像号",
        "sha-256": "sha-256", "sha256": "sha-256", "digest": "sha-256",
        "摘要": "sha-256",
    }
    cols = [aliases.get(h) for h in header]
    missing = [name for name in expected if name not in (cols or [])]
    if missing:
        raise ReviewError([
            "影像目录表头缺少列：" + "、".join(missing)
            + f"。正确表头：病例号,影像号,SHA-256；当前表头：{', '.join(header)}"
        ])

    body = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    if not body:
        raise ReviewError(["影像目录没有数据行。"])
    if len(body) > MAX_CATALOG_ROWS:
        raise ReviewError([f"影像目录最多 {MAX_CATALOG_ROWS} 行，当前 {len(body)} 行。"])

    i_case, i_image, i_digest = cols.index("病例号"), cols.index("影像号"), cols.index("sha-256")
    known = set(known_case_ids)
    entries: List[CatalogEntry] = []
    seen_images: Set[str] = set()
    errors: List[str] = []

    for line_no, row in enumerate(body, start=2):
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        case_id = row[i_case].strip()
        image_id = row[i_image].strip()
        digest = row[i_digest].strip().lower()

        if not case_id:
            errors.append(f"第 {line_no} 行：病例号为空")
            continue
        if not image_id:
            errors.append(f"第 {line_no} 行：影像号为空（病例 {case_id}）")
            continue
        if not digest:
            errors.append(f"第 {line_no} 行：SHA-256 摘要为空（影像 {image_id}）")
            continue
        if case_id not in known:
            errors.append(f"第 {line_no} 行：病例号 {case_id} 不在所选清单内")
        if not SHA256_RE.match(digest):
            errors.append(
                f"第 {line_no} 行：摘要必须是 64 位十六进制字符，"
                f"实际为 “{row[i_digest].strip()}”（影像 {image_id}）"
            )
        if image_id in seen_images:
            errors.append(f"第 {line_no} 行：影像号 {image_id} 重复（影像号不得重复）")
            continue
        seen_images.add(image_id)
        entries.append(CatalogEntry(case_id=case_id, image_id=image_id, digest=digest))

    if errors:
        raise ReviewError(errors)
    return entries


# --------------------------------------------------------------------------- #
# 审查主流程
# --------------------------------------------------------------------------- #
def review_delivery(
    cases: Sequence[Case],
    relative_pairs: Sequence[RelativePair],
    assignments: Dict[str, int],
    catalog: Sequence[CatalogEntry],
) -> dict:
    """执行一次完整的交付审查，返回可直接序列化为 JSON 的结果。

    ``assignments`` 为清单中每个病例的分组（1=训练集，2=评估集）。
    """
    cases = sorted(cases, key=lambda c: c.case_id)
    if not cases:
        raise ReviewError(["所选清单不包含任何病例，无法审查。"])

    unknown_groups = sorted({cid for cid in assignments if cid not in {c.case_id for c in cases}})
    if unknown_groups:
        raise ReviewError([f"清单分组引用了不存在的病例：{', '.join(unknown_groups)}"])

    # 1) 构建同患者 + 亲属传递闭包的连通分量（复用切分平台的并查集实现）
    raw_components, _merges = build_components(cases, relative_pairs)
    patient_of = {c.case_id: c.patient_id for c in cases}
    group_of = dict(assignments)

    # 以分量内最小病例号作为稳定 ID，保证确定性
    comp_members: Dict[str, List[str]] = {}
    comp_id_by_case: Dict[str, str] = {}
    for _root, members in raw_components.items():
        cid = min(members)
        members_sorted = sorted(members)
        comp_members[cid] = members_sorted
        for m in members_sorted:
            comp_id_by_case[m] = cid

    comp_ids_sorted = sorted(comp_members)
    components: Dict[str, ComponentInfo] = {}
    for cid in comp_ids_sorted:
        members = comp_members[cid]
        groups = {group_of[m] for m in members if m in group_of}
        if len(groups) > 1:
            # 切分平台的硬约束已保证同分量同组；这里防御性校验
            raise ReviewError([
                f"清单数据矛盾：连通分量（以病例 {cid} 为代表）内的病例被分到不同组，"
                "无法按整组隔离。"
            ])
        group = next(iter(groups)) if groups else 0
        components[cid] = ComponentInfo(
            component_id=cid,
            case_ids=members,
            group=group,
            patients=sorted({patient_of[m] for m in members}),
            case_count=len(members),
        )

    # 2) 缺片：病例在目录中没有任何影像
    cases_with_images: Set[str] = {e.case_id for e in catalog}
    missing_cases = [c.case_id for c in cases if c.case_id not in cases_with_images]
    missing_by_component: Dict[str, List[str]] = {}
    for cid in missing_cases:
        missing_by_component.setdefault(comp_id_by_case[cid], []).append(cid)

    # 3) 跨组重复摘要 -> 分量冲突图
    # digest -> 出现过该摘要的病例集合
    digest_to_cases: Dict[str, Set[str]] = {}
    for e in catalog:
        digest_to_cases.setdefault(e.digest, set()).add(e.case_id)

    edge_keys: Dict[Tuple[str, str], ConflictEdge] = {}
    duplicate_digests: List[str] = []
    for digest in sorted(digest_to_cases):
        owners = sorted(digest_to_cases[digest])
        comps = sorted({comp_id_by_case[c] for c in owners if c in comp_id_by_case})
        cross = [c for c in comps if components[c].group in (1, 2) and
                 any(components[c2].group != components[c].group
                     and components[c2].group in (1, 2) for c2 in comps)]
        if not cross:
            continue
        duplicate_digests.append(digest)
        # 该摘要下每一对跨组分量各产生一条边（同组内重复不违规，不产生边）
        for i, ca in enumerate(comps):
            for cb in comps[i + 1:]:
                ga, gb = components[ca].group, components[cb].group
                if ga == gb or ga not in (1, 2) or gb not in (1, 2):
                    continue
                key = tuple(sorted((ca, cb)))
                if key in edge_keys:
                    continue
                rep_a = next(c for c in owners if comp_id_by_case.get(c) == ca)
                rep_b = next(c for c in owners if comp_id_by_case.get(c) == cb)
                edge_keys[key] = ConflictEdge(
                    edge_id=f"E{len(edge_keys) + 1:03d}",
                    component_a=key[0],
                    component_b=key[1],
                    group_a=components[key[0]].group,
                    group_b=components[key[1]].group,
                    digest=digest,
                    case_a=rep_a,
                    case_b=rep_b,
                )
    edges = sorted(edge_keys.values(), key=lambda e: (e.component_a, e.component_b, e.digest))

    # 4) 最小权重点覆盖：每个冲突分量一个布尔变量，True=整组隔离
    #    目标先最小化隔离病例总数；再按分量 ID 字典序最小化隔离集合（确定性）。
    opt = z3.Optimize()
    x = {cid: z3.Bool(f"isolate_{i}_{cid}") for i, cid in enumerate(comp_ids_sorted)}

    for edge in edges:
        opt.add(z3.Or(x[edge.component_a], x[edge.component_b]))

    weight_terms = [z3.If(x[cid], components[cid].case_count, 0) for cid in comp_ids_sorted]
    isolated_count = z3.Sum(weight_terms)
    opt.minimize(isolated_count)
    # 平局裁决：字典序靠前的分量优先不隔离，得到唯一确定的最小隔离集
    for cid in comp_ids_sorted:
        opt.minimize(z3.If(x[cid], 1, 0))

    result = opt.check()
    if result != z3.sat:
        raise ReviewError([f"隔离优化求解失败（求解器状态：{result}）。"])
    model = opt.model()
    isolated_components = {
        cid for cid in comp_ids_sorted
        if z3.is_true(model.eval(x[cid], model_completion=True))
    }

    # 5) 独立复核：每条冲突边至少一端被隔离
    for edge in edges:
        assert edge.component_a in isolated_components or edge.component_b in isolated_components

    # 6) 汇总逐病例结论与依据
    # 病例 -> 依据列表；依据类型：missing（缺片）/ duplicate（重复摘要）/ kinship（亲属连带）
    case_reasons: Dict[str, List[dict]] = {}

    def add_reason(case_id: str, kind: str, detail: str) -> None:
        reason = {"kind": kind, "kind_name": _reason_name(kind), "detail": detail}
        existed = case_reasons.setdefault(case_id, [])
        if all(r["detail"] != detail or r["kind"] != kind for r in existed):
            existed.append(reason)

    # 6a. 缺片依据
    for cid in missing_cases:
        add_reason(cid, "missing", "影像目录中没有该病例的任何影像（缺片）")

    # 6b. 重复摘要 + 亲属连带依据
    for cid in sorted(isolated_components):
        info = components[cid]
        related_edges = [
            e for e in edges
            if cid in (e.component_a, e.component_b)
        ]
        digest_list = sorted({e.digest for e in related_edges})
        for case_id in info.case_ids:
            # 直接持有跨组重复摘要的病例：重复摘要依据
            owned_dups = sorted(
                d for d in digest_list
                if case_id in digest_to_cases.get(d, set())
            )
            if owned_dups:
                add_reason(
                    case_id, "duplicate",
                    "该病例持有的摘要 " + "、".join(owned_dups) + " 同时出现在训练集与评估集"
                )
            else:
                # 分量内其余病例：亲属/同患者连带
                peers = sorted({
                    peer for e in related_edges
                    for peer in (e.case_a, e.case_b)
                    if comp_id_by_case.get(peer) == cid and peer != case_id
                })
                add_reason(
                    case_id, "kinship",
                    "与持有跨组重复摘要的病例 "
                    + ("、".join(peers) if peers else "同组病例")
                    + " 属同一患者或亲属连通，按整组连带隔离"
                )

    isolated_case_set: Set[str] = set(missing_cases)
    for cid in isolated_components:
        isolated_case_set.update(components[cid].case_ids)

    deliverable_cases = [c.case_id for c in cases if c.case_id not in isolated_case_set]
    isolated_cases_sorted = sorted(isolated_case_set)

    # 7) 最终安全复核：保留病例之间不存在任何跨组重复摘要
    kept_digest_groups: Dict[str, Set[int]] = {}
    for e in catalog:
        if e.case_id in isolated_case_set:
            continue
        kept_digest_groups.setdefault(e.digest, set()).add(group_of.get(e.case_id, 0))
    leaked = sorted(d for d, gs in kept_digest_groups.items()
                    if 1 in gs and 2 in gs)
    assert not leaked, f"内部错误：保留病例仍存在跨组重复摘要 {leaked}"

    case_rows = []
    for c in cases:
        cid = c.case_id
        images = sorted((e for e in catalog if e.case_id == cid),
                        key=lambda e: e.image_id)
        is_isolated = cid in isolated_case_set
        case_rows.append({
            "case_id": cid,
            "patient_id": c.patient_id,
            "group": group_of.get(cid, 0),
            "group_name": GROUP_NAMES.get(group_of.get(cid, 0), "未分组"),
            "component_id": comp_id_by_case[cid],
            "image_count": len(images),
            "image_ids": [e.image_id for e in images],
            "digests": sorted({e.digest for e in images}),
            "decision": "isolated" if is_isolated else "deliverable",
            "decision_name": "隔离" if is_isolated else "可交付",
            "reasons": case_reasons.get(cid, []),
        })

    return {
        "status": "ok",
        "stats": {
            "catalog_rows": len(catalog),
            "case_total": len(cases),
            "component_total": len(components),
            "deliverable_count": len(deliverable_cases),
            "isolated_count": len(isolated_cases_sorted),
            "missing_count": len(missing_cases),
            "duplicate_digest_count": len(duplicate_digests),
            "conflict_edge_count": len(edges),
            "isolated_component_count": len(isolated_components),
            "minimum_isolated_verified": True,
        },
        "duplicate_digests": duplicate_digests,
        "conflict_edges": [
            {
                "edge_id": e.edge_id,
                "digest": e.digest,
                "component_a": e.component_a,
                "component_b": e.component_b,
                "group_a": e.group_a,
                "group_b": e.group_b,
                "group_a_name": GROUP_NAMES[e.group_a],
                "group_b_name": GROUP_NAMES[e.group_b],
                "case_a": e.case_a,
                "case_b": e.case_b,
            }
            for e in edges
        ],
        "components": [
            {
                "component_id": cid,
                "case_ids": components[cid].case_ids,
                "patients": components[cid].patients,
                "group": components[cid].group,
                "group_name": GROUP_NAMES.get(components[cid].group, "未分组"),
                "case_count": components[cid].case_count,
                "isolated": cid in isolated_components,
                "has_missing_case": cid in missing_by_component,
            }
            for cid in comp_ids_sorted
        ],
        "cases": case_rows,
        "deliverable_case_ids": deliverable_cases,
        "isolated_case_ids": isolated_cases_sorted,
    }


def _reason_name(kind: str) -> str:
    return {
        "missing": "缺片",
        "duplicate": "重复摘要",
        "kinship": "亲属连带",
    }.get(kind, kind)
