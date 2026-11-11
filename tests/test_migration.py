"""Offline tests. No live Backlog account or network is used."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backlog_migrate as b


def cat(destination=False):
    shift = 1000 if destination else 0
    def obj(i, name, **extra):
        return {"id": i + shift, "name": name, **extra}
    return {
        "project": {"id": 2 if destination else 1, "projectKey": "NEW" if destination else "OLD",
                    "textFormattingRule": "markdown", "subtaskingEnabled": True},
        "statuses": [obj(1, "未対応"), obj(2, "処理中"), obj(3, "レビュー"), obj(4, "完了")],
        "issueTypes": [obj(10, "タスク")],
        "categories": [obj(20, "画面")],
        "versions": [obj(30, "v1")],
        "priorities": [obj(3, "中")],
        "resolutions": [obj(0, "対応済み")],
        "users": [obj(7, "伊藤", mailAddress="ito@example.com", nulabAccount={"nulabId": "same-nulab-id"})],
        "customFields": [obj(40, "工数", typeId=3, required=True, applicableIssueTypes=[]),
                         obj(41, "環境", typeId=6, items=[obj(50, "Mac"), obj(51, "Windows")], applicableIssueTypes=[]),
                         obj(42, "日付", typeId=4, applicableIssueTypes=[])],
    }


def issue(iid=11, key=1, parent=None):
    c = cat()
    return {"id": iid, "projectId": 1, "keyId": key, "issueKey": f"OLD-{key}",
            "summary": "テスト課題" + str(key), "description": "本文\n<@U7> メール a@b.example",
            "issueType": c["issueTypes"][0], "status": c["statuses"][2],
            "priority": c["priorities"][0], "resolution": None, "assignee": c["users"][0],
            "category": c["categories"], "versions": c["versions"], "milestone": c["versions"],
            "startDate": "2026-11-01T00:00:00Z", "dueDate": "2026-11-30T00:00:00Z",
            "estimatedHours": 0, "actualHours": 1.5, "parentIssueId": parent,
            "createdUser": c["users"][0], "updatedUser": c["users"][0],
            "created": "2026-10-05T03:00:00Z", "updated": "2026-11-01T03:00:00Z",
            "customFields": [{"id": 40, "value": 0}, {"id": 41, "value": [{"id": 50, "name": "Mac"}]},
                             {"id": 42, "value": "2026-11-07T00:00:00Z"}],
            "attachments": [], "sharedFiles": []}


class FakeAPI:
    def __init__(self, destination=False, rows=None):
        self.destination = destination
        self.catalog = cat(destination)
        self.rows = {x["id"]: copy.deepcopy(x) for x in (rows or [])}
        self.comments = {iid: [] for iid in self.rows}
        self.attachments = {iid: [] for iid in self.rows}
        self.bytes = {}
        self.uploads = {}
        self.calls = []
        self.next_id = 5000
        self.lost_response = None
        self.fail_get_after_attachment = False
        self.fail_attachment_listing = False

    def num(self):
        self.next_id += 1
        return self.next_id

    def get(self, path, params=None):
        self.calls.append(("GET", path, copy.deepcopy(params)))
        params = params or {}
        if re.fullmatch(r"/projects/[^/]+", path):
            return copy.deepcopy(self.catalog["project"])
        m = re.fullmatch(r"/projects/[^/]+/([^/]+)", path)
        if m:
            return copy.deepcopy(self.catalog[m[1]])
        if path in ("/priorities", "/resolutions"):
            return copy.deepcopy(self.catalog[path[1:]])
        if path == "/issues/count":
            return {"count": len(self.rows)}
        if path == "/issues":
            rows = sorted(self.rows.values(), key=lambda x: x["keyId"])
            start = params.get("offset", 0)
            return copy.deepcopy(rows[start:start + params.get("count", 20)])
        m = re.fullmatch(r"/issues/(\d+)(.*)", path)
        if not m:
            raise AssertionError(path)
        iid, tail = int(m[1]), m[2]
        if not tail:
            return copy.deepcopy(self.rows[iid])
        if tail == "/comments/count":
            return {"count": len(self.comments.get(iid, []))}
        if tail == "/comments":
            # Test the inclusive-minId variant; deduplication must also support it.
            cs = [c for c in self.comments.get(iid, []) if c["id"] >= params.get("minId", 0)]
            return copy.deepcopy(cs[:params.get("count", 20)])
        if tail == "/attachments":
            if self.fail_attachment_listing:
                self.fail_attachment_listing = False
                raise b.APIError("temporary GET failure", method="GET", status=503)
            return copy.deepcopy(self.attachments.get(iid, []))
        raise AssertionError(path)

    def lookup(self, kind, num):
        return copy.deepcopy(next(x for x in self.catalog[kind] if x["id"] == num))

    def post(self, path, form):
        assert self.destination, "SOURCE WRITE"
        self.calls.append(("POST", path, copy.deepcopy(form)))
        if path == "/issues":
            assert "statusId" not in form, "Create Issue must not send statusId"
            iid = self.num()
            row = {"id": iid, "projectId": form["projectId"], "keyId": iid,
                   "issueKey": f"NEW-{iid}", "summary": form["summary"], "description": form["description"],
                   "issueType": self.lookup("issueTypes", form["issueTypeId"]),
                   "priority": self.lookup("priorities", form["priorityId"]),
                   "status": self.catalog["statuses"][0], "resolution": None,
                   "assignee": self.lookup("users", form["assigneeId"]) if "assigneeId" in form else None,
                   "category": [self.lookup("categories", n) for n in form.get("categoryId[]", [])],
                   "versions": [self.lookup("versions", n) for n in form.get("versionId[]", [])],
                   "milestone": [self.lookup("versions", n) for n in form.get("milestoneId[]", [])],
                   "parentIssueId": form.get("parentIssueId"), "customFields": [],
                   "created": "2026-11-09T00:00:00Z", "updated": "2026-11-09T00:00:00Z"}
            for k in ("startDate", "dueDate", "estimatedHours", "actualHours"):
                row[k] = form.get(k)
            for k, value in form.items():
                m = re.fullmatch(r"customField_(\d+)(\[\])?", k)
                if m:
                    f = self.lookup("customFields", int(m[1]))
                    if f["typeId"] in (5, 6, 7, 8):
                        vals = value if isinstance(value, list) else [value]
                        value = [next(x for x in f["items"] if x["id"] == v) for v in vals]
                    row["customFields"].append({"id": int(m[1]), "value": value})
            self.rows[iid] = row
            self.comments[iid] = []
            self.attachments[iid] = []
            result = copy.deepcopy(row)
            kind = "issue"
        else:
            m = re.fullmatch(r"/issues/(\d+)/comments", path)
            assert m, path
            iid = int(m[1])
            result = {"id": self.num(), "content": form["content"], "created": "2026-11-09T00:00:00Z"}
            self.comments[iid].append(result)
            kind = "comment"
            for temp in form.get("attachmentId[]", []):
                a = self.uploads.pop(temp)
                aid = self.num()
                self.attachments[iid].append({"id": aid, "name": a["name"], "size": len(a["data"])})
                self.bytes[aid] = a["data"]
                kind = "attachment"
            if kind == "attachment" and self.fail_get_after_attachment:
                self.fail_get_after_attachment = False
                self.fail_attachment_listing = True
        if self.lost_response == kind:
            self.lost_response = None
            raise b.APIError("timeout after server commit", uncertain=True, method="POST")
        return result

    def patch(self, path, form):
        assert self.destination, "SOURCE WRITE"
        self.calls.append(("PATCH", path, copy.deepcopy(form)))
        iid = int(path.rsplit("/", 1)[1])
        assert set(form) <= {"statusId", "resolutionId"}
        if "statusId" in form:
            self.rows[iid]["status"] = self.lookup("statuses", form["statusId"])
        if "resolutionId" in form:
            self.rows[iid]["resolution"] = self.lookup("resolutions", form["resolutionId"])
        return copy.deepcopy(self.rows[iid])

    def upload(self, path, name):
        assert self.destination
        uid = self.num()
        self.uploads[uid] = {"name": name, "data": path.read_bytes()}
        self.calls.append(("POST", "/space/attachment", name))
        return {"id": uid}

    def download(self, path, file):
        self.calls.append(("GET", path, None))
        aid = int(path.rsplit("/", 1)[1])
        data = self.bytes[aid]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.cfg = {"source": {"url": "https://old.backlog.jp", "project_key": "OLD"},
                    "destination": {"url": "https://new.backlog.jp", "project_key": "NEW"},
                    "work_dir": str(self.work), "options": copy.deepcopy(b.DEFAULT_OPTIONS)}
        # The child has the earlier issue number: ancestor inclusion must work with --limit 1.
        self.src = FakeAPI(rows=[issue(11, 1, 12), issue(12, 2), issue(13, 3)])
        self.src.comments[11] = [
            {"id": 101, "content": "確認 <@project>", "created": "2026-11-01T00:00:00Z",
             "updated": "2026-11-02T00:00:00Z", "createdUser": cat()["users"][0], "changeLog": None},
            {"id": 102, "content": None, "created": "2026-11-02T00:00:00Z", "createdUser": None,
             "changeLog": [{"field": "status", "oldValue": "旧", "newValue": "レビュー"}]}]
        self.src.attachments[11] = [{"id": 900, "name": "確認資料.txt", "size": 6},
                                   {"id": 901, "name": "確認資料.txt", "size": 6}]
        self.src.bytes[900] = b"ABC123"
        self.src.bytes[901] = b"XYZ456"
        self.dst = FakeAPI(destination=True)
        # The production error concerned the 12-status cap: test with all 12 slots occupied.
        self.src.catalog["statuses"] += [{"id": x, "name": f"追加状態{x}"} for x in range(5, 13)]
        self.dst.catalog["statuses"] += [{"id": x + 1000, "name": f"追加状態{x}"} for x in range(5, 13)]
        self.m = b.Migrator(self.cfg, self.src, self.dst)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.m.prepare()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_prepare_and_check_are_read_only(self):
        self.m.check()
        self.assertFalse(any(c[0] != "GET" for c in self.src.calls + self.dst.calls))

    def test_complete_migration_and_verify(self):
        self.m.migrate(apply=True)
        self.m.verify(deep=True)
        report = b.read_json(self.work / "verification_report.json")
        self.assertEqual(report["ok"], 3)
        self.assertEqual(len(self.dst.rows), 3)
        self.assertEqual(sum(len(v) for v in self.dst.attachments.values()), 2)
        self.assertTrue((self.work / "issue_mapping.csv").exists())
        self.assertFalse(any(c[0] != "GET" for c in self.src.calls))
        self.assertFalse(any(c[0] != "GET" and "statuses" in c[1] for c in self.dst.calls))

    def test_limit_includes_parent_then_full_resume_no_duplicates(self):
        self.m.migrate(apply=True, limit=1)
        self.assertEqual(len(self.dst.rows), 2)
        self.m.verify(deep=True, limit=1)
        self.m.migrate(apply=True)
        before = [x for x in self.dst.calls if x[0] != "GET"]
        b.Migrator(self.cfg, self.src, self.dst).migrate(apply=True)
        after = [x for x in self.dst.calls if x[0] != "GET"]
        self.assertEqual(before, after)
        self.assertEqual(len(self.dst.rows), 3)

    def test_source_change_blocks(self):
        self.src.rows[11]["updated"] = "2026-11-09T03:00:00Z"
        with self.assertRaisesRegex(b.MigrationError, "事前チェック"):
            self.m.migrate(apply=True)
        self.assertEqual(len(self.dst.rows), 0)

    def test_unknown_destination_issues_block(self):
        self.dst.rows[999] = {**issue(), "id": 999, "projectId": 2}
        with self.assertRaisesRegex(b.MigrationError, "事前チェック"):
            self.m.check()

    def test_unresolved_status_blocks_all_writes(self):
        mapping = b.read_json(self.work / "mapping.json")
        mapping["statuses"]["3"] = None
        b.save_json(self.work / "mapping.json", mapping)
        with self.assertRaisesRegex(b.MigrationError, "事前チェック"):
            self.m.migrate(apply=True)
        self.assertEqual(len(self.dst.rows), 0)

    def test_api_result_lost_issue_recovers(self):
        self.dst.lost_response = "issue"
        with self.assertRaises(b.APIError):
            self.m.migrate(apply=True)
        self.assertEqual(len(self.dst.rows), 1)
        b.Migrator(self.cfg, self.src, self.dst).migrate(apply=True)
        self.assertEqual(len(self.dst.rows), 3)
        self.m.verify(deep=True)

    def test_api_result_lost_comment_recovers(self):
        self.dst.lost_response = "comment"
        with self.assertRaises(b.APIError):
            self.m.migrate(apply=True)
        b.Migrator(self.cfg, self.src, self.dst).migrate(apply=True)
        self.m.verify(deep=True)
        self.assertEqual(sum(len(c) for c in self.dst.comments.values()), 4)

    def test_api_result_lost_attachment_recovers(self):
        self.dst.lost_response = "attachment"
        with self.assertRaises(b.APIError):
            self.m.migrate(apply=True)
        b.Migrator(self.cfg, self.src, self.dst).migrate(apply=True)
        self.m.verify(deep=True)
        self.assertEqual(sum(len(c) for c in self.dst.comments.values()), 4)

    def test_get_failure_after_attachment_commit_retains_journal(self):
        self.dst.fail_get_after_attachment = True
        with self.assertRaises(b.APIError):
            self.m.migrate(apply=True)
        state = b.read_json(self.work / "state.json")
        self.assertEqual(state["ops"]["attachment:11:900"]["status"], "pending")
        b.Migrator(self.cfg, self.src, self.dst).migrate(apply=True)
        self.m.verify(deep=True)
        self.assertEqual(sum(len(c) for c in self.dst.comments.values()), 4)

    def test_same_size_file_corruption_detected_by_hash(self):
        self.m.migrate(apply=True)
        aid = next(iter(self.dst.bytes))
        self.dst.bytes[aid] = b"BAD123"
        with self.assertRaisesRegex(b.MigrationError, "照合"):
            self.m.verify(deep=True)

    def test_local_file_corruption_prevents_write(self):
        (self.work / "files" / "11" / "900").write_bytes(b"BAD123")
        with self.assertRaises(b.MigrationError):
            self.m.check()

    def test_mapping_changes_block_after_migration(self):
        self.m.migrate(apply=True, limit=1)
        mapping = b.read_json(self.work / "mapping.json")
        mapping["statuses"]["3"] = 1002
        b.save_json(self.work / "mapping.json", mapping)
        with self.assertRaisesRegex(b.MigrationError, "途中再開"):
            self.m.migrate(apply=True)

    def test_recreated_target_rejected(self):
        self.dst.catalog["project"]["id"] = 88
        with self.assertRaisesRegex(b.MigrationError, "再作成"):
            self.m.check()

    def test_apply_required(self):
        with self.assertRaisesRegex(b.MigrationError, "--apply"):
            self.m.migrate(apply=False)
        self.assertEqual(len(self.dst.rows), 0)


class UnitTests(unittest.TestCase):
    def test_clients_accept_config_keys_and_prefer_them(self):
        config = b.load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        config["source"]["api_key"] = " source-test-key "
        config["destination"]["api_key"] = "destination-test-key"
        with patch.dict(b.os.environ, {"BACKLOG_SRC_API_KEY": "environment-key"}, clear=True):
            source, destination = b.make_clients(config)
        self.assertEqual(source.key, "source-test-key")
        self.assertEqual(destination.key, "destination-test-key")
        self.assertFalse(source.writable)
        self.assertTrue(destination.writable)

    def test_clients_fall_back_to_environment_for_empty_or_absent_key(self):
        config = b.load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        config["source"].pop("api_key")
        config["source"]["api_key_env"] = "CUSTOM_SRC_KEY"
        config["destination"]["api_key"] = "  "
        with patch.dict(b.os.environ, {"CUSTOM_SRC_KEY": "source-env-key",
                                       "BACKLOG_DST_API_KEY": "destination-env-key"}, clear=True):
            source, destination = b.make_clients(config)
        self.assertEqual(source.key, "source-env-key")
        self.assertEqual(destination.key, "destination-env-key")

    def test_clients_reject_missing_or_non_string_keys(self):
        config = b.load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        with patch.dict(b.os.environ, {}, clear=True):
            with self.assertRaisesRegex(b.MigrationError, "source.api_key"):
                b.make_clients(config)
            config["source"]["api_key"] = 123
            with self.assertRaisesRegex(b.MigrationError, "文字列"):
                b.make_clients(config)

    def test_cli_error_redacts_direct_config_keys(self):
        config = {"source": {"api_key": "source-direct-secret"},
                  "destination": {"api_key": "destination-direct-secret"}}
        output = io.StringIO()
        with patch.object(b, "load_config", return_value=config), \
                patch.object(b, "make_clients", side_effect=b.MigrationError(
                    "source-direct-secret / destination-direct-secret")), \
                contextlib.redirect_stdout(output):
            previous_umask = b.os.umask(0o077)
            try:
                self.assertEqual(b.main(["check"]), 1)
            finally:
                b.os.umask(previous_umask)
        self.assertNotIn("source-direct-secret", output.getvalue())
        self.assertNotIn("destination-direct-secret", output.getvalue())
        self.assertIn("[REDACTED] / [REDACTED]", output.getvalue())

    def test_cli_error_redacts_custom_api_key_environment(self):
        config = {"source": {"api_key_env": "CUSTOM_SRC_KEY"},
                  "destination": {"api_key_env": "CUSTOM_DST_KEY"}}
        output = io.StringIO()
        with patch.dict(b.os.environ, {"CUSTOM_SRC_KEY": "source-test-secret",
                                       "CUSTOM_DST_KEY": "destination-test-secret"}), \
                patch.object(b, "load_config", return_value=config), \
                patch.object(b, "make_clients", side_effect=b.MigrationError(
                    "source-test-secret / destination-test-secret")), \
                contextlib.redirect_stdout(output):
            previous_umask = b.os.umask(0o077)
            try:
                self.assertEqual(b.main(["check"]), 1)
            finally:
                b.os.umask(previous_umask)
        self.assertNotIn("source-test-secret", output.getvalue())
        self.assertNotIn("destination-test-secret", output.getvalue())
        self.assertIn("[REDACTED] / [REDACTED]", output.getvalue())

    def test_users_not_matched_by_display_name(self):
        s, d = cat(), cat(True)
        d["users"][0]["mailAddress"] = "someoneelse@example.com"
        d["users"][0]["nulabAccount"] = {"nulabId": "someoneelse"}
        self.assertIsNone(b.make_mapping(s, d, [])['users']['7'])

    def test_ambiguous_name_not_auto_selected(self):
        s, d = cat(), cat(True)
        d["statuses"].append({"id": 7777, "name": "レビュー"})
        self.assertIsNone(b.make_mapping(s, d, [])['statuses']['3'])

    def test_date_zero_and_list_fields(self):
        s, d = cat(), cat(True)
        mapper = b.Mapper(b.make_mapping(s, d, []), s, d)
        f = mapper.fields(issue())
        self.assertEqual(f["estimatedHours"], 0)
        self.assertEqual(f["customField_1040"], 0)
        self.assertEqual(f["customField_1041[]"], [1050])
        self.assertEqual(f["customField_1042"], "2026-11-07")
        self.assertNotIn("statusId", f)

    def test_unassigned_explicit_mapping(self):
        s, d = cat(), cat(True)
        mapping = b.make_mapping(s, d, [])
        mapping["users"]["7"] = -1
        self.assertNotIn("assigneeId", b.Mapper(mapping, s, d).fields(issue()))

    def test_custom_other_input_stops(self):
        s, d = cat(), cat(True)
        i = issue()
        i["customFields"][1]["otherValue"] = "Linux"
        with self.assertRaises(b.MigrationError):
            b.Mapper(b.make_mapping(s, d, []), s, d).fields(i)

    def test_comments_over_100_inclusive_cursor(self):
        api = FakeAPI(rows=[issue()])
        api.comments[11] = [{"id": n, "content": str(n)} for n in range(1, 252)]
        self.assertEqual(len(b.comment_list(api, 11)), 251)

    def test_issues_over_100(self):
        api = FakeAPI(rows=[issue(n, n) for n in range(1, 402)])
        self.assertEqual(len(b.issue_list(api, 1)), 401)

    def test_cycle_detected(self):
        records = [{"issue": issue(11, 1, 12)}, {"issue": issue(12, 2, 11)}]
        with self.assertRaises(b.MigrationError):
            b.Migrator.selected(records, 0)

    def test_mentions_are_inert(self):
        txt = b.text_copy("<@U1> <@T2> <@project> [~old] @name", True)
        self.assertNotIn("<@", txt)
        self.assertNotIn("[~", txt)
        self.assertIn("@\u200bname", txt)

    def test_url_safety(self):
        self.assertEqual(b.validate_url("https://ABC.backlog.jp/"), "https://abc.backlog.jp")
        for url in ("http://a.backlog.jp", "https://a.backlog.jp.evil.com", "https://evil.com",
                    "https://a.backlog.jp/path", "https://user:secret@a.backlog.jp", "https://a.backlog.jp?apiKey=x"):
            with self.assertRaises(b.MigrationError):
                b.validate_url(url)

    def test_client_source_and_status_write_guard(self):
        api = b.BacklogAPI("https://a.backlog.jp", "secret", writable=False, options=b.DEFAULT_OPTIONS)
        with self.assertRaises(b.MigrationError):
            api.post("/issues", {})
        api.writable = True
        with self.assertRaises(b.MigrationError):
            api.post("/projects/1/statuses", {"name": "oops"})
        with self.assertRaises(b.MigrationError):
            api.request("DELETE", "/issues/1")

    def test_post_timeout_not_retried(self):
        api = b.BacklogAPI("https://a.backlog.jp", "secret", writable=True, options=b.DEFAULT_OPTIONS)
        api.opener = Mock()
        api.opener.open.side_effect = socket.timeout()
        with self.assertRaises(b.APIError) as exc:
            api.post("/issues", {"summary": "hello"})
        self.assertTrue(exc.exception.uncertain)
        self.assertEqual(api.opener.open.call_count, 1)

    def test_api_key_is_header_not_query_and_form_array_repeats(self):
        api = b.BacklogAPI("https://a.backlog.jp", "secret", writable=True, options=b.DEFAULT_OPTIONS)
        response = Mock()
        response.headers = {}
        response.read.return_value = b'{"id":1}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        api.opener = Mock()
        api.opener.open.return_value = response
        api.post("/issues", {"categoryId[]": [1, 2], "summary": "日本語"})
        request = api.opener.open.call_args[0][0]
        self.assertNotIn("secret", request.full_url)
        self.assertIn("secret", dict(request.header_items()).values())
        self.assertIn(b"categoryId%5B%5D=1&categoryId%5B%5D=2", request.data)

    def test_429_is_retried_but_not_status_creation(self):
        api = b.BacklogAPI("https://a.backlog.jp", "secret", writable=True, options=b.DEFAULT_OPTIONS)
        response = Mock()
        response.headers = {}
        response.read.return_value = b'{"id":1}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        fail = urllib.error.HTTPError("https://a.backlog.jp/api/v2/issues", 429, "rate", {}, io.BytesIO(b'{}'))
        api.opener = Mock()
        api.opener.open.side_effect = [fail, response]
        with patch.object(b.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api.post("/issues", {"summary": "hello"})["id"], 1)
        self.assertEqual(api.opener.open.call_count, 2)

    def test_multipart_has_utf8_filename_and_file_bytes(self):
        api = b.BacklogAPI("https://a.backlog.jp", "secret", writable=True, options=b.DEFAULT_OPTIONS)
        response = Mock()
        response.headers = {}
        response.read.return_value = b'{"id":1}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        api.opener = Mock()
        api.opener.open.return_value = response
        with tempfile.TemporaryDirectory() as temp:
            f = Path(temp) / "file"
            f.write_bytes(b"\x00\x01hello\xff")
            api.upload(f, "日本語.txt")
        req = api.opener.open.call_args[0][0]
        self.assertIn('filename="日本語.txt"'.encode("utf-8"), req.data)
        self.assertIn(b"\x00\x01hello\xff", req.data)

    def test_api_error_redacts_api_key(self):
        api = b.BacklogAPI("https://a.backlog.jp", "very-secret-key", writable=True, options=b.DEFAULT_OPTIONS)
        fail = urllib.error.HTTPError("https://a.backlog.jp/api/v2/issues", 400, "bad", {},
                                     io.BytesIO(b'{"error":"very-secret-key"}'))
        api.opener = Mock()
        api.opener.open.side_effect = fail
        with self.assertRaises(b.APIError) as exc:
            api.post("/issues", {})
        self.assertNotIn("very-secret-key", str(exc.exception))
        self.assertEqual(exc.exception.method, "POST")

    def test_uncertain_journal_does_not_retry_without_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = b.Journal(Path(temp) / "state.json", {"test": True})
            journal.pending("issue:1", {})
            action = Mock(return_value={"id": 1})
            with self.assertRaises(b.MigrationError):
                journal.run("issue:1", action, lambda _: None)
            action.assert_not_called()

    def test_definitive_400_releases_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = b.Journal(Path(temp) / "state.json", {"test": True})
            action = Mock(side_effect=b.APIError("validation", status=400, method="POST"))
            with self.assertRaises(b.APIError):
                journal.run("issue:1", action, lambda _: None)
            self.assertIsNone(journal.get("issue:1"))


if __name__ == "__main__":
    unittest.main()
