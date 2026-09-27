"""复审包差异：空差异、逐字段变化、新增/删除、只读不触发复审、最小披露。"""
from __future__ import annotations

import unittest

from service_09252_006.domain.enums import (
    Decision,
    MaterialKind,
    PackageStatus,
    Role,
    Sensitivity,
)
from service_09252_006.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from service_09252_006.domain.models import EntrySnapshot
from service_09252_006.domain.package_diff import (
    DiffChange,
    diff_entry_snapshots,
    summarize_diff,
)
from tests.flow import complete_review, seal_new_package, upload_material
from tests.support import Harness


def snap(
    material_id: str,
    *,
    package_id: str = "pkg-x",
    version_id: str | None = None,
    sha256: str | None = None,
    kind: str = MaterialKind.SYLLABUS.value,
    sensitivity: str = Sensitivity.NORMAL.value,
    version_no: int = 1,
    supersedes_version_id: str | None = None,
    withdrawn: bool = False,
) -> EntrySnapshot:
    vid = version_id or f"ver-{material_id}"
    return EntrySnapshot(
        package_id=package_id,
        material_id=material_id,
        version_id=vid,
        sha256=sha256 or f"sha-{vid}",
        kind=kind,
        sensitivity=sensitivity,
        version_no=version_no,
        supersedes_version_id=supersedes_version_id,
        version_withdrawn=withdrawn,
    )


# --------------------------------------------------------------------- 纯函数
class DiffPureFunctionTests(unittest.TestCase):
    def test_identical_snapshots_yield_empty_diff(self) -> None:
        entries = [snap("mat-a"), snap("mat-b", kind=MaterialKind.FACULTY.value)]
        changes = diff_entry_snapshots(entries, [snap("mat-a"), snap("mat-b", kind=MaterialKind.FACULTY.value)])
        self.assertEqual(changes, [])
        summary = summarize_diff(changes)
        self.assertTrue(summary["empty"])
        self.assertEqual(summary["total_changes"], 0)

    def test_added_and_removed_point_to_source_package(self) -> None:
        base = [snap("mat-a", package_id="pkg-old")]
        target = [snap("mat-b", package_id="pkg-new")]
        changes = diff_entry_snapshots(base, target)
        self.assertEqual({c["change"] for c in changes}, {"added", "removed"})
        added = next(c for c in changes if c["change"] == "added")
        removed = next(c for c in changes if c["change"] == "removed")
        self.assertEqual(added["entry"]["package_id"], "pkg-new")
        self.assertEqual(added["entry"]["material_id"], "mat-b")
        self.assertEqual(removed["entry"]["package_id"], "pkg-old")
        self.assertEqual(removed["entry"]["material_id"], "mat-a")
        # 新增/删除行携带完整字段值（与封存清单同口径）
        self.assertEqual(added["entry"]["sha256"], "sha-ver-mat-b")
        self.assertEqual(removed["entry"]["kind"], MaterialKind.SYLLABUS.value)

    def test_modified_lists_every_changed_field(self) -> None:
        old = snap(
            "mat-a", package_id="pkg-old", version_id="ver-1",
            sha256="sha-1", sensitivity=Sensitivity.NORMAL.value,
        )
        new = snap(
            "mat-a", package_id="pkg-new", version_id="ver-2",
            sha256="sha-2", sensitivity=Sensitivity.SENSITIVE.value,
            version_no=2, supersedes_version_id="ver-1",
        )
        changes = diff_entry_snapshots([old], [new])
        self.assertEqual(len(changes), 1)
        row = changes[0]
        self.assertEqual(row["change"], DiffChange.MODIFIED.value)
        self.assertEqual(row["from"]["package_id"], "pkg-old")
        self.assertEqual(row["to"]["package_id"], "pkg-new")
        fields = {f["field"]: f for f in row["fields"]}
        self.assertEqual(set(fields), {"version_id", "sha256", "sensitivity"})
        self.assertEqual(fields["version_id"]["base"]["value"], "ver-1")
        self.assertEqual(fields["version_id"]["target"]["value"], "ver-2")
        self.assertEqual(fields["sha256"]["base"]["value"], "sha-1")
        self.assertEqual(fields["sha256"]["target"]["value"], "sha-2")
        self.assertEqual(fields["sensitivity"]["base"]["value"], "normal")
        self.assertEqual(fields["sensitivity"]["target"]["value"], "sensitive")
        # 未变化的 kind 不出现在字段列表
        self.assertNotIn("kind", fields)
        summary = summarize_diff(changes)
        self.assertFalse(summary["empty"])
        self.assertEqual(summary["modified"], 1)
        self.assertEqual(summary["modified_fields"]["version_id"], 1)
        self.assertEqual(summary["modified_fields"]["sha256"], 1)
        self.assertEqual(summary["modified_fields"]["sensitivity"], 1)


