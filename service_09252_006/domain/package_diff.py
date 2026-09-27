"""复审包差异：逐字段比较两个评审包版本的封存清单。

纯函数、只读：不触碰数据库、不推进任何状态机——查看差异绝不触发新的
复审任务。条目按 material_id 配对（同一逻辑材料跨版本比较），逐字段
列出新增（added）/删除（removed）/修改（modified），每条结果都指向
两个来源包（base/target 的 package_id 与具体 version_id）。

字段口径与封存清单指纹一致（version_id/sha256/kind/sensitivity）：
- version_id 或 sha256 变化记为 content_changed（材料被新版本替换，
  被撤回旧版本导致的删除另由 removed + withdrawn 表达）；
- kind/sensitivity 变化各自单列，便于复审人看清“只是元数据调整”。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import EntrySnapshot

# 逐字段比较的字段及其在差异结果中的稳定名称（与封存指纹同口径）
COMPARED_FIELDS: tuple[str, ...] = (
    "version_id",
    "sha256",
    "kind",
    "sensitivity",
)


class DiffChange(str, Enum):
    ADDED = "added"        # 新包新增的材料
    REMOVED = "removed"    # 新包删除的材料（含旧版本撤回导致不再带入）
    MODIFIED = "modified"  # 同一材料字段级变化
    UNCHANGED = "unchanged"


@dataclass
class PackageRef:
    """差异中指向一个来源包版本的最小信息。"""

    package_id: str
    status: str | None = None
    sealed_at: str | None = None
    manifest_fingerprint: str | None = None

    def to_dict(self) -> dict:
        return {
            "package_id": self.package_id,
            "status": self.status,
            "sealed_at": self.sealed_at,
            "manifest_fingerprint": self.manifest_fingerprint,
        }


def _snapshot_index(
    entries: list[EntrySnapshot],
) -> dict[str, EntrySnapshot]:
    """按 material_id 索引；同一包内同一材料只应有一个条目（UNIQUE 约束在
    package_id+version_id，这里对防御性重复保留后写入的一条并显式排序）。"""
    index: dict[str, EntrySnapshot] = {}
    for e in sorted(entries, key=lambda x: (x.material_id, x.version_id)):
        index[e.material_id] = e
    return index


def _entry_ref(e: EntrySnapshot) -> dict:
    """条目在差异中的完整字段引用（与封存清单同口径）。"""
    return {
        "package_id": e.package_id,
        "material_id": e.material_id,
        "version_id": e.version_id,
        "sha256": e.sha256,
        "kind": e.kind,
        "sensitivity": e.sensitivity,
        "version_no": e.version_no,
        "supersedes_version_id": e.supersedes_version_id,
        "withdrawn": e.version_withdrawn,
    }


def _field_changes(
    base: EntrySnapshot, target: EntrySnapshot
) -> list[dict]:
    changes: list[dict] = []
    for field in COMPARED_FIELDS:
        old_value = getattr(base, field)
        new_value = getattr(target, field)
        if old_value != new_value:
            changes.append(
                {
                    "field": field,
                    "base": {
                        "value": old_value,
                        "package_id": base.package_id,
                        "version_id": base.version_id,
                    },
                    "target": {
                        "value": new_value,
                        "package_id": target.package_id,
                        "version_id": target.version_id,
                    },
                }
            )
    return changes


def diff_entry_snapshots(
    base_entries: list[EntrySnapshot],
    target_entries: list[EntrySnapshot],
) -> list[dict]:
    """逐字段比较两份清单快照，返回稳定排序的差异行。

    每行：
      change=added/removed：含 entry（新/旧条目引用，带两个来源包标识）；
      change=modified：含 from/to（两端条目引用）与 fields（逐字段变化）；
      完全相同的材料不出现在结果中。
    """
    base_idx = _snapshot_index(base_entries)
    target_idx = _snapshot_index(target_entries)
    result: list[dict] = []

    for material_id in target_idx.keys() - base_idx.keys():
        e = target_idx[material_id]
        result.append(
            {
                "change": DiffChange.ADDED.value,
                "material_id": material_id,
                "entry": _entry_ref(e),
            }
        )

    for material_id in base_idx.keys() - target_idx.keys():
        e = base_idx[material_id]
        result.append(
            {
                "change": DiffChange.REMOVED.value,
                "material_id": material_id,
                "entry": _entry_ref(e),
            }
        )

    for material_id in base_idx.keys() & target_idx.keys():
        old = base_idx[material_id]
        new = target_idx[material_id]
        fields = _field_changes(old, new)
        if not fields:
            continue
        result.append(
            {
                "change": DiffChange.MODIFIED.value,
                "material_id": material_id,
                "from": _entry_ref(old),
                "to": _entry_ref(new),
                "fields": fields,
            }
        )

    result.sort(key=lambda row: (row["change"], row["material_id"]))
    return result


def summarize_diff(changes: list[dict]) -> dict:
    counts = {c.value: 0 for c in DiffChange if c is not DiffChange.UNCHANGED}
    modified_fields: dict[str, int] = {}
    for row in changes:
        counts[row["change"]] = counts.get(row["change"], 0) + 1
        if row["change"] == DiffChange.MODIFIED.value:
            for f in row["fields"]:
                modified_fields[f["field"]] = modified_fields.get(f["field"], 0) + 1
    return {
        "added": counts.get(DiffChange.ADDED.value, 0),
        "removed": counts.get(DiffChange.REMOVED.value, 0),
        "modified": counts.get(DiffChange.MODIFIED.value, 0),
        "modified_fields": modified_fields,
        "total_changes": len(changes),
        "empty": len(changes) == 0,
    }
