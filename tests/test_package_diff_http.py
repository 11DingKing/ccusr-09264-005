"""HTTP 端到端：复审包差异端点（只读、参数校验、不触发复审任务）。"""
import base64
import unittest

from service_09252_006.api.http_api import HttpApiServer
from service_09252_006.domain.enums import Decision, MaterialKind
from tests.support import Harness
from tests.test_http_api import ApiClient


class PackageDiffHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.server = HttpApiServer(
            self.h.ctx, host="127.0.0.1", port=0, bootstrap_token="boot-secret"
        )
        self.server.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"
        self.boot = ApiClient(self.base, bootstrap="boot-secret")

    def tearDown(self) -> None:
        self.server.stop()
        self.h.close()

    def _user(self, user_id, roles, institution_id, token):
        status, _ = self.boot.request(
            "POST", "/v1/admin/users",
            {"user_id": user_id, "roles": roles,
             "institution_id": institution_id},
        )
        self.assertEqual(status, 201)
        status, _ = self.boot.request(
            "POST", "/v1/admin/tokens",
            {"user_id": user_id, "token": token},
        )
        self.assertEqual(status, 201)
        return ApiClient(self.base, token=token)

    def _request_ids(self, client, package_id):
        status, body = client.request(
            "GET", f"/v1/packages/{package_id}/requests"
        )
        self.assertEqual(status, 200, body)
        return [r["request_id"] for r in body["requests"]]

    def _decide_package(self, admin, authority, rev_client, reviewer_id):
        status, mat = admin.request(
            "POST", "/v1/materials",
            {"kind": "syllabus", "title": "大纲"},
        )
        self.assertEqual(status, 201)
        status, ver = admin.request(
            "POST", f"/v1/materials/{mat['material_id']}/versions",
            {"content_base64": base64.b64encode("大纲 v1".encode()).decode("ascii")},
        )
        self.assertEqual(status, 201)
        status, pkg = admin.request("POST", "/v1/packages", {"title": "原包"})
        self.assertEqual(status, 201)
        pid = pkg["package_id"]
        status, _ = admin.request(
            "POST", f"/v1/packages/{pid}/entries",
            {"version_id": ver["version_id"]},
        )
        self.assertEqual(status, 201)
        status, _ = admin.request("POST", f"/v1/packages/{pid}/seal", {})
        self.assertEqual(status, 200)
        status, req = authority.request(
            "POST", f"/v1/packages/{pid}/assignments",
            {"reviewer_id": reviewer_id},
        )
        self.assertEqual(status, 201)
        rid = req["request_id"]
        status, _ = rev_client.request(
            "POST", f"/v1/requests/{rid}/respond", {"accept": True}
        )
        self.assertEqual(status, 200)
        status, _ = rev_client.request(
            "POST", f"/v1/requests/{rid}/verdict", {"verdict": "approve"}
        )
        self.assertEqual(status, 200)
        status, _ = authority.request(
            "POST", f"/v1/packages/{pid}/decision",
            {"decision": Decision.APPROVED.value},
        )
        self.assertEqual(status, 200)
        return pid

    def test_diff_endpoint_empty_then_added_creates_no_task(self) -> None:
        admin = self._user("admin-a", ["institution_admin"], "inst-a", "tok-admin")
        authority = self._user("auth", ["quality_authority"], None, "tok-auth")
        rev_client = self._user("rev-1", ["reviewer"], "inst-ext", "tok-rev")
        pid = self._decide_package(admin, authority, rev_client, "rev-1")

        # 复审包：仅自动带入旧条目 —— 空差异
        status, re_pkg = admin.request(
            "POST", "/v1/packages",
            {"title": "复审包", "supersedes_package_id": pid},
        )
        self.assertEqual(status, 201)
        rid_pkg = re_pkg["package_id"]

        before = self._request_ids(admin, rid_pkg)
        status, body = admin.request(
            "GET", f"/v1/packages/{rid_pkg}/diff?base_package_id={pid}"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["kind"], "package_diff")
        self.assertEqual(body["base"]["package_id"], pid)
        self.assertEqual(body["target"]["package_id"], rid_pkg)
        self.assertTrue(body["summary"]["is_empty"])
        self.assertEqual(body["entries"], [])
        # 查看差异不产生新的评审请求/复审任务
        self.assertEqual(self._request_ids(admin, rid_pkg), before)

        # 后补材料进入复审包并封存后：字段级 added
        status, late_mat = admin.request(
            "POST", "/v1/materials",
            {"kind": MaterialKind.ASSESSMENT.value, "title": "后补考核"},
        )
        self.assertEqual(status, 201)
        status, late_ver = admin.request(
            "POST", f"/v1/materials/{late_mat['material_id']}/versions",
            {"content_base64": base64.b64encode("补充考核".encode()).decode("ascii")},
        )
        self.assertEqual(status, 201)
        status, _ = admin.request(
            "POST", f"/v1/packages/{rid_pkg}/entries",
            {"version_id": late_ver["version_id"]},
        )
        self.assertEqual(status, 201)
        status, _ = admin.request("POST", f"/v1/packages/{rid_pkg}/seal", {})
        self.assertEqual(status, 200)

        status, body = admin.request(
            "GET", f"/v1/packages/{rid_pkg}/diff?base_package_id={pid}"
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["summary"]["is_empty"])
        self.assertEqual(body["summary"]["added"], 1)
        added = [e for e in body["entries"] if e["status"] == "added"]
        self.assertEqual(len(added), 1)
        self.assertIsNone(added[0]["base"])
        self.assertEqual(added[0]["target"]["package_id"], rid_pkg)
        self.assertEqual(added[0]["target"]["version_id"], late_ver["version_id"])
        self.assertEqual(body["base"]["package_id"], pid)

        # 复审包仍未被分配：查看差异没有触发复审
        self.assertEqual(self._request_ids(admin, rid_pkg), [])
        # 原包请求数也不变
        self.assertEqual(len(self._request_ids(admin, pid)), 1)

    def test_diff_requires_base_package_id(self) -> None:
        admin = self._user("admin-a", ["institution_admin"], "inst-a", "tok-admin")
        status, pkg = admin.request("POST", "/v1/packages", {"title": "P"})
        self.assertEqual(status, 201)
        status, body = admin.request(
            "GET", f"/v1/packages/{pkg['package_id']}/diff"
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_diff_requires_authentication(self) -> None:
        status, body = ApiClient(self.base).request(
            "GET", "/v1/packages/pkg_1/diff?base_package_id=pkg_2"
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "permission_denied")


if __name__ == "__main__":
    unittest.main()
