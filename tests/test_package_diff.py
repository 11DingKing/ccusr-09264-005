"""复审包差异：空差异、字段级新增/删除/修改、只读不触发复审、最小披露。"""
import unittest

from service_09252_006.domain.diff import diff_packages
from service_09252_006.domain.enums import (
    Decision,
    MaterialKind,
    PackageStatus,
    Role,
    Sensitivity,
)
from service_09252_006.domain.errors import (
    PermissionDeniedError,
    ValidationError,
)
from service_09252_006.domain.models import EntryVersionSnapshot, ReviewPackage
from tests.flow import complete_review, seal_new_package, upload_material
from tests.support import Harness


def _snapshot(
    *,
    material_id="mat-1",
    version_id="ver-1",
    sha256="a" * 64,
    kind=MaterialKind.SYLLABUS.value,
    sensitivity=Sensitivity.NORMAL.value,
    package_id="pkg-1",
    version_no=1,
    withdrawn=False,
) -> EntryVersionSnapshot:
    return EntryVersionSnapshot(
        entry_id=f"ent-{version_id}",
        package_id=package_id,
        material_id=material_id,
        version_id=version_id,
        sha256=sha256,
        kind=kind,
        sensitivity=sensitivity,
        added_at="2026-09-25T01:00:00+00:00",
        version_no=version_no,
        media_type="text/plain",
        size=1,
        version_withdrawn=withdrawn,
    )


def _package(package_id: str, *, supersedes=None) -> ReviewPackage:
    return ReviewPackage(
        package_id=package_id,
        institution_id="inst-a",
        title="包" + package_id,
        status=PackageStatus.DECIDED.value,
        created_by="admin-a",
        created_at="2026-09-25T01:00:00+00:00",
        sealed_at="2026-09-25T01:00:00+00:00",
        manifest_fingerprint="sha256:" + ("f" if package_id.endswith("1") else "e") * 64,
        decided_at="2026-09-25T02:00:00+00:00",
        decision=Decision.APPROVED.value,
        decision_note=None,
        review_fingerprint=None,
        supersedes_package_id=supersedes,
    )


