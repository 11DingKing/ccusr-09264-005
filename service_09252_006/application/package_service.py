"""评审包服务：组包、封存、后补材料触发复审。

核心不变量：
- draft 包只能引用“当前未撤回”的版本；封存后清单指纹固定，
  此后材料撤回/新版本都不改变历史包——“某次评审看到了什么”可证；
- 后补（新上传/恢复）的材料不能塞进已封存或已决定的包，
  只能基于旧包创建新的复审包（supersedes 链）；
- 封存是幂等的：重复封存返回同一指纹。
"""
from __future__ import annotations

from ..domain.disclosure import DisclosureContext, redact_entry
from ..domain.enums import PackageStatus, Role
from ..domain.errors import (
    ConflictError,
    ImmutabilityError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.fingerprint import manifest_fingerprint
from ..domain.models import EntrySnapshot, PackageEntry, ReviewPackage, User
from ..domain.package_diff import (
    PackageRef,
    diff_entry_snapshots,
    summarize_diff,
)
from .base import Service, require_roles


class PackageService(Service):
    def create_package(
        self,
        actor: User,
        *,
        title: str,
        supersedes_package_id: str | None = None,
        package_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)
        if not title.strip():
            raise ValidationError("评审包标题不能为空")

        def work() -> dict:
            pid = package_id or self.ids.new_id("pkg")
            if self.repo.get_package(pid) is not None:
                return self._package_dict(self.repo.get_package(pid))

            predecessor: ReviewPackage | None = None
            if supersedes_package_id is not None:
                predecessor = self.repo.get_package(supersedes_package_id)
                if predecessor is None:
                    raise NotFoundError(
                        "被复审的原评审包不存在",
                        details={"supersedes_package_id": supersedes_package_id},
                    )
                if predecessor.institution_id != actor.institution_id and not actor.has_role(
                    Role.QUALITY_AUTHORITY
                ):
                    raise PermissionDeniedError("不能为其他机构创建复审包")
                if predecessor.status != PackageStatus.DECIDED.value:
                    raise ConflictError(
                        "仅已签发结论的评审包可发起复审",
                        details={"predecessor_status": predecessor.status},
                    )

            package = ReviewPackage(
                package_id=pid,
                institution_id=actor.institution_id or "",
                title=title.strip(),
                status=PackageStatus.DRAFT.value,
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
                sealed_at=None,
                manifest_fingerprint=None,
                decided_at=None,
                decision=None,
                decision_note=None,
                review_fingerprint=None,
                supersedes_package_id=supersedes_package_id,
            )
            self.repo.insert_package(package)
            detail = {}
            if predecessor is not None:
                # 复审包默认带上原包中【未撤回】的条目，撤回的条目不复制
                detail["copied_entries"] = self._copy_live_entries(actor, predecessor, pid)
                detail["supersedes_package_id"] = supersedes_package_id
            self.audit(
                actor.user_id, "package.created",
                package_id=pid, institution_id=package.institution_id, detail=detail,
            )
            return self._package_dict(self.repo.get_package(pid))

        return self.idempotent(idempotency_key, work)

    def _copy_live_entries(self, actor: User, predecessor: ReviewPackage, new_pid: str) -> int:
        count = 0
        for entry in predecessor.entries:
            version = self.repo.get_version(entry.version_id)
            if version is None or version.withdrawn:
                continue
            new_entry = PackageEntry(
                entry_id=self.ids.new_id("ent"),
                package_id=new_pid,
                material_id=entry.material_id,
                version_id=entry.version_id,
                sha256=entry.sha256,
                kind=entry.kind,
                sensitivity=entry.sensitivity,
                added_at=self.clock.now_iso(),
            )
            self.repo.insert_entry(new_entry)
            count += 1
        return count

    def add_entry(
        self,
        actor: User,
        *,
        package_id: str,
        version_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.INSTITUTION_SUBMITTER)

        def work() -> dict:
            package = self.repo.get_package(package_id)
            if package is None:
                raise NotFoundError("评审包不存在")
            if package.institution_id != actor.institution_id:
                raise PermissionDeniedError("只能向本机构评审包添加材料")
            if not package.is_mutable():
                raise ImmutabilityError(
                    "评审包已封存，后补材料只能发起新的复审请求",
                    details={"package_id": package_id, "status": package.status},
                )
            version = self.repo.get_version(version_id)
            if version is None:
                raise NotFoundError("材料版本不存在")
            if version.institution_id != actor.institution_id:
                raise PermissionDeniedError("不能把其他机构材料加入评审包")
            if version.withdrawn:
                raise ConflictError("该版本已撤回，不能进入评审包")

            if self.repo.entry_exists(package_id, version_id):
                return {"package_id": package_id, "version_id": version_id, "replayed": True}

            entry = PackageEntry(
                entry_id=self.ids.new_id("ent"),
                package_id=package_id,
                material_id=version.material_id,
                version_id=version.version_id,
                sha256=version.sha256,
                kind=self.repo.get_material(version.material_id).kind,
                sensitivity=self.repo.get_material(version.material_id).sensitivity,
                added_at=self.clock.now_iso(),
            )
            self.repo.insert_entry(entry)
            self.audit(
                actor.user_id, "package.entry_added",
                package_id=package_id, institution_id=package.institution_id,
                detail={"version_id": version_id, "entry_id": entry.entry_id},
            )
            return {"package_id": package_id, "version_id": version_id, "entry_id": entry.entry_id}

        return self.idempotent(idempotency_key, work)

    def seal_package(
        self,
        actor: User,
        *,
        package_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)

        def work() -> dict:
            package = self.repo.get_package(package_id)
            if package is None:
                raise NotFoundError("评审包不存在")
            if package.institution_id != actor.institution_id and not actor.has_role(
                Role.QUALITY_AUTHORITY
            ):
                raise PermissionDeniedError("只能封存本机构评审包")

            if package.status == PackageStatus.SEALED.value:
                return self._package_dict(package, replayed=True)
            if package.status != PackageStatus.DRAFT.value:
                raise ImmutabilityError(
                    "评审包当前状态不能封存",
                    details={"status": package.status},
                )
            if not package.entries:
                raise ValidationError("评审包没有任何材料，不能封存")

            # 封存前最后一次撤回拦截（与 add_entry 构成双重检查）
            for entry in package.entries:
                version = self.repo.get_version(entry.version_id)
                if version is None or version.withdrawn:
                    raise ConflictError(
                        "清单中存在已撤回版本，请移除后再封存",
                        details={"version_id": entry.version_id},
                    )

            sealed_at = self.clock.now_iso()
            fingerprint = manifest_fingerprint(
                package.package_id,
                package.institution_id,
                [
                    {
                        "material_id": e.material_id,
                        "version_id": e.version_id,
                        "sha256": e.sha256,
                        "kind": e.kind,
                        "sensitivity": e.sensitivity,
                    }
                    for e in package.entries
                ],
                sealed_at,
            )
            ok = self.repo.transition_package_status(
                package_id,
                PackageStatus.DRAFT.value,
                PackageStatus.SEALED.value,
                sealed_at=sealed_at,
                manifest_fingerprint=fingerprint,
            )
            if not ok:
                # 并发：另一事务已推进状态
                fresh = self.repo.get_package(package_id)
                if fresh.status == PackageStatus.SEALED.value:
                    return self._package_dict(fresh, replayed=True)
                raise ConflictError("评审包状态已被其他操作改变，请重试")

            sealed = self.repo.get_package(package_id)
            self.audit(
                actor.user_id, "package.sealed",
                package_id=package_id, institution_id=package.institution_id,
                detail={"manifest_fingerprint": fingerprint,
                        "entries": len(package.entries)},
            )
            return self._package_dict(sealed)

        return self.idempotent(idempotency_key, work)

    # -------------------------------------------------------------- 差异视图
    def build_package_diff(
        self,
        actor: User,
        *,
        package_id: str,
        against_package_id: str | None = None,
    ) -> dict:
        """只读查看复审包差异：target 包相对 base 来源包逐字段的
        新增/删除/修改。

        只读保证：不开事务、不写 audit/idempotency、不创建复审包、不推进
        任何状态机——查看差异绝不会触发新的复审任务。base 缺省取 target
        的 supersedes_package_id。敏感条目沿用最小披露：无权限时遮蔽
        sha256 内容指纹与版本链细节。
        """
        target = self.repo.get_package(package_id)
        if target is None:
            raise NotFoundError("评审包不存在")
        base_id = against_package_id or target.supersedes_package_id
        if not base_id:
            raise ValidationError(
                "未指定被比较的来源包，且该包没有 supersedes_package_id",
                details={"package_id": package_id},
            )
        if base_id == package_id:
            raise ValidationError("不能比较评审包与其自身")
        base = self.repo.get_package(base_id)
        if base is None:
            raise NotFoundError(
                "被比较的来源评审包不存在",
                details={"against_package_id": base_id},
            )
        self._require_view_package(actor, target)
        self._require_view_package(actor, base)
        if (
            base.institution_id != target.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
        ):
            raise PermissionDeniedError("只能比较同一机构的评审包")

        base_snapshots = self.repo.list_entry_snapshots(base_id)
        target_snapshots = self.repo.list_entry_snapshots(package_id)
        changes = diff_entry_snapshots(base_snapshots, target_snapshots)
        # 计数在遮蔽前计算：数量不泄露内容，遮蔽只影响字段值
        summary = summarize_diff(changes)
        changes = self._redact_diff_changes(
            actor, base, base_snapshots, target, target_snapshots, changes
        )
        return {
            "base": PackageRef(
                base.package_id,
                base.status,
                base.sealed_at,
                base.manifest_fingerprint,
            ).to_dict(),
            "target": PackageRef(
                target.package_id,
                target.status,
                target.sealed_at,
                target.manifest_fingerprint,
            ).to_dict(),
            "summary": summary,
            "changes": changes,
            "read_only": True,
            "viewer": actor.user_id,
        }

    def _require_view_package(self, actor: User, package: ReviewPackage) -> None:
        """与包视图一致的查看权限：本机构 / 权威 / 审计 / 曾被分配的评审人。"""
        is_assigned = actor.has_role(Role.REVIEWER) and any(
            r.reviewer_id == actor.user_id
            for r in self.repo.list_requests_by_package(package.package_id)
        )
        if (
            actor.institution_id != package.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
            and not is_assigned
        ):
            raise PermissionDeniedError("不能查看其他机构评审包")

    def _redact_diff_changes(
        self,
        actor: User,
        base: ReviewPackage,
        base_snapshots: list[EntrySnapshot],
        target: ReviewPackage,
        target_snapshots: list[EntrySnapshot],
        changes: list[dict],
    ) -> list[dict]:
        """按最小披露遮蔽差异中无权查看的内容指纹与版本链细节。"""
        active = {
            r.package_id
            for r in self.repo.list_active_requests_by_reviewer(actor.user_id)
        }
        ctx = DisclosureContext(actor, active)
        visible_base = {
            s.material_id: bool(ctx.can_see_entry(s, base))
            for s in base_snapshots
        }
        visible_target = {
            s.material_id: bool(ctx.can_see_entry(s, target))
            for s in target_snapshots
        }

        def mask_ref(ref: dict, visible: bool) -> dict:
            if visible:
                return {**ref, "redacted": False}
            masked = dict(ref)
            # 与 redact_entry 同口径：仅保留材料存在/类别/敏感度，
            # 不泄露内容摘要、版本序号与版本链指向
            masked["sha256"] = None
            masked["version_no"] = None
            masked["supersedes_version_id"] = None
            masked["withdrawn"] = None
            masked["redacted"] = True
            return masked

        for row in changes:
            mid = row["material_id"]
            if row["change"] == "added":
                row["entry"] = mask_ref(row["entry"], visible_target.get(mid, False))
            elif row["change"] == "removed":
                row["entry"] = mask_ref(row["entry"], visible_base.get(mid, False))
            else:
                row["from"] = mask_ref(row["from"], visible_base.get(mid, False))
                row["to"] = mask_ref(row["to"], visible_target.get(mid, False))
                for field_change in row["fields"]:
                    if field_change["field"] != "sha256":
                        continue
                    if not visible_base.get(mid, False):
                        field_change["base"]["value"] = None
                        field_change["base"]["redacted"] = True
                    if not visible_target.get(mid, False):
                        field_change["target"]["value"] = None
                        field_change["target"]["redacted"] = True
        return changes

    # -------------------------------------------------------------- 视图
    def build_package_view(self, actor: User, package_id: str) -> dict:
        """按最小披露返回包视图；敏感条目对无权用户做遮蔽。

        曾被分配到该包的评审人（即使请求已取消/拒绝）可打开视图看到
        非敏感条目与“存在敏感条目”的事实，但敏感内容按当前有效分配遮蔽；
        与该包毫无关系的外部机构用户直接拒绝。
        """
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError("评审包不存在")
        is_assigned = (
            actor.has_role(Role.REVIEWER)
            and any(
                r.reviewer_id == actor.user_id
                for r in self.repo.list_requests_by_package(package_id)
            )
        )
        if (
            actor.institution_id != package.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
            and not is_assigned
        ):
            raise PermissionDeniedError("不能查看其他机构评审包")

        active = {
            r.package_id
            for r in self.repo.list_active_requests_by_reviewer(actor.user_id)
        }
        ctx = DisclosureContext(actor, active)

        visible_entries = []
        hidden_count = 0
        for entry in package.entries:
            can_see = ctx.can_see_entry(entry, package)
            if not can_see:
                hidden_count += 1
            visible_entries.append(redact_entry(entry, can_see))

        view = self._package_dict(package)
        view["entries"] = visible_entries
        view["redacted_entries"] = hidden_count
        view["viewer"] = actor.user_id
        return view

    def download_entry(
        self, actor: User, *, package_id: str, version_id: str
    ) -> tuple[dict, bytes, str]:
        """通过评审包条目下载内容字节，强制走最小披露授权。

        返回 (版本描述, 字节, media_type)。评审人只可下载其仍有效分配
        所在包的敏感反馈；请求一旦取消，授权即时消失。
        """
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError("评审包不存在")
        entry = next(
            (e for e in package.entries if e.version_id == version_id), None
        )
        if entry is None:
            raise NotFoundError("该材料版本不在评审包中")
        active = {
            r.package_id
            for r in self.repo.list_active_requests_by_reviewer(actor.user_id)
        }
        ctx = DisclosureContext(actor, active)
        if not ctx.can_see_entry(entry, package):
            raise PermissionDeniedError("无权下载该材料（最小披露限制）")
        version = self.repo.get_version(version_id)
        blob = self.repo.get_blob(entry.sha256)
        if version is None or blob is None:
            raise NotFoundError("内容缺失，无法提供")
        return {
            "version_id": version.version_id,
            "material_id": version.material_id,
            "sha256": "sha256:" + version.sha256,
            "media_type": version.media_type,
            "size": version.size,
        }, blob.data, version.media_type

    def list_packages(self, actor: User) -> list[dict]:
        if actor.has_role(Role.AUDITOR) or actor.has_role(Role.QUALITY_AUTHORITY):
            packages = self.repo.list_packages(None)
        else:
            packages = self.repo.list_packages(actor.institution_id)
        return [self._package_dict(p) for p in packages]

    @staticmethod
    def _package_dict(p: ReviewPackage, *, replayed: bool = False) -> dict:
        return {
            "package_id": p.package_id,
            "institution_id": p.institution_id,
            "title": p.title,
            "status": p.status,
            "created_by": p.created_by,
            "created_at": p.created_at,
            "sealed_at": p.sealed_at,
            "manifest_fingerprint": p.manifest_fingerprint,
            "decided_at": p.decided_at,
            "decision": p.decision,
            "decision_note": p.decision_note,
            "review_fingerprint": p.review_fingerprint,
            "supersedes_package_id": p.supersedes_package_id,
            "entry_count": len(p.entries),
            "replayed": replayed,
        }
