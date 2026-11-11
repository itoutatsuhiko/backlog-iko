#!/usr/bin/env python3
"""Backlog issue migration, Python 3.10+, standard library only.

No source writes, no status creation, no deletion. See README_JA.md before use.
This is an independently authored utility, not a Nulab product.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

VERSION = "1.0.0"
CATEGORIES = ("statuses", "issueTypes", "categories", "versions", "priorities", "resolutions", "users", "customFields")
DEFAULT_OPTIONS = {
    "request_interval_seconds": 1.05,
    "timeout_seconds": 90,
    "max_file_bytes": 104857600,
    "copy_comments": True,
    "copy_attachments": True,
    "include_change_logs": True,
    "neutralize_mentions": True,
    "allow_nonempty_destination": False,
}


class MigrationError(Exception):
    pass


class APIError(MigrationError):
    def __init__(self, message: str, *, uncertain: bool = False, status: int | None = None, method: str | None = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status
        self.method = method


def say(message: str) -> None:
    print(message, flush=True)


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise MigrationError(f"JSONを読めません: {path} ({type(e).__name__})") from None


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    # Persist the directory entry as well as the file where supported.
    with contextlib.suppress(OSError):
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def work_lock(work: Path):
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(work, 0o700)
    with (work / ".lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError("同じ作業ディレクトリで別のプロセスが動いています。") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def validate_url(value: str) -> str:
    u = urllib.parse.urlsplit(value.strip())
    # Only send secrets to documented Backlog Cloud hosts, over verified HTTPS.
    if (u.scheme != "https" or u.username or u.password or u.port not in (None, 443)
            or u.path not in ("", "/") or u.query or u.fragment
            or not re.fullmatch(r"[a-zA-Z0-9-]+\.(backlog\.jp|backlog\.com|backlogtool\.com)", u.hostname or "")):
        raise MigrationError("スペースURLは https://xxx.backlog.jp などのルートURLを指定してください。")
    return "https://" + u.hostname.lower()


def load_config(path: Path) -> dict:
    c = read_json(path)
    if not isinstance(c, dict):
        raise MigrationError("config.json はJSONオブジェクトにしてください。")
    for side in ("source", "destination"):
        if not isinstance(c.get(side), dict):
            raise MigrationError(f"config.json の {side} がありません。")
        c[side]["url"] = validate_url(str(c[side].get("url", "")))
        key = str(c[side].get("project_key", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", key):
            raise MigrationError(f"{side}.project_key が不正です。")
        c[side]["project_key"] = key
    if c["source"]["url"] == c["destination"]["url"] and c["source"]["project_key"] == c["destination"]["project_key"]:
        raise MigrationError("同じプロジェクトを移行元・先に指定できません。")
    supplied = c.get("options", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_OPTIONS):
        raise MigrationError("options に不明なキーがあります。config.example.jsonを確認してください。")
    c["options"] = {**DEFAULT_OPTIONS, **supplied}
    for k, default in DEFAULT_OPTIONS.items():
        if isinstance(default, bool) and type(c["options"][k]) is not bool:
            raise MigrationError(f"options.{k} は true / false にしてください。")
    if not 1 <= float(c["options"]["request_interval_seconds"]) <= 120:
        raise MigrationError("request_interval_seconds は1〜120にしてください。")
    if not 1 <= int(c["options"]["timeout_seconds"]) <= 600:
        raise MigrationError("timeout_seconds は1〜600にしてください。")
    if not 1 <= int(c["options"]["max_file_bytes"]) <= 1024 * 1024 * 1024:
        raise MigrationError("max_file_bytes は1〜1073741824にしてください。")
    work = Path(c.get("work_dir", "work")).expanduser()
    if not work.is_absolute():
        work = path.resolve().parent / work
    c["work_dir"] = str(work.resolve())
    return c


def identity(config: dict) -> dict:
    return {s: {k: config[s][k] for k in ("url", "project_key")} for s in ("source", "destination")}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward API keys, including to a redirected download URL.
        return None


class BacklogAPI:
    def __init__(self, url: str, key: str, *, writable: bool, options: dict):
        self.url = url
        self.key = key
        self.writable = writable
        self.interval = float(options["request_interval_seconds"])
        self.timeout = float(options["timeout_seconds"])
        self.max_file_bytes = int(options["max_file_bytes"])
        self.opener = urllib.request.build_opener(NoRedirect())
        self.next_request = 0.0
        self.rate_wait_until = 0.0

    def _safe_error(self, text: str) -> str:
        return text.replace(self.key, "[REDACTED]")[:800]

    def _authorize_write(self, method: str, path: str) -> None:
        if method == "GET":
            return
        if not self.writable:
            raise MigrationError("安全装置: 移行元への書き込みを拒否しました。")
        allowed = ((method == "POST" and (path in ("/issues", "/space/attachment")
                                         or re.fullmatch(r"/issues/\d+/comments", path)))
                   or (method == "PATCH" and re.fullmatch(r"/issues/\d+", path)))
        if not allowed:
            raise MigrationError(f"安全装置: 許可されていないAPI操作 {method} {path}")

    def request(self, method: str, path: str, *, params: dict | None = None,
                form: dict | None = None, upload: tuple[Path, str] | None = None,
                download: Path | None = None) -> Any:
        self._authorize_write(method, path)
        if not path.startswith("/") or ".." in path:
            raise MigrationError("APIパスが不正です。")
        target = self.url + "/api/v2" + path
        if params:
            target += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"Backlog-API-Key": self.key, "User-Agent": "backlog-issue-migrator/" + VERSION,
                   "Accept": "*/*" if download else "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if upload:
            file, name = upload
            if file.stat().st_size > self.max_file_bytes:
                raise MigrationError(f"ファイルが max_file_bytes を超過: {name}")
            boundary = "----backlog-migrate-" + uuid.uuid4().hex
            # Prevent multipart header injection; preserve normal Japanese filenames.
            safe_name = name.replace("\r", "").replace("\n", "").replace('"', "%22").replace("\\", "%5C")
            typ = mimetypes.guess_type(name)[0] or "application/octet-stream"
            start = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
                     f"Content-Type: {typ}\r\n\r\n").encode("utf-8")
            data = start + file.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        for attempt in range(6):
            wait = max(self.next_request - time.monotonic(), self.rate_wait_until - time.time(), 0)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(target, data=data, headers=headers, method=method)
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    self._observe_limits(r.headers)
                    if download:
                        download.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        tmp = download.with_name(download.name + ".part")
                        total = 0
                        with tmp.open("wb") as f:
                            for chunk in iter(lambda: r.read(1024 * 1024), b""):
                                total += len(chunk)
                                if total > self.max_file_bytes:
                                    raise MigrationError("ダウンロードが max_file_bytes を超えました。")
                                f.write(chunk)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp, download)
                        return {"size": total, "sha256": file_digest(download)}
                    raw = r.read()
                    try:
                        return json.loads(raw) if raw else None
                    except ValueError:
                        raise APIError(f"JSONでない応答: {method} {path}", uncertain=method != "GET") from None
            except urllib.error.HTTPError as e:
                self._observe_limits(e.headers)
                text = e.read(8192).decode("utf-8", "replace")
                if e.code == 429 and attempt < 5:
                    reset = self._reset_time(e.headers)
                    self.rate_wait_until = max(reset, time.time() + 60)
                    say("APIレート制限: 解除時刻まで待機して再試行します。")
                    continue
                if method == "GET" and e.code in (408, 500, 502, 503, 504) and attempt < 5:
                    self.rate_wait_until = time.time() + min(2 ** (attempt + 1), 30)
                    continue
                raise APIError(self._safe_error(f"{method} {path}: HTTP {e.code} {text}"),
                               uncertain=method != "GET" and (e.code >= 500 or e.code in (408, 409)),
                               status=e.code, method=method) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError,
                    http.client.HTTPException, OSError) as e:
                if method == "GET" and attempt < 5:
                    self.rate_wait_until = time.time() + min(2 ** (attempt + 1), 30)
                    continue
                # Do NOT blindly retry a POST on timeout: the server may have committed it.
                raise APIError(f"通信失敗: {method} {path} ({type(e).__name__})",
                               uncertain=method != "GET") from None
            finally:
                self.next_request = time.monotonic() + self.interval
        raise APIError("API再試行の上限に達しました。", uncertain=method != "GET")

    @staticmethod
    def _reset_time(headers: Any) -> float:
        try:
            return float(headers.get("X-RateLimit-Reset", 0)) + 2
        except (TypeError, ValueError):
            return 0

    def _observe_limits(self, headers: Any) -> None:
        if headers.get("X-RateLimit-Remaining") == "0":
            self.rate_wait_until = max(self.rate_wait_until, self._reset_time(headers), time.time() + 1)

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, form: dict) -> Any:
        return self.request("POST", path, form=form)

    def patch(self, path: str, form: dict) -> Any:
        return self.request("PATCH", path, form=form)

    def upload(self, path: Path, name: str) -> Any:
        return self.request("POST", "/space/attachment", upload=(path, name))

    def download(self, path: str, file: Path) -> dict:
        return self.request("GET", path, download=file)


def issue_list(api: BacklogAPI, project_id: int) -> list[dict]:
    rows: list[dict] = []
    seen: set[int] = set()
    offset = 0
    while True:
        batch = api.get("/issues", {"projectId[]": [project_id], "count": 100, "offset": offset,
                                    "sort": "created", "order": "asc", "parentChild": 0})
        if not isinstance(batch, list):
            raise MigrationError("課題一覧の応答形式が不正です。")
        for r in batch:
            if r["id"] in seen:
                raise MigrationError("課題一覧のページ間で重複を検知。元プロジェクトの更新を止めて再実行してください。")
            seen.add(r["id"])
        rows.extend(batch)
        if len(batch) < 100:
            break
        offset += len(batch)
    count = api.get("/issues/count", {"projectId[]": [project_id]})["count"]
    if len(rows) != count:
        raise MigrationError(f"課題取得件数と件数APIが不一致: {len(rows)} / {count}。更新を止めてください。")
    return sorted(rows, key=lambda x: (x.get("keyId", x["id"]), x["id"]))


def comment_list(api: BacklogAPI, issue_id: int) -> list[dict]:
    rows: dict[int, dict] = {}
    cursor = None
    while True:
        q: dict = {"count": 100, "order": "asc"}
        if cursor is not None:
            q["minId"] = cursor
        batch = api.get(f"/issues/{issue_id}/comments", q)
        if not isinstance(batch, list):
            raise MigrationError("コメント一覧の応答形式が不正です。")
        new = [c for c in batch if c["id"] not in rows]
        if not new:
            if len(batch) >= 100:
                raise MigrationError("コメントのページングが進みません。")
            break
        rows.update({c["id"]: c for c in new})
        high = max(rows)
        if cursor is not None and high <= cursor:
            raise MigrationError("コメントのカーソルが進みません。")
        cursor = high
        if len(batch) < 100:
            break
    count = api.get(f"/issues/{issue_id}/comments/count")["count"]
    if len(rows) != count:
        raise MigrationError(f"課題ID {issue_id}: コメント件数が変動しました ({len(rows)} / {count})。")
    return sorted(rows.values(), key=lambda c: c["id"])


def catalog(api: BacklogAPI, project_key: str) -> dict:
    p = api.get("/projects/" + urllib.parse.quote(project_key, safe=""))
    result = {"project": p}
    for kind in CATEGORIES:
        path = "/" + kind if kind in ("priorities", "resolutions") else f"/projects/{p['id']}/{kind}"
        result[kind] = api.get(path)
        if not isinstance(result[kind], list):
            raise MigrationError(f"{kind} 一覧の応答形式が不正です。")
    return result


def one_match(rows: list[dict], predicate: Callable[[dict], bool]) -> int | None:
    found = [r["id"] for r in rows if predicate(r)]
    return found[0] if len(found) == 1 else None


def make_mapping(src: dict, dst: dict, issues: list[dict]) -> dict:
    # Include assignees that are no longer returned by project-members API.
    users = {u["id"]: u for u in src["users"]}
    for i in issues:
        if i.get("assignee"):
            users[i["assignee"]["id"]] = i["assignee"]
    result: dict = {"_instructions": "null=未解決。数値=移行先ID。usersだけ -1=担当者未設定。詳細はREADME。"}
    for kind in CATEGORIES:
        result[kind] = {}
        for s in (list(users.values()) if kind == "users" else src[kind]):
            target = None
            if kind == "users":
                nid = (s.get("nulabAccount") or {}).get("nulabId")
                email = (s.get("mailAddress") or "").strip().casefold()
                if nid:
                    target = one_match(dst[kind], lambda d: (d.get("nulabAccount") or {}).get("nulabId") == nid)
                if target is None and email:
                    target = one_match(dst[kind], lambda d: (d.get("mailAddress") or "").strip().casefold() == email)
            else:
                target = one_match(dst[kind], lambda d: d["name"] == s["name"] and
                                   (kind != "customFields" or d.get("typeId") == s.get("typeId")))
            result[kind][str(s["id"])] = target
    result["customFieldItems"] = {}
    dest_cf = {f["id"]: f for f in dst["customFields"]}
    for f in src["customFields"]:
        d = dest_cf.get(result["customFields"].get(str(f["id"])), {})
        result["customFieldItems"][str(f["id"])] = {
            str(it["id"]): one_match(d.get("items", []), lambda x: x["name"] == it["name"])
            for it in f.get("items", [])
        }
    return result


def text_copy(text: Any, neutralize: bool) -> str:
    s = str(text or "")
    if neutralize:
        # Backlog mention syntax and common @ syntax are made inert; original text is in the snapshot.
        s = re.sub(r"<@([A-Za-z0-9_-]+)>", r"(旧メンション:\1)", s)
        s = re.sub(r"\[~([^\]\n]+)\]", r"(旧メンション:\1)", s)
        s = s.replace("@", "@\u200b")
    return s


def display_user(u: dict | None) -> str:
    if not u:
        return "不明・削除済みユーザー"
    return f"{u.get('name', '')} (旧ID:{u.get('id', '')})"


def has_value(value: Any) -> bool:
    return value is not None and value != "" and value != []


class Mapper:
    def __init__(self, mapping: dict, source: dict, destination: dict):
        self.mapping = mapping
        self.source = source
        self.destination = destination
        self.dst = {k: {r["id"]: r for r in destination[k]} for k in CATEGORIES}
        self.src_cf = {r["id"]: r for r in source["customFields"]}

    def get(self, kind: str, sid: int, *, allow_unassigned: bool = False) -> int | None:
        v = self.mapping.get(kind, {}).get(str(sid))
        if allow_unassigned and v == -1:
            return None
        if type(v) is not int or v not in self.dst[kind]:
            raise MigrationError(f"mapping.json: {kind} / 移行元ID {sid} の移行先IDが未設定または無効です。")
        return v

    def fields(self, issue: dict) -> dict:
        data: dict = {
            "projectId": self.destination["project"]["id"],
            "summary": issue["summary"],
            "issueTypeId": self.get("issueTypes", issue["issueType"]["id"]),
            "priorityId": self.get("priorities", issue["priority"]["id"]),
        }
        if issue.get("assignee"):
            uid = self.get("users", issue["assignee"]["id"], allow_unassigned=True)
            if uid is not None:
                data["assigneeId"] = uid
        for source_key, param, kind in (("category", "categoryId[]", "categories"),
                                        ("versions", "versionId[]", "versions"),
                                        ("milestone", "milestoneId[]", "versions")):
            if issue.get(source_key):
                values = [self.get(kind, r["id"]) for r in issue[source_key]]
                data[param] = sorted(set(values))
        for date_key in ("startDate", "dueDate"):
            if issue.get(date_key):
                data[date_key] = str(issue[date_key])[:10]
        for k in ("estimatedHours", "actualHours"):
            if issue.get(k) is not None:
                data[k] = issue[k]  # Keep 0; do not treat it as missing.
        for field in issue.get("customFields", []):
            value, other = field.get("value"), field.get("otherValue")
            if not has_value(value) and not has_value(other):
                continue
            sid = field["id"]
            if sid not in self.src_cf:
                raise MigrationError(f"定義が見つからないカスタム属性: {sid}")
            did = self.get("customFields", sid)
            definition = self.dst["customFields"][did]
            typ = self.src_cf[sid]["typeId"]
            if definition["typeId"] != typ:
                raise MigrationError(f"カスタム属性 {sid} の型が一致しません。")
            applicable = definition.get("applicableIssueTypes", [])
            if applicable and data["issueTypeId"] not in applicable:
                raise MigrationError(f"カスタム属性 {did} が移行先の種別に適用されていません。")
            param = f"customField_{did}"
            if typ in (1, 2):
                if not isinstance(value, str):
                    raise MigrationError(f"文字列カスタム属性 {sid} の形式が不正です。")
                data[param] = value
            elif typ == 3:
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    raise MigrationError(f"数値カスタム属性 {sid} の形式が不正です。")
                data[param] = value
            elif typ == 4:
                data[param] = str(value)[:10]
            elif typ in (5, 6, 7, 8):
                items = value if isinstance(value, list) else ([] if value is None else [value])
                mapped = []
                choices = {x["id"] for x in definition.get("items", [])}
                for item in items:
                    old_id = item.get("id") if isinstance(item, dict) else item
                    v = self.mapping.get("customFieldItems", {}).get(str(sid), {}).get(str(old_id))
                    if type(v) is not int or v not in choices:
                        # Unknown "other" representations are not guessed or silently dropped.
                        raise MigrationError(f"カスタム属性 {sid} の選択肢 {old_id} が未対応です。")
                    mapped.append(v)
                if typ in (5, 8) and len(mapped) > 1:
                    raise MigrationError(f"単一選択属性 {sid} に複数の値があります。")
                if mapped:
                    data[param + ("[]" if typ in (6, 7) else "")] = mapped if typ in (6, 7) else mapped[0]
                if has_value(other):
                    # The API's unspecified sentinel for an "other" list item is deliberately not invented.
                    raise MigrationError(f"カスタム属性 {sid} は「その他入力」を使用。安全のため停止します。READMEを確認してください。")
            else:
                raise MigrationError(f"未対応のカスタム属性 typeId={typ}")
        for f in self.destination["customFields"]:
            if not f.get("required"):
                continue
            applicable = f.get("applicableIssueTypes", [])
            if applicable and data["issueTypeId"] not in applicable:
                continue
            p = f"customField_{f['id']}"
            if not has_value(data.get(p)) and not has_value(data.get(p + "[]")):
                raise MigrationError(f"移行先の必須カスタム属性 {f['name']} (ID {f['id']}) に値がありません。")
        return data

    def state_fields(self, issue: dict) -> dict:
        data = {"statusId": self.get("statuses", issue["status"]["id"])}
        if issue.get("resolution"):
            data["resolutionId"] = self.get("resolutions", issue["resolution"]["id"])
        return data


class Journal:
    def __init__(self, path: Path, binding: dict):
        self.path = path
        if path.exists():
            self.data = read_json(path)
            if self.data["binding"] != binding:
                raise MigrationError("途中再開時に設定・マッピング・スナップショットが変わっています。state.jsonを削除しないでください。")
        else:
            self.data = {"binding": binding, "created": stamp(), "ops": {}}

    def save(self):
        save_json(self.path, self.data)

    def get(self, key: str) -> dict | None:
        return self.data["ops"].get(key)

    def pending(self, key: str, meta: dict):
        self.data["ops"][key] = {"status": "pending", "meta": meta, "started": stamp()}
        self.save()

    def done(self, key: str, result: dict):
        op = self.data["ops"].setdefault(key, {"meta": {}})
        op.update({"status": "done", "result": result, "finished": stamp()})
        self.save()
        return result

    def reject(self, key: str):
        self.data["ops"].pop(key, None)
        self.save()

    def run(self, key: str, action: Callable[[], dict], recover: Callable[[dict], dict | None],
            *, meta: dict | None = None, retry_pending: bool = False) -> dict:
        old = self.get(key)
        if old and old["status"] == "done":
            return old["result"]
        if old:
            recovered = recover(old["meta"])
            if recovered is not None:
                say(f"再開: {key} の登録済みデータを確認しました。")
                return self.done(key, recovered)
            if not retry_pending:
                raise MigrationError(f"結果未確定の操作: {key}\n"
                                     "移行先で重複・登録結果を確認してください。未登録を確認できた場合だけ、同じコマンドに --retry-pending を付けます。")
        self.pending(key, meta or (old or {}).get("meta", {}))
        try:
            result = action()
        except APIError as e:
            if not e.uncertain and e.method in ("POST", "PATCH"):
                self.reject(key)
            raise
        # Other failures/crashes intentionally leave the operation pending.
        return self.done(key, result)


class Migrator:
    def __init__(self, config: dict, source: BacklogAPI, destination: BacklogAPI):
        self.config = config
        self.options = config["options"]
        self.src = source
        self.dst = destination
        self.work = Path(config["work_dir"])
        self.snapshot: dict = {}
        self.mapping: dict = {}
        self.mapper: Mapper | None = None
        self.journal: Journal | None = None
        self.dst_catalog: dict = {}
        self.retry_pending = False

    def prepare(self):
        if (self.work / "snapshot.json").exists() or (self.work / "state.json").exists():
            raise MigrationError("準備済みです。check に進んでください。別の移行には別のwork_dirを指定します。")
        say("プロジェクト設定と課題を取得します（移行先への書き込みなし）。")
        sc = catalog(self.src, self.config["source"]["project_key"])
        dc = catalog(self.dst, self.config["destination"]["project_key"])
        if self.config["source"]["url"] == self.config["destination"]["url"] and sc["project"]["id"] == dc["project"]["id"]:
            raise MigrationError("移行元と移行先が同じプロジェクトです。")
        source_rows = issue_list(self.src, sc["project"]["id"])
        records = []
        for pos, brief in enumerate(source_rows, 1):
            iid = brief["id"]
            say(f"取得 {pos}/{len(source_rows)}: {brief['issueKey']}")
            issue = self.src.get(f"/issues/{iid}")
            comments = comment_list(self.src, iid) if self.options["copy_comments"] else []
            attachments = self.src.get(f"/issues/{iid}/attachments")
            files = []
            if self.options["copy_attachments"]:
                for a in attachments:
                    if any(ch in a["name"] for ch in ("\r", "\n", '"', "\\")):
                        raise MigrationError(f"添付名に安全に送信できない文字があります: ID {a['id']}。元のファイルは変更しません。")
                    if a["size"] > self.options["max_file_bytes"]:
                        raise MigrationError(f"{issue['issueKey']}: 添付 {a['name']} がmax_file_bytesを超えます。")
                    relative = Path("files") / str(iid) / str(a["id"])
                    target = self.work / relative
                    # Re-download on an interrupted prepare; never trust an incomplete file.
                    got = self.src.download(f"/issues/{iid}/attachments/{a['id']}", target)
                    if got["size"] != a["size"]:
                        raise MigrationError(f"添付サイズ不一致: {issue['issueKey']} / {a['name']}")
                    files.append({**a, **got, "path": str(relative)})
            after = self.src.get(f"/issues/{iid}")
            if after.get("updated") != issue.get("updated"):
                raise MigrationError(f"取得中に {issue['issueKey']} が更新されました。更新を止めて再実行してください。")
            record = {"issue": issue, "comments": comments, "attachments": files,
                      "source_attachment_count": len(attachments)}
            save_json(self.work / "source" / f"{iid}.json", record)
            records.append(record)
        final_rows = issue_list(self.src, sc["project"]["id"])
        before = {r["issue"]["id"]: r["issue"].get("updated") for r in records}
        after = {r["id"]: r.get("updated") for r in final_rows}
        if before != after:
            raise MigrationError("準備中に元プロジェクトが変更されました。更新を停止してprepareを再実行してください。")
        mapping = make_mapping(sc, dc, [r["issue"] for r in records])
        snapshot = {"version": VERSION, "created": stamp(), "identity": identity(self.config),
                    "options": self.options, "source_catalog": sc, "destination_project_id": dc["project"]["id"], "records": records}
        save_json(self.work / "source_catalog.json", sc)
        save_json(self.work / "destination_catalog.json", dc)
        save_json(self.work / "mapping.json", mapping)
        save_json(self.work / "snapshot.json", snapshot)
        self._mapping_report(sc, dc, mapping)
        say(f"準備完了: {len(records)}件。{self.work / 'mapping.json'} を確認して check を実行してください。")

    def _mapping_report(self, sc: dict, dc: dict, mapping: dict):
        lines = ["# 対応表（参照用）", "", "修正するファイルは mapping.json です。IDは数値で指定してください。", ""]
        for k in CATEGORIES:
            dest = {r["id"]: r for r in dc[k]}
            lines += [f"## {k}", ""]
            for r in sc[k]:
                v = mapping[k].get(str(r["id"]))
                name = dest.get(v, {}).get("name", "未解決")
                lines.append(f"- 元 {r['id']} : {r.get('name', '')} → 先 {v} : {name}")
            lines.append("")
        (self.work / "mapping_report.md").write_text("\n".join(lines), encoding="utf-8")

    def load(self):
        self.snapshot = read_json(self.work / "snapshot.json")
        if self.snapshot.get("identity") != identity(self.config):
            raise MigrationError("prepare時と移行元・移行先が異なります。別のwork_dirを使ってください。")
        if self.snapshot.get("options") != self.options:
            raise MigrationError("prepare後にoptionsが変更されています。既存workを保持し、別のwork_dirで準備してください。")
        self.mapping = read_json(self.work / "mapping.json")
        binding = {"identity": identity(self.config), "snapshot_sha256": digest(self.snapshot),
                   "mapping_sha256": digest(self.mapping)}
        self.journal = Journal(self.work / "state.json", binding)

    def refresh_destination(self):
        self.dst_catalog = catalog(self.dst, self.config["destination"]["project_key"])
        if self.dst_catalog["project"]["id"] != self.snapshot["destination_project_id"]:
            raise MigrationError("移行先プロジェクトがprepare後に再作成されています。新しいwork_dirを使ってください。")
        save_json(self.work / "destination_catalog.json", self.dst_catalog)
        self.mapper = Mapper(self.mapping, self.snapshot["source_catalog"], self.dst_catalog)

    def check(self) -> list[dict]:
        self.load()
        self.refresh_destination()
        errors: list[str] = []
        warnings: list[str] = []
        records = self.snapshot["records"]
        source_project = self.snapshot["source_catalog"]["project"]
        current = issue_list(self.src, source_project["id"])
        saved = {r["issue"]["id"]: r["issue"].get("updated") for r in records}
        live = {r["id"]: r.get("updated") for r in current}
        if saved != live:
            errors.append("移行元の課題件数・ID・更新日時がprepare後に変化しました。移行中は更新を停止してください。")
        if source_project.get("textFormattingRule") != self.dst_catalog["project"].get("textFormattingRule"):
            errors.append("テキスト整形ルールが一致しません。移行先を同じルールに合わせてください。")
        ids = {r["issue"]["id"] for r in records}
        if len(ids) != len(records):
            errors.append("snapshotに重複する課題IDがあります。")
        for r in records:
            i = r["issue"]
            try:
                self.mapper.fields(i)
                self.mapper.state_fields(i)
                parent = i.get("parentIssueId")
                if parent and parent not in ids:
                    raise MigrationError(f"親課題 {parent} が移行対象にありません。")
                if parent and not self.dst_catalog["project"].get("subtaskingEnabled", False):
                    raise MigrationError("移行先で親子課題が有効になっていません。")
                for a in r["attachments"]:
                    path = self.work / a["path"]
                    if not path.is_file() or path.stat().st_size != a["size"] or file_digest(path) != a["sha256"]:
                        raise MigrationError(f"保存した添付が欠落・変化: {a['name']}")
                if i.get("sharedFiles"):
                    warnings.append(f"{i['issueKey']}: 共有ファイルリンクは対象外です。")
                if i.get("assignee") and self.mapping.get("users", {}).get(str(i["assignee"]["id"])) == -1:
                    warnings.append(f"{i['issueKey']}: 担当者を未設定にします。")
            except MigrationError as e:
                errors.append(f"{i['issueKey']}: {e}")
        try:
            self.selected(records, 0)
        except MigrationError as e:
            errors.append(str(e))
        # Reject untracked existing issues by default, including leftovers of the official tool.
        existing = issue_list(self.dst, self.dst_catalog["project"]["id"])
        known = {op["result"]["id"] for key, op in self.journal.data["ops"].items()
                 if key.startswith("issue:") and op["status"] == "done"}
        pending_markers = {self.marker("issue", key.split(":")[1])
                           for key, op in self.journal.data["ops"].items()
                           if key.startswith("issue:") and op["status"] == "pending"}
        unknown = [i for i in existing if i["id"] not in known and
                   not any(self.ends_marker(i.get("description", ""), m) for m in pending_markers)]
        if unknown and not self.options["allow_nonempty_destination"]:
            errors.append(f"移行先に、このツールの管理外の課題が{len(unknown)}件あります。空の移行先を使ってください。既存課題への追記・統合はしません。")
        if len(self.dst_catalog["statuses"]) > 12:
            warnings.append("移行先に12個を超える状態があります。管理画面を確認してください。")
        for k in ("copy_comments", "copy_attachments", "include_change_logs", "neutralize_mentions"):
            if not self.options[k]:
                warnings.append(f"設定 {k}=false です。READMEの制限を確認してください。")
        report = {"checked": stamp(), "issues": len(records), "comments": sum(len(r["comments"]) for r in records),
                  "attachments": sum(len(r["attachments"]) for r in records),
                  "errors": sorted(set(errors)), "warnings": sorted(set(warnings))}
        save_json(self.work / "check_report.json", report)
        for w in report["warnings"]:
            say("注意: " + w)
        if errors:
            for e in report["errors"][:30]:
                say("ERROR: " + e)
            raise MigrationError(f"事前チェックで{len(report['errors'])}件の問題。check_report.jsonを確認してください。移行は開始していません。")
        say(f"CHECK OK: 課題 {report['issues']} / コメント {report['comments']} / 添付 {report['attachments']}。状態追加は行いません。")
        return records

    @staticmethod
    def selected(records: list[dict], limit: int) -> list[dict]:
        by_id = {r["issue"]["id"]: r for r in records}
        out: list[dict] = []
        done: set[int] = set()
        visiting: set[int] = set()
        def add(iid: int):
            if iid in done:
                return
            if iid in visiting:
                raise MigrationError("親子課題が循環しています。")
            if iid not in by_id:
                raise MigrationError(f"親課題 {iid} が対象外です。")
            visiting.add(iid)
            r = by_id[iid]
            if r["issue"].get("parentIssueId"):
                add(r["issue"]["parentIssueId"])
            visiting.remove(iid)
            done.add(iid)
            out.append(r)
        # limit controls seed issues; any needed ancestors are included automatically.
        for r in records[:limit] if limit else records:
            add(r["issue"]["id"])
        return out

    def marker(self, kind: str, *ids: Any) -> str:
        scope = digest(identity(self.config))[:20]
        return "[BLMIG:" + scope + ":" + kind + ":" + ":".join(str(i) for i in ids) + "]"

    @staticmethod
    def ends_marker(text: str | None, marker: str) -> bool:
        return bool(text) and text.rstrip().endswith("\n" + marker)

    def description(self, issue: dict) -> str:
        original = text_copy(issue.get("description"), self.options["neutralize_mentions"])
        user = text_copy(display_user(issue.get("createdUser")), True)
        updated_user = text_copy(display_user(issue.get("updatedUser")), True)
        return (original + "\n\n---\n[移行元情報]\n"
                f"旧課題: {issue['issueKey']}\n"
                f"旧URL: {self.config['source']['url']}/view/{issue['issueKey']}\n"
                f"元の作成者: {user}\n元の作成日時: {issue.get('created', '')}\n"
                f"元の更新者: {updated_user}\n元の更新日時: {issue.get('updated', '')}\n"
                f"元の担当者: {text_copy(display_user(issue.get('assignee')), True)}\n"
                "実際の作成者・作成日時はAPI実行者・移行時点です。\n"
                + self.marker("issue", issue["id"]))

    def comment_content(self, issue: dict, comment: dict) -> str:
        body = text_copy(comment.get("content"), self.options["neutralize_mentions"])
        who = text_copy(display_user(comment.get("createdUser")), True)
        content = (f"[移行コメント]\n元の投稿者: {who}\n元の投稿日時: {comment.get('created', '')}\n"
                   f"元の更新日時: {comment.get('updated', '')}\n\n{body}")
        if self.options["include_change_logs"] and comment.get("changeLog"):
            history = json.dumps(comment["changeLog"], ensure_ascii=False, indent=2)
            content += "\n\n[旧変更履歴・参照用（状態操作としては再実行しません）]\n" + text_copy(history, self.options["neutralize_mentions"])
        return content + "\n\n" + self.marker("comment", issue["id"], comment["id"])

    def wanted_comments(self, record: dict) -> list[dict]:
        return [c for c in record["comments"] if has_value(c.get("content")) or
                (self.options["include_change_logs"] and c.get("changeLog"))]

    def find_issue_marker(self, marker: str) -> dict | None:
        rows = issue_list(self.dst, self.dst_catalog["project"]["id"])
        hits = [i for i in rows if self.ends_marker(i.get("description"), marker)]
        if len(hits) > 1:
            raise MigrationError("同じ移行マーカーの課題が複数存在します。重複を確認してください。")
        return {"id": hits[0]["id"], "issueKey": hits[0]["issueKey"]} if hits else None

    def find_comment_marker(self, issue_id: int, marker: str) -> dict | None:
        rows = comment_list(self.dst, issue_id)
        hits = [c for c in rows if self.ends_marker(c.get("content"), marker)]
        if len(hits) > 1:
            raise MigrationError("同じ移行マーカーのコメントが複数存在します。")
        return {"id": hits[0]["id"]} if hits else None

    def create_issue(self, record: dict) -> dict:
        i = record["issue"]
        form = self.mapper.fields(i)
        form["description"] = self.description(i)
        parent = i.get("parentIssueId")
        if parent:
            op = self.journal.get(f"issue:{parent}")
            if not op or op["status"] != "done":
                raise MigrationError("親課題が未作成です。")
            form["parentIssueId"] = op["result"]["id"]
        def action():
            r = self.dst.post("/issues", form)
            return {"id": r["id"], "issueKey": r["issueKey"]}
        return self.journal.run(f"issue:{i['id']}", action,
                                lambda meta: self.find_issue_marker(self.marker("issue", i["id"])),
                                retry_pending=self.retry_pending)

    def copy_comment(self, issue: dict, comment: dict, dest_id: int):
        marker = self.marker("comment", issue["id"], comment["id"])
        def action():
            r = self.dst.post(f"/issues/{dest_id}/comments", {"content": self.comment_content(issue, comment)})
            return {"id": r["id"]}
        return self.journal.run(f"comment:{issue['id']}:{comment['id']}", action,
                                lambda meta: self.find_comment_marker(dest_id, marker),
                                retry_pending=self.retry_pending)

    def copy_attachment(self, issue: dict, attachment: dict, dest_id: int):
        key = f"attachment:{issue['id']}:{attachment['id']}"
        old = self.journal.get(key)
        if old and old["status"] == "done":
            return old["result"]
        marker = self.marker("attachment", issue["id"], attachment["id"])
        baseline = ((old or {}).get("meta") or {}).get("baseline")
        if baseline is None:
            baseline = [a["id"] for a in self.dst.get(f"/issues/{dest_id}/attachments")]
        def identify(comment_id: int) -> dict:
            attachments = self.dst.get(f"/issues/{dest_id}/attachments")
            # Each attachment is put into its own migration comment. Its original
            # comment association / embedded URL is NOT guessed.
            hits = [a for a in attachments if a["id"] not in baseline and
                    a["name"] == attachment["name"] and a["size"] == attachment["size"]]
            if len(hits) != 1:
                raise MigrationError(f"添付の登録結果を一意に特定できません: {attachment['name']}。stateを保持して確認してください。")
            return {"id": hits[0]["id"], "comment_id": comment_id, "name": hits[0]["name"], "sha256": attachment["sha256"]}
        def recover(meta):
            c = self.find_comment_marker(dest_id, marker)
            return identify(c["id"]) if c else None
        def action():
            temp = self.dst.upload(self.work / attachment["path"], attachment["name"])
            content = ("[移行添付ファイル]\n"
                       f"元ファイル: {text_copy(attachment['name'], True)}\n"
                       f"元の添付ID: {attachment['id']}\nSHA-256: {attachment['sha256']}\n"
                       "元コメントへの関連付けではなく、移行用コメントへの添付です。\n\n" + marker)
            r = self.dst.post(f"/issues/{dest_id}/comments", {"content": content, "attachmentId[]": [temp["id"]]})
            return identify(r["id"])
        return self.journal.run(key, action, recover, meta={"baseline": baseline}, retry_pending=self.retry_pending)

    def set_current_status(self, issue: dict, dest_id: int):
        form = self.mapper.state_fields(issue)
        def matches(r):
            return (r["status"]["id"] == form["statusId"] and
                    ("resolutionId" not in form or (r.get("resolution") or {}).get("id") == form["resolutionId"]))
        def recover(meta):
            return {"id": dest_id} if matches(self.dst.get(f"/issues/{dest_id}")) else None
        def action():
            current = self.dst.get(f"/issues/{dest_id}")
            if not matches(current):
                self.dst.patch(f"/issues/{dest_id}", form)
            return {"id": dest_id}
        return self.journal.run(f"status:{issue['id']}", action, recover, retry_pending=self.retry_pending)

    def migrate(self, *, apply: bool, limit: int = 0, retry_pending: bool = False):
        if not apply:
            raise MigrationError("書き込みには --apply が必要です。確認だけの場合は check を使ってください。")
        records = self.check()
        records = self.selected(records, limit)
        self.retry_pending = retry_pending
        say(f"移行先: {self.config['destination']['url']} / {self.config['destination']['project_key']}")
        say(f"対象 {len(records)}件（必要な親課題を含む）。旧データの削除・状態追加・既存課題への統合はしません。")
        for n, record in enumerate(records, 1):
            issue = record["issue"]
            say(f"移行 {n}/{len(records)}: {issue['issueKey']}")
            target = self.create_issue(record)
            for c in self.wanted_comments(record):
                self.copy_comment(issue, c, target["id"])
            for a in record["attachments"]:
                self.copy_attachment(issue, a, target["id"])
            self.set_current_status(issue, target["id"])
            self.journal.done(f"complete:{issue['id']}", {"id": target["id"], "issueKey": target["issueKey"]})
            self.export_correspondence()
        say("選択した課題のコピー処理が終了しました。verify --deep を実行し、画面でも確認してください。")

    def export_correspondence(self):
        path = self.work / "issue_mapping.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["source_key", "source_id", "destination_key", "destination_id", "source_url", "destination_url", "complete"])
            for r in self.snapshot["records"]:
                i = r["issue"]
                op = self.journal.get(f"issue:{i['id']}")
                if op and op["status"] == "done":
                    t = op["result"]
                    w.writerow([i["issueKey"], i["id"], t["issueKey"], t["id"],
                                self.config["source"]["url"] + "/view/" + i["issueKey"],
                                self.config["destination"]["url"] + "/view/" + t["issueKey"],
                                bool(self.journal.get(f"complete:{i['id']}"))])

    def verify(self, *, deep: bool, limit: int = 0):
        self.load()
        self.refresh_destination()
        results = []
        for record in self.selected(self.snapshot["records"], limit):
            i = record["issue"]
            problems = []
            op = self.journal.get(f"issue:{i['id']}")
            if not op or op["status"] != "done":
                results.append({"source": i["issueKey"], "ok": False, "problems": ["未移行"]})
                continue
            target = op["result"]
            t = self.dst.get(f"/issues/{target['id']}")
            expected = self.mapper.fields(i)
            if t["projectId"] != self.dst_catalog["project"]["id"]:
                problems.append("移行先プロジェクトID")
            for name, a, b in (("件名", i["summary"], t["summary"]),
                                ("詳細", self.description(i), t.get("description", "")),
                                ("種別", expected["issueTypeId"], t["issueType"]["id"]),
                                ("優先度", expected["priorityId"], t["priority"]["id"]),
                                ("担当者", expected.get("assigneeId"), (t.get("assignee") or {}).get("id")),
                                ("状態", self.mapper.state_fields(i)["statusId"], t["status"]["id"]),
                                ("完了理由", self.mapper.state_fields(i).get("resolutionId"), (t.get("resolution") or {}).get("id"))):
                if a != b:
                    problems.append(name)
            for k in ("startDate", "dueDate"):
                if (i.get(k) or "")[:10] != (t.get(k) or "")[:10]:
                    problems.append(k)
            for k in ("estimatedHours", "actualHours"):
                if i.get(k) != t.get(k):
                    problems.append(k)
            for name, param in (("category", "categoryId[]"), ("versions", "versionId[]"), ("milestone", "milestoneId[]")):
                if sorted(expected.get(param, [])) != sorted(x["id"] for x in t.get(name, [])):
                    problems.append(name)
            parent = i.get("parentIssueId")
            parent_target = None
            if parent:
                po = self.journal.get(f"issue:{parent}")
                parent_target = po["result"]["id"] if po and po["status"] == "done" else -1
            if t.get("parentIssueId") != parent_target:
                problems.append("親子関係")
            actual_cf = {f["id"]: f for f in t.get("customFields", [])}
            for key, value in expected.items():
                m = re.fullmatch(r"customField_(\d+)(\[\])?", key)
                if not m:
                    continue
                fid = int(m[1])
                actual = actual_cf.get(fid, {}).get("value")
                typ = self.mapper.dst["customFields"][fid]["typeId"]
                if typ in (5, 6, 7, 8):
                    vals = actual if isinstance(actual, list) else ([] if actual is None else [actual])
                    got = sorted(x["id"] if isinstance(x, dict) else x for x in vals)
                    want = sorted(value if isinstance(value, list) else [value])
                    same = got == want
                elif typ == 4:
                    same = str(actual or "")[:10] == str(value)[:10]
                elif typ == 3:
                    try:
                        same = float(actual) == float(value)
                    except (TypeError, ValueError):
                        same = False
                else:
                    same = actual == value
                if not same:
                    problems.append(f"customField_{fid}")
            comments = comment_list(self.dst, target["id"])
            for c in self.wanted_comments(record):
                content = self.comment_content(i, c)
                hits = [x for x in comments if self.ends_marker(x.get("content"), self.marker("comment", i["id"], c["id"]))]
                if len(hits) != 1 or hits[0].get("content") != content:
                    problems.append(f"コメント:{c['id']}")
            atts = {a["id"]: a for a in self.dst.get(f"/issues/{target['id']}/attachments")}
            for a in record["attachments"]:
                ao = self.journal.get(f"attachment:{i['id']}:{a['id']}")
                if not ao or ao["status"] != "done":
                    problems.append(f"添付未登録:{a['id']}")
                    continue
                da = atts.get(ao["result"]["id"])
                if not da or da["name"] != a["name"] or da["size"] != a["size"]:
                    problems.append(f"添付情報:{a['id']}")
                elif deep:
                    temp = self.work / "verification_files" / str(i["id"]) / str(a["id"])
                    got = self.dst.download(f"/issues/{target['id']}/attachments/{da['id']}", temp)
                    if got["sha256"] != a["sha256"]:
                        problems.append(f"添付SHA-256:{a['id']}")
                    temp.unlink(missing_ok=True)
            if not self.journal.get(f"complete:{i['id']}"):
                problems.append("処理未完了")
            say(f"照合 {i['issueKey']} → {target['issueKey']}: " + ("OK" if not problems else ", ".join(problems)))
            results.append({"source": i["issueKey"], "destination": target["issueKey"], "ok": not problems, "problems": problems})
        live = issue_list(self.src, self.snapshot["source_catalog"]["project"]["id"])
        source_unchanged = ({r["id"]: r.get("updated") for r in live} ==
                            {r["issue"]["id"]: r["issue"].get("updated") for r in self.snapshot["records"]})
        report = {"verified": stamp(), "deep": deep, "source_unchanged_by_id_and_updated": source_unchanged,
                  "checked": len(results), "ok": sum(r["ok"] for r in results), "results": results}
        save_json(self.work / "verification_report.json", report)
        self.export_correspondence()
        if any(not r["ok"] for r in results) or not source_unchanged:
            raise MigrationError("照合に不一致・未移行、または移行元の更新があります。verification_report.jsonを確認してください。")
        say(f"VERIFY OK: {len(results)}件。リンク・画像表示・通知・対象外データは画面で別途確認してください。")


def make_clients(config: dict) -> tuple[BacklogAPI, BacklogAPI]:
    clients = []
    for side, default_env in (("source", "BACKLOG_SRC_API_KEY"), ("destination", "BACKLOG_DST_API_KEY")):
        name = config[side].get("api_key_env", default_env)
        direct = config[side].get("api_key", "")
        if not isinstance(direct, str):
            raise MigrationError(f"{side}.api_key は文字列にしてください。")
        key = direct.strip() or os.environ.get(name, "").strip()
        if not key:
            raise MigrationError(f"{side}.api_key にAPIキーを設定してください（環境変数 {name} でも指定できます）。")
        clients.append(BacklogAPI(config[side]["url"], key, writable=side == "destination", options=config["options"]))
    return clients[0], clients[1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backlog課題の別スペース移行（状態追加なし・Git対象外）")
    p.add_argument("command", choices=("prepare", "check", "migrate", "verify"))
    p.add_argument("--config", default="config.json")
    p.add_argument("--apply", action="store_true", help="migrateの書き込みを明示的に許可")
    p.add_argument("--limit", type=int, default=0, help="先頭N課題＋必要な親課題。0=全件")
    p.add_argument("--deep", action="store_true", help="verifyで移行先添付をダウンロードしSHA-256照合")
    p.add_argument("--retry-pending", action="store_true", help="未登録を人手確認した結果不明の操作を再試行（要注意）")
    p.add_argument("--version", action="version", version=VERSION)
    args = p.parse_args(argv)
    if args.limit < 0:
        p.error("--limit は0以上です。")
    if args.command != "migrate" and (args.apply or args.retry_pending):
        p.error("--apply / --retry-pending は migrate 専用です。")
    if args.command != "verify" and args.deep:
        p.error("--deep は verify 専用です。")
    if args.command in ("prepare", "check") and args.limit:
        p.error("prepare / check は全件を検査します。--limit は migrate / verify 専用です。")
    secret_names = {"BACKLOG_SRC_API_KEY", "BACKLOG_DST_API_KEY"}
    direct_secrets = set()
    try:
        os.umask(0o077)
        c = load_config(Path(args.config))
        for side, default in (("source", "BACKLOG_SRC_API_KEY"), ("destination", "BACKLOG_DST_API_KEY")):
            direct = c[side].get("api_key", "")
            if isinstance(direct, str) and direct.strip():
                direct_secrets.add(direct.strip())
            name = c[side].get("api_key_env", default)
            if isinstance(name, str):
                secret_names.add(name)
        src, dst = make_clients(c)
        with work_lock(Path(c["work_dir"])):
            m = Migrator(c, src, dst)
            if args.command == "prepare":
                m.prepare()
            elif args.command == "check":
                m.check()
            elif args.command == "migrate":
                m.migrate(apply=args.apply, limit=args.limit, retry_pending=args.retry_pending)
            else:
                m.verify(deep=args.deep, limit=args.limit)
        return 0
    except KeyboardInterrupt:
        say("\n中断しました。work/を保持してください。migrateは同じ設定・同じworkで再開できます。")
        return 130
    except (MigrationError, OSError, ValueError, KeyError, TypeError) as e:
        # Never dump a traceback with an API key / request object in local variables.
        message = str(e)
        for key in sorted(direct_secrets | {os.environ.get(name, "").strip() for name in secret_names}, key=len, reverse=True):
            if key:
                message = message.replace(key, "[REDACTED]")
        say("ERROR: " + message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
