"""复审包差异：逐字段比较两个评审包的封存清单（纯函数，无 I/O）。

设计约束：
- 纯 Python 只读：不打开数据库、不产生任何写操作，不触发复审任务；
- 差异必须能指向两个来源版本：每条变化同时给出 base（原包）与
  target（复审包）的 package_id/version_id/sha256；
- 条目按 material_id 归组对齐（复审包自动带入旧条目，材料维度稳定），
  再逐字段比较 version_id/sha256/kind/sensitivity；
- 同一材料在一个包中可能引用多个版本（entries 的唯一键是
  (package_id, version_id)）：先按相同 version_id 配平，剩余版本按
  version_no 顺序配对为 modified，落单方为 added/removed。
"""
from __future__ import annotations

from typing import Any, Iterable

from .models import EntryVersionSnapshot, ReviewPackage

ADDED = "added"          # 复审包新增的材料
REMOVED = "removed"      # 复审包中消失（撤回不带入或移除）
MODIFIED = "modified"    # 同一材料逐字段变化
UNCHANGED = "unchanged"  # 逐字段相同（默认不输出）

# 逐字段比较的字段顺序，决定输出稳定性
ENTRY_FIELDS = ("version_id", "sha256", "kind", "sensitivity")


def _group_by_material(
    entries: Iterable[EntryVersionSnapshot],
) -> dict[str, list[EntryVersionSnapshot]]:
    grouped: dict[str, list[EntryVersionSnapshot]] = {}
    for e in entries:
        grouped.setdefault(e.material_id, []).append(e)
    for snaps in grouped.values():
        snaps.sort(key=lambda s: (s.version_no, s.version_id))
    return grouped


def _align_material(
    base_list: list[EntryVersionSnapshot],
    target_list: list[EntryVersionSnapshot],
) -> list[tuple[EntryVersionSnapshot | None, EntryVersionSnapshot | None]]:
    """同一材料在两侧的版本配对：相同 version_id 优先，其余按顺序配对。"""
    base_by_vid = {s.version_id: s for s in base_list}
    target_by_vid = {s.version_id: s for s in target_list}

    pairs: list[tuple[EntryVersionSnapshot | None, EntryVersionSnapshot | None]] = []
    common = sorted(set(base_by_vid) & set(target_by_vid))
    for vid in common:
        pairs.append((base_by_vid[vid], target_by_vid[vid]))

    base_left = [s for s in base_list if s.version_id not in target_by_vid]
    target_left = [s for s in target_list if s.version_id not in base_by_vid]
    paired = min(len(base_left), len(target_left))
    for i in range(paired):
        pairs.append((base_left[i], target_left[i]))
    for i in range(paired, len(base_left)):
        pairs.append((base_left[i], None))
    for i in range(paired, len(target_left)):
        pairs.append((None, target_left[i]))
    return pairs


def _entry_ref(
    e: EntryVersionSnapshot | None, package_id: str | None
) -> dict[str, Any] | None:
    """变化条目指向的来源版本坐标。"""
    if e is None:
        return None
    return {
        "package_id": package_id,
        "entry_id": e.entry_id,
        "material_id": e.material_id,
        "version_id": e.version_id,
        "version_no": e.version_no,
        "sha256": e.sha256,
        "version_withdrawn": e.version_withdrawn,
    }


def _field_changes(
    base: EntryVersionSnapshot | None,
    target: EntryVersionSnapshot | None,
) -> list[dict[str, Any]]:
    changes = []
    for field_name in ENTRY_FIELDS:
        old = getattr(base, field_name) if base is not None else None
        new = getattr(target, field_name) if target is not None else None
        if old != new:
            changes.append({"field": field_name, "base": old, "target": new})
    return changes


def diff_packages(
    base: ReviewPackage,
    target: ReviewPackage,
    *,
    base_snapshots: Iterable[EntryVersionSnapshot],
    target_snapshots: Iterable[EntryVersionSnapshot],
    include_unchanged: bool = False,
) -> dict[str, Any]:
    """计算 base -> target 的清单级差异。

    base/target 提供包级坐标（package_id、title、状态、指纹）；
    snapshots 为各自条目经 SQLite JOIN versions 后的只读快照，
    由持久化层在只读查询中装配（关联来源版本）。
    """
    base_groups = _group_by_material(base_snapshots)
    target_groups = _group_by_material(target_snapshots)

    entry_diffs: list[dict[str, Any]] = []
    counts = {ADDED: 0, REMOVED: 0, MODIFIED: 0, UNCHANGED: 0}

    for material_id in sorted(set(base_groups) | set(target_groups)):
        old_list = base_groups.get(material_id, [])
        new_list = target_groups.get(material_id, [])
        for old, new in _align_material(old_list, new_list):
            changes = _field_changes(old, new)
            if old is None:
                status = ADDED
            elif new is None:
                status = REMOVED
            else:
                status = MODIFIED if changes else UNCHANGED

            if status == UNCHANGED and not include_unchanged:
                counts[UNCHANGED] += 1
                continue
            counts[status] += 1
            present = new or old
            assert present is not None
            entry_diffs.append(
                {
                    "material_id": material_id,
                    "kind": present.kind,
                    "sensitivity": present.sensitivity,
                    "status": status,
                    "changes": changes,
                    "base": _entry_ref(old, base.package_id),
                    "target": _entry_ref(new, target.package_id),
                }
            )

    field_changes_total = sum(len(d["changes"]) for d in entry_diffs)
    return {
        "kind": "package_diff",
        "base": {
            "package_id": base.package_id,
            "title": base.title,
            "status": base.status,
            "manifest_fingerprint": base.manifest_fingerprint,
            "supersedes_package_id": base.supersedes_package_id,
        },
        "target": {
            "package_id": target.package_id,
            "title": target.title,
            "status": target.status,
            "manifest_fingerprint": target.manifest_fingerprint,
            "supersedes_package_id": target.supersedes_package_id,
        },
        "summary": {
            "added": counts[ADDED],
            "removed": counts[REMOVED],
            "modified": counts[MODIFIED],
            "unchanged": counts[UNCHANGED],
            "field_changes": field_changes_total,
            "is_empty": counts[ADDED] == 0
            and counts[REMOVED] == 0
            and counts[MODIFIED] == 0,
        },
        "entries": entry_diffs,
    }