class PureDiffTests(unittest.TestCase):
    def test_empty_diff_when_entries_identical(self) -> None:
        p1 = _package("pkg-1")
        p2 = _package("pkg-2", supersedes="pkg-1")
        snaps = [_snapshot()]
        result = diff_packages(
            p1, p2, base_snapshots=list(snaps), target_snapshots=list(snaps)
        )
        self.assertTrue(result["summary"]["is_empty"])
        self.assertEqual(result["summary"]["added"], 0)
        self.assertEqual(result["summary"]["removed"], 0)
        self.assertEqual(result["summary"]["modified"], 0)
        self.assertEqual(result["summary"]["unchanged"], 1)
        self.assertEqual(result["entries"], [])
        # 指向两个来源版本/包
        self.assertEqual(result["base"]["package_id"], "pkg-1")
        self.assertEqual(result["target"]["package_id"], "pkg-2")

    def test_empty_diff_include_unchanged_lists_every_field(self) -> None:
        p1 = _package("pkg-1")
        p2 = _package("pkg-2", supersedes="pkg-1")
        result = diff_packages(
            p1, p2, base_snapshots=[_snapshot()],
            target_snapshots=[_snapshot()], include_unchanged=True,
        )
        self.assertTrue(result["summary"]["is_empty"])
        self.assertEqual(len(result["entries"]), 1)
        row = result["entries"][0]
        self.assertEqual(row["status"], "unchanged")
        self.assertEqual(row["changes"], [])
        self.assertEqual(row["base"]["version_id"], "ver-1")
        self.assertEqual(row["target"]["version_id"], "ver-1")

    def test_field_level_added_and_removed(self) -> None:
        p1 = _package("pkg-1")
        p2 = _package("pkg-2", supersedes="pkg-1")
        result = diff_packages(
            p1, p2,
            base_snapshots=[_snapshot(material_id="mat-old")],
            target_snapshots=[_snapshot(material_id="mat-new")],
        )
        self.assertFalse(result["summary"]["is_empty"])
        statuses = {e["material_id"]: e["status"] for e in result["entries"]}
        self.assertEqual(statuses, {"mat-new": "added", "mat-old": "removed"})

        added = next(e for e in result["entries"] if e["material_id"] == "mat-new")
        self.assertIsNone(added["base"])
        self.assertEqual(added["target"]["package_id"], "pkg-2")
        self.assertEqual(
            {c["field"] for c in added["changes"]},
            {"version_id", "sha256", "kind", "sensitivity"},
        )
        self.assertTrue(all(c["base"] is None for c in added["changes"]))

        removed = next(e for e in result["entries"] if e["material_id"] == "mat-old")
        self.assertIsNone(removed["target"])
        self.assertEqual(removed["base"]["package_id"], "pkg-1")
        self.assertTrue(all(c["target"] is None for c in removed["changes"]))

    def test_field_level_modified_lists_each_changed_field(self) -> None:
        p1 = _package("pkg-1")
        p2 = _package("pkg-2", supersedes="pkg-1")
        old = _snapshot(
            version_id="ver-1", sha256="a" * 64,
            kind=MaterialKind.SYLLABUS.value,
            sensitivity=Sensitivity.NORMAL.value,
        )
        new = _snapshot(
            version_id="ver-2", sha256="b" * 64,
            kind=MaterialKind.SYLLABUS.value,
            sensitivity=Sensitivity.SENSITIVE.value,
        )
        result = diff_packages(
            p1, p2, base_snapshots=[old], target_snapshots=[new]
        )
        self.assertEqual(result["summary"]["modified"], 1)
        row = result["entries"][0]
        self.assertEqual(row["status"], "modified")
        changes = {c["field"]: c for c in row["changes"]}
        self.assertEqual(set(changes), {"version_id", "sha256", "sensitivity"})
        self.assertEqual(changes["version_id"]["base"], "ver-1")
        self.assertEqual(changes["version_id"]["target"], "ver-2")
        self.assertEqual(changes["sha256"]["base"], "a" * 64)
        self.assertEqual(changes["sha256"]["target"], "b" * 64)
        self.assertEqual(changes["sensitivity"]["base"], "normal")
        self.assertEqual(changes["sensitivity"]["target"], "sensitive")
        # kind 未变不出现在 changes 中；两侧来源坐标完整
        self.assertEqual(row["base"]["package_id"], "pkg-1")
        self.assertEqual(row["target"]["package_id"], "pkg-2")

    def test_reverse_direction_flips_added_and_removed(self) -> None:
        p1 = _package("pkg-1")
        p2 = _package("pkg-2", supersedes="pkg-1")
        forward = diff_packages(
            p1, p2,
            base_snapshots=[], target_snapshots=[_snapshot()],
        )
        reverse = diff_packages(
            p2, p1,
            base_snapshots=[_snapshot(package_id="pkg-2")],
            target_snapshots=[],
        )
        self.assertEqual(forward["summary"]["added"], 1)
        self.assertEqual(reverse["summary"]["removed"], 1)


class PackageDiffServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.submitter = self.h.user(
            "sub-a", Role.INSTITUTION_SUBMITTER
        )
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.reviewer = self.h.user(
            "rev-1", Role.REVIEWER, institution_id="inst-ext"
        )
        self.outsider = self.h.user(
            "out", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )

    def tearDown(self) -> None:
        self.h.close()

    def _decided_package(self):
        sealed = seal_new_package(self.h, self.admin)
        complete_review(
            self.h, self.authority, self.reviewer, sealed.package_id
        )
        self.h.ctx.reviews.issue_decision(
            self.authority,
            package_id=sealed.package_id,
            decision=Decision.APPROVED.value,
        )
        return sealed

    def _rereview(self, predecessor_id: str, *, seal: bool = True):
        re = self.h.ctx.packages.create_package(
            self.admin, title="复审包", supersedes_package_id=predecessor_id
        )
        if seal:
            self.h.ctx.packages.seal_package(
                self.admin, package_id=re["package_id"]
            )
        return re

    def test_empty_diff_for_identical_rereview_package(self) -> None:
        sealed = self._decided_package()
        re = self._rereview(sealed.package_id)
        result = self.h.ctx.packages.build_package_diff(
            self.admin,
            target_package_id=re["package_id"],
            base_package_id=sealed.package_id,
        )
        self.assertTrue(result["summary"]["is_empty"], result["entries"])
        self.assertEqual(result["summary"]["added"], 0)
        self.assertEqual(result["summary"]["removed"], 0)
        self.assertEqual(result["summary"]["modified"], 0)
        self.assertEqual(result["summary"]["unchanged"], 2)
        self.assertEqual(result["entries"], [])

    def test_added_late_material_field_level_diff(self) -> None:
        sealed = self._decided_package()
        re = self.h.ctx.packages.create_package(
            self.admin, title="复审包",
            supersedes_package_id=sealed.package_id,
        )
        late = upload_material(
            self.h, self.admin, kind=MaterialKind.ASSESSMENT.value,
            data="补充考核说明".encode("utf-8"), title="后补考核",
        )
        self.h.ctx.packages.add_entry(
            self.admin, package_id=re["package_id"],
            version_id=late.version["version_id"],
        )
        self.h.ctx.packages.seal_package(
            self.admin, package_id=re["package_id"]
        )
        result = self.h.ctx.packages.build_package_diff(
            self.admin,
            target_package_id=re["package_id"],
            base_package_id=sealed.package_id,
            include_unchanged=True,
        )
        self.assertFalse(result["summary"]["is_empty"])
        self.assertEqual(result["summary"]["added"], 1)
        self.assertEqual(result["summary"]["unchanged"], 2)
        added = [e for e in result["entries"] if e["status"] == "added"]
        self.assertEqual(len(added), 1)
        row = added[0]
        self.assertIsNone(row["base"])
        self.assertEqual(row["target"]["package_id"], re["package_id"])
        self.assertEqual(row["target"]["version_id"], late.version["version_id"])
        self.assertEqual(
            row["target"]["sha256"], late.version["sha256"].removeprefix("sha256:")
            if late.version["sha256"].startswith("sha256:")
            else late.version["sha256"]
        )
        changed_fields = {c["field"] for c in row["changes"]}
        self.assertEqual(
            changed_fields, {"version_id", "sha256", "kind", "sensitivity"}
        )

    def test_removed_withdrawn_version_field_level_diff(self) -> None:
        sealed = self._decided_package()
        target_version = sealed.items[0].version["version_id"]
        self.h.ctx.evidence.withdraw_version(
            self.admin, version_id=target_version, reason="发现错误"
        )
        re = self._rereview(sealed.package_id)
        result = self.h.ctx.packages.build_package_diff(
            self.admin,
            target_package_id=re["package_id"],
            base_package_id=sealed.package_id,
        )
        self.assertEqual(result["summary"]["removed"], 1)
        row = next(e for e in result["entries"] if e["status"] == "removed")
        self.assertIsNone(row["target"])
        self.assertEqual(row["base"]["package_id"], sealed.package_id)
        self.assertEqual(row["base"]["version_id"], target_version)
        self.assertTrue(row["base"]["version_withdrawn"])

    def test_modified_when_version_replaced(self) -> None:
        sealed = self._decided_package()
        syllabus = sealed.items[0]
        v1_id = syllabus.version["version_id"]
        v2 = self.h.ctx.evidence.upload_version(
            self.admin,
            material_id=syllabus.material["material_id"],
            data="大纲 v2 修订".encode("utf-8"),
        )
        # 撤回旧版本：复审包不复制 v1，改为加入 v2 —— 同一材料版本被替换
        self.h.ctx.evidence.withdraw_version(
            self.admin, version_id=v1_id, reason="旧版作废"
        )
        re = self.h.ctx.packages.create_package(
            self.admin, title="复审包",
            supersedes_package_id=sealed.package_id,
        )
        self.h.ctx.packages.add_entry(
            self.admin, package_id=re["package_id"],
            version_id=v2["version_id"],
        )
        self.h.ctx.packages.seal_package(
            self.admin, package_id=re["package_id"]
        )
        result = self.h.ctx.packages.build_package_diff(
            self.admin,
            target_package_id=re["package_id"],
            base_package_id=sealed.package_id,
        )
        self.assertEqual(result["summary"]["modified"], 1)
        self.assertEqual(result["summary"]["added"], 0)
        self.assertEqual(result["summary"]["removed"], 0)
        row = next(e for e in result["entries"] if e["status"] == "modified")
        fields = {c["field"]: c for c in row["changes"]}
        self.assertEqual(fields["version_id"]["base"], v1_id)
        self.assertEqual(fields["version_id"]["target"], v2["version_id"])
        self.assertIn("sha256", fields)
        self.assertEqual(row["base"]["package_id"], sealed.package_id)
        self.assertEqual(row["target"]["package_id"], re["package_id"])

    def test_diff_is_read_only_and_creates_no_review_task(self) -> None:
        sealed = self._decided_package()
        re = self._rereview(sealed.package_id)

        before_requests = [
            r.request_id
            for r in self.h.repo.list_requests_by_package(sealed.package_id)
        ]
        before_audit = self.h.repo.list_audit(limit=1000)
        before_packages = {p.package_id for p in self.h.repo.list_packages(None)}

        result1 = self.h.ctx.packages.build_package_diff(
            self.authority,
            target_package_id=re["package_id"],
            base_package_id=sealed.package_id,
        )
        # 重复查看结果确定一致
        result2 = self.h.ctx.packages.build_package_diff(
            self.authority,
            target_package_id=re["package_id"],
            base_package_id=sealed.package_id,
        )
        self.assertEqual(result1["summary"], result2["summary"])

        after_requests = [
            r.request_id
            for r in self.h.repo.list_requests_by_package(sealed.package_id)
        ]
        after_audit = self.h.repo.list_audit(limit=1000)
        after_packages = {p.package_id for p in self.h.repo.list_packages(None)}

        self.assertEqual(before_requests, after_requests)
        self.assertEqual(len(before_audit), len(after_audit))
        self.assertEqual(before_packages, after_packages)
        # 两包状态未被查看动作推进
        self.assertEqual(
            self.h.repo.get_package(re["package_id"]).status,
            PackageStatus.SEALED.value,
        )
        self.assertEqual(
            self.h.repo.get_package(sealed.package_id).status,
            PackageStatus.DECIDED.value,
        )

    def test_diff_rejects_packages_outside_review_chain(self) -> None:
        sealed = self._decided_package()
        other = self.h.ctx.packages.create_package(self.admin, title="无关包")
        with self.assertRaises(ValidationError):
            self.h.ctx.packages.build_package_diff(
                self.admin,
                target_package_id=other["package_id"],
                base_package_id=sealed.package_id,
            )

    def test_diff_denied_for_other_institution(self) -> None:
        sealed = self._decided_package()
        re = self._rereview(sealed.package_id)
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.build_package_diff(
                self.outsider,
                target_package_id=re["package_id"],
                base_package_id=sealed.package_id,
            )

    def test_diff_redacts_sensitive_sha256_for_submitter(self) -> None:
        sealed = self._decided_package()
        re = self._rereview(sealed.package_id)
        result = self.h.ctx.packages.build_package_diff(
            self.submitter,
            target_package_id=re["package_id"],
            base_package_id=sealed.package_id,
            include_unchanged=True,
        )
        sensitive_rows = [
            e for e in result["entries"]
            if e["sensitivity"] == Sensitivity.SENSITIVE.value
        ]
        self.assertEqual(len(sensitive_rows), 1)
        row = sensitive_rows[0]
        self.assertTrue(row["redacted"])
        self.assertIsNone(row["base"]["sha256"])
        self.assertIsNone(row["target"]["sha256"])
        self.assertTrue(row["base"]["sha256_redacted"])
        # 非敏感条目不遮蔽
        normal = next(
            e for e in result["entries"]
            if e["sensitivity"] == Sensitivity.NORMAL.value
        )
        self.assertFalse(normal["redacted"])
        self.assertIsNotNone(normal["base"]["sha256"])


if __name__ == "__main__":
    unittest.main()