# ------------------------------------------------------------------ 服务用例
class PackageDiffServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.submitter = self.h.user("sub-a", Role.INSTITUTION_SUBMITTER)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.reviewer = self.h.user(
            "rev-1", Role.REVIEWER, institution_id="inst-ext"
        )
        self.other_admin = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )

    def tearDown(self) -> None:
        self.h.close()

    def _decide(self, package_id: str) -> None:
        complete_review(self.h, self.authority, self.reviewer, package_id)
        self.h.ctx.reviews.issue_decision(
            self.authority,
            package_id=package_id,
            decision=Decision.APPROVED.value,
        )

    def test_empty_diff_for_identical_rereview(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        self._decide(sealed.package_id)
        rereview = self.h.ctx.packages.create_package(
            self.admin, title="复审包",
            supersedes_package_id=sealed.package_id,
        )
        result = self.h.ctx.packages.build_package_diff(
            self.admin, package_id=rereview["package_id"]
        )
        self.assertTrue(result["summary"]["empty"])
        self.assertEqual(result["changes"], [])
        self.assertTrue(result["read_only"])
        # 差异结果指向两个来源版本
        self.assertEqual(result["base"]["package_id"], sealed.package_id)
        self.assertEqual(
            result["target"]["package_id"], rereview["package_id"]
        )
        self.assertEqual(
            result["base"]["manifest_fingerprint"],
            sealed.sealed["manifest_fingerprint"],
        )

    def test_added_late_material_and_removed_withdrawn_version(self) -> None:
        syllabus = upload_material(
            self.h, self.admin,
            kind=MaterialKind.SYLLABUS.value, data="大纲 v1".encode("utf-8"),
        )
        faculty = upload_material(
            self.h, self.admin,
            kind=MaterialKind.FACULTY.value, data="师资 v1".encode("utf-8"),
        )
        sealed = seal_new_package(
            self.h, self.admin, items=[syllabus, faculty]
        )
        self._decide(sealed.package_id)

        # 旧包决定后撤回师资版本：复审包不再复制该条目
        self.h.ctx.evidence.withdraw_version(
            self.admin,
            version_id=faculty.version["version_id"],
            reason="师资信息错误",
        )
        rereview = self.h.ctx.packages.create_package(
            self.admin, title="复审",
            supersedes_package_id=sealed.package_id,
        )
        # 后补考核材料
        late = upload_material(
            self.h, self.admin, kind=MaterialKind.ASSESSMENT.value,
            data="补充考核".encode("utf-8"), title="后补考核",
        )
        self.h.ctx.packages.add_entry(
            self.admin,
            package_id=rereview["package_id"],
            version_id=late.version["version_id"],
        )

        result = self.h.ctx.packages.build_package_diff(
            self.admin, package_id=rereview["package_id"]
        )
        changes = result["changes"]
        self.assertEqual(result["summary"]["added"], 1)
        self.assertEqual(result["summary"]["removed"], 1)
        added = next(c for c in changes if c["change"] == "added")
        removed = next(c for c in changes if c["change"] == "removed")
        self.assertEqual(
            added["entry"]["material_id"], late.material["material_id"]
        )
        self.assertEqual(added["entry"]["package_id"], rereview["package_id"])
        self.assertEqual(
            removed["entry"]["material_id"], faculty.material["material_id"]
        )
        self.assertEqual(removed["entry"]["package_id"], sealed.package_id)
        self.assertTrue(removed["entry"]["withdrawn"])

    def test_field_level_modified_for_upgraded_version(self) -> None:
        item = upload_material(self.h, self.admin, data="大纲 v1".encode("utf-8"))
        pkg1 = self.h.ctx.packages.create_package(self.admin, title="P1")
        self.h.ctx.packages.add_entry(
            self.admin, package_id=pkg1["package_id"],
            version_id=item.version["version_id"],
        )
        v2 = self.h.ctx.evidence.upload_version(
            self.admin,
            material_id=item.material["material_id"],
            data="大纲 v2".encode("utf-8"),
        )
        pkg2 = self.h.ctx.packages.create_package(self.admin, title="P2")
        self.h.ctx.packages.add_entry(
            self.admin, package_id=pkg2["package_id"],
            version_id=v2["version_id"],
        )
        result = self.h.ctx.packages.build_package_diff(
            self.admin,
            package_id=pkg2["package_id"],
            against_package_id=pkg1["package_id"],
        )
        self.assertEqual(result["summary"]["modified"], 1)
        row = result["changes"][0]
        self.assertEqual(row["change"], "modified")
        fields = {f["field"]: f for f in row["fields"]}
        self.assertEqual(
            fields["version_id"]["base"]["value"], item.version["version_id"]
        )
        self.assertEqual(fields["version_id"]["target"]["value"], v2["version_id"])
        self.assertEqual(
            fields["sha256"]["base"]["value"], item.version["sha256"].split(":", 1)[1]
        )
        self.assertEqual(
            fields["sha256"]["target"]["value"], v2["sha256"].split(":", 1)[1]
        )

    def test_kind_sensitivity_field_changes_via_repo_constructed_pair(self) -> None:
        item = upload_material(
            self.h, self.admin, data="反馈 v1".encode("utf-8"),
            kind=MaterialKind.ENTERPRISE_FEEDBACK.value,
            sensitivity=Sensitivity.SENSITIVE.value,
        )
        pkg1 = self.h.ctx.packages.create_package(self.admin, title="P1")
        pkg2 = self.h.ctx.packages.create_package(self.admin, title="P2")
        from service_09252_006.domain.models import PackageEntry

        self.h.repo.insert_entry(
            PackageEntry(
                entry_id="ent-manual-1", package_id=pkg1["package_id"],
                material_id=item.material["material_id"],
                version_id=item.version["version_id"],
                sha256=item.version["sha256"].split(":", 1)[1],
                kind=MaterialKind.ENTERPRISE_FEEDBACK.value,
                sensitivity=Sensitivity.NORMAL.value,
                added_at=self.h.clock.now_iso(),
            )
        )
        self.h.repo.insert_entry(
            PackageEntry(
                entry_id="ent-manual-2", package_id=pkg2["package_id"],
                material_id=item.material["material_id"],
                version_id=item.version["version_id"],
                sha256=item.version["sha256"].split(":", 1)[1],
                kind=MaterialKind.ENTERPRISE_FEEDBACK.value,
                sensitivity=Sensitivity.SENSITIVE.value,
                added_at=self.h.clock.now_iso(),
            )
        )
        result = self.h.ctx.packages.build_package_diff(
            self.admin,
            package_id=pkg2["package_id"],
            against_package_id=pkg1["package_id"],
        )
        fields = {f["field"]: f for f in result["changes"][0]["fields"]}
        self.assertEqual(set(fields), {"sensitivity"})
        self.assertEqual(fields["sensitivity"]["base"]["value"], "normal")
        self.assertEqual(fields["sensitivity"]["target"]["value"], "sensitive")

    def test_viewing_diff_is_read_only_and_creates_no_review(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        self._decide(sealed.package_id)
        rereview = self.h.ctx.packages.create_package(
            self.admin, title="复审包",
            supersedes_package_id=sealed.package_id,
        )
        late = upload_material(
            self.h, self.admin, kind=MaterialKind.ASSESSMENT.value,
            data="补充".encode("utf-8"),
        )
        self.h.ctx.packages.add_entry(
            self.admin, package_id=rereview["package_id"],
            version_id=late.version["version_id"],
        )

        packages_before = {p.package_id: p.status for p in self.h.repo.list_packages(None)}
        audits_before = len(self.h.repo.list_audit(limit=10_000))
        requests_before = len(
            self.h.repo.list_requests_by_package(rereview["package_id"])
        )

        result1 = self.h.ctx.packages.build_package_diff(
            self.authority, package_id=rereview["package_id"]
        )
        result2 = self.h.ctx.packages.build_package_diff(
            self.authority, package_id=rereview["package_id"]
        )

        # 没有新建任何包（复审任务）
        self.assertEqual(
            packages_before,
            {p.package_id: p.status for p in self.h.repo.list_packages(None)},
        )
        # 没有新增审计记录
        self.assertEqual(
            audits_before, len(self.h.repo.list_audit(limit=10_000))
        )
        # 没有产生评审分配
        self.assertEqual(
            requests_before,
            len(self.h.repo.list_requests_by_package(rereview["package_id"])),
        )
        # 重复查看结果一致且标记只读
        self.assertEqual(result1["changes"], result2["changes"])
        self.assertTrue(result1["read_only"])
        rerefresh = self.h.repo.get_package(rereview["package_id"])
        self.assertEqual(rerefresh.status, PackageStatus.DRAFT.value)

    def test_diff_defaults_to_supersedes_link_and_validates_inputs(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        self._decide(sealed.package_id)
        standalone = self.h.ctx.packages.create_package(self.admin, title="独立包")

        with self.assertRaises(ValidationError):
            self.h.ctx.packages.build_package_diff(
                self.admin, package_id=standalone["package_id"]
            )
        with self.assertRaises(ValidationError):
            self.h.ctx.packages.build_package_diff(
                self.admin,
                package_id=standalone["package_id"],
                against_package_id=standalone["package_id"],
            )
        with self.assertRaises(NotFoundError):
            self.h.ctx.packages.build_package_diff(
                self.admin,
                package_id=standalone["package_id"],
                against_package_id="pkg-missing",
            )
        with self.assertRaises(NotFoundError):
            self.h.ctx.packages.build_package_diff(
                self.admin, package_id="pkg-missing"
            )

    def test_diff_permission_and_redaction(self) -> None:
        feedback = upload_material(
            self.h, self.admin,
            kind=MaterialKind.ENTERPRISE_FEEDBACK.value,
            data="敏感反馈".encode("utf-8"),
            sensitivity=Sensitivity.SENSITIVE.value,
        )
        sealed = seal_new_package(self.h, self.admin, items=[feedback])
        self._decide(sealed.package_id)
        rereview = self.h.ctx.packages.create_package(
            self.admin, title="复审",
            supersedes_package_id=sealed.package_id,
        )
        late = upload_material(
            self.h, self.admin,
            kind=MaterialKind.ENTERPRISE_FEEDBACK.value,
            data="后补敏感反馈".encode("utf-8"),
            sensitivity=Sensitivity.SENSITIVE.value,
        )
        self.h.ctx.packages.add_entry(
            self.admin, package_id=rereview["package_id"],
            version_id=late.version["version_id"],
        )

        # 其他机构成员无权
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.build_package_diff(
                self.other_admin, package_id=rereview["package_id"]
            )

        # 本机构提交人：可见差异存在，但敏感条目的内容指纹被遮蔽
        result = self.h.ctx.packages.build_package_diff(
            self.submitter, package_id=rereview["package_id"]
        )
        added = next(c for c in result["changes"] if c["change"] == "added")
        self.assertTrue(added["entry"]["redacted"])
        self.assertIsNone(added["entry"]["sha256"])
        # 材料存在与类别仍可见
        self.assertEqual(added["entry"]["kind"], "enterprise_feedback")
        self.assertEqual(added["entry"]["material_id"], late.material["material_id"])

        # 本机构管理员可见完整指纹
        admin_result = self.h.ctx.packages.build_package_diff(
            self.admin, package_id=rereview["package_id"]
        )
        admin_added = next(
            c for c in admin_result["changes"] if c["change"] == "added"
        )
        self.assertFalse(admin_added["entry"]["redacted"])
        self.assertEqual(
            admin_added["entry"]["sha256"],
            late.version["sha256"].split(":", 1)[1],
        )

    def test_reviewer_assigned_to_base_but_not_target_is_denied(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        self._decide(sealed.package_id)
        rereview = self.h.ctx.packages.create_package(
            self.admin, title="复审",
            supersedes_package_id=sealed.package_id,
        )
        # reviewer 曾被分配到旧包（请求已完成），但从未分配到复审包：
        # 看不到新包，差异拒绝
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.build_package_diff(
                self.reviewer, package_id=rereview["package_id"]
            )
        # 权威机构跨机构只读放行
        result = self.h.ctx.packages.build_package_diff(
            self.authority, package_id=rereview["package_id"]
        )
        self.assertEqual(result["base"]["package_id"], sealed.package_id)


if __name__ == "__main__":
    unittest.main()
