#!/usr/bin/env python3
"""Independent document migration CLI. Public API only; no issue writes."""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse

import backlog_migrate as common
from document_markdown import Converter, normalize

MigrationError = common.MigrationError
SCHEMA = 1


def doc_id(value):
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise MigrationError('ドキュメントIDの形式が不正です')
    return value


class DocumentAPI(common.BacklogAPI):
    """Separate allowlist: create documents, tag only this run's owned IDs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.owned_ids = set()

    def _authorize_write(self, method, path):
        if method == 'GET':
            return
        tag = re.fullmatch(r'/documents/([A-Za-z0-9_-]+)/tags', path)
        if self.writable and method == 'POST' and (path == '/documents' or
                (tag and tag[1] in self.owned_ids)):
            return
        raise MigrationError(f'安全装置: ドキュメント用クライアントでは禁止された操作 {method} {path}')


def document_list(api, pid):
    rows, seen, offset = [], set(), 0
    while True:
        batch = api.get('/documents', {'projectId[]': [pid], 'offset': offset,
                                      'count': 100, 'sort': 'created', 'order': 'asc'})
        if not isinstance(batch, list):
            raise MigrationError('ドキュメント一覧の形式が不正')
        for row in batch:
            did = doc_id(row['id'])
            if did in seen or row['projectId'] != pid:
                raise MigrationError('ドキュメント一覧に重複または別プロジェクトを検出')
            seen.add(did)
        rows.extend(batch)
        if len(batch) < 100:
            return rows
        offset += len(batch)


def flatten_tree(tree, pid):
    if tree.get('projectId') != pid:
        raise MigrationError('ツリーのプロジェクトIDが不一致')
    result = {}
    for kind in ('activeTree', 'trashTree'):
        branch = tree.get(kind)
        if not isinstance(branch, dict) or not isinstance(branch.get('children'), list):
            raise MigrationError('未確認のツリー形式')
        def walk(children, parent):
            for order, child in enumerate(children):
                did = doc_id(child['id'])
                if did in result:
                    raise MigrationError('ツリーに重複・循環を検出')
                result[did] = {'id': did, 'parent': parent, 'order': order, 'trash': kind == 'trashTree'}
                walk(child['children'], did)
        walk(branch['children'], None)
    return result


def inventory(api, pid):
    rows = document_list(api, pid)
    tree = api.get('/documents/tree', {'projectIdOrKey': pid})
    flat = flatten_tree(tree, pid)
    count = api.get('/documents/count', {'projectIdOrKey': pid})['count']
    active = {k for k, v in flat.items() if not v['trash']}
    listed = {str(r['id']) for r in rows}
    # Public docs do not define whether list/count include trash. Accept only
    # a complete, internally consistent active or active+trash response.
    if listed not in (active, set(flat)) or count not in (len(active), len(flat)):
        raise MigrationError('一覧・ツリー・件数が不一致。更新停止と権限を確認してください')
    return {'rows': rows, 'tree': tree, 'flat': flat, 'count': count}


def rewrite_links(markdown, config, mapping, issues):
    reports = []
    source = config['source']['url']
    def replace(match):
        url = match[0]
        if not url.startswith(source + '/'):
            reports.append({'url': url, 'kind': 'external', 'resolved': True})
            return url
        parsed = urllib.parse.urlsplit(url)
        hit = re.fullmatch(r'/document/([A-Za-z0-9_-]+)', parsed.path)
        issue = re.fullmatch(r'/view/([A-Za-z0-9_]+-\d+)', parsed.path)
        if hit and hit[1] in mapping:
            new = config['destination']['url'] + '/document/' + mapping[hit[1]] + ('#' + parsed.fragment if parsed.fragment else '')
            if parsed.fragment or parsed.query:
                reports.append({'url': url, 'kind': 'document_anchor_or_query', 'resolved': False})
                return url
            reports.append({'url': url, 'kind': 'document', 'resolved': True, 'replacement': new})
            return new
        if issue and issue[1] in issues and not parsed.query and not parsed.fragment:
            new = issues[issue[1]]
            reports.append({'url': url, 'kind': 'issue', 'resolved': True, 'replacement': new})
            return new
        kind = 'document' if hit else 'issue' if issue else 'attachment_or_shared_file_or_other'
        reports.append({'url': url, 'kind': kind, 'resolved': False})
        return url
    # URL mapping is intentionally conservative. Unknown URL routes are reported.
    return re.sub(r'https?://[^\s<>\[\]()"\u200b]+', replace, markdown), reports


class DocumentMigrator:
    def __init__(self, config, src, dst, work, issue_mapping=None):
        self.config, self.src, self.dst, self.work = config, src, dst, Path(work)
        self.issue_mapping = {}
        if issue_mapping:
            with Path(issue_mapping).open(encoding='utf-8-sig', newline='') as f:
                for row in csv.DictReader(f):
                    if row.get('complete', '').lower() != 'true':
                        continue
                    if (row['source_url'] != config['source']['url'] + '/view/' + row['source_key'] or
                            row['destination_url'] != config['destination']['url'] + '/view/' + row['destination_key']):
                        raise MigrationError('課題対応CSVの移行元・先URLが設定と一致しません')
                    if row['source_key'] in self.issue_mapping:
                        raise MigrationError('課題対応CSVに重複があります')
                    self.issue_mapping[row['source_key']] = row['destination_url']

    def save(self, name, value):
        common.save_json(self.work / name, value)

    def project(self, api, side):
        return api.get('/projects/' + self.config[side]['project_key'])

    def marker(self, did):
        return '[BLDOC:' + self.snapshot['run_id'] + ':' + did + ']'

    def prepare(self):
        if (self.work / 'snapshot.json').exists() or (self.work / 'state.json').exists():
            raise MigrationError('ドキュメント準備済み。既存データを保持してください')
        # Refuse interrupted preparation too: do not overwrite backups automatically.
        if any(p.name != '.lock' for p in self.work.iterdir()):
            raise MigrationError('保存先にデータがあります。保持して別の --work-dir を指定してください')
        sp, dp = self.project(self.src, 'source'), self.project(self.dst, 'destination')
        if self.config['source']['url'] == self.config['destination']['url'] and sp['id'] == dp['id']:
            raise MigrationError('移行元と移行先が同じプロジェクトです')
        inv = inventory(self.src, sp['id'])
        records = []
        for did, position in inv['flat'].items():
            if position['trash']:
                continue
            detail = self.src.get('/documents/' + did)
            if str(detail['id']) != did or detail['projectId'] != sp['id']:
                raise MigrationError('詳細のIDが不一致')
            comments = self.src.get('/documents/' + did + '/comments')
            if not isinstance(comments, list):
                raise MigrationError('コメント応答が配列ではありません')
            record = {'document': detail, 'position': position, 'comments': comments, 'files': []}
            # Preserve raw metadata/comments even if a later download fails.
            self.save('source/' + did + '.json', record)
            for att in detail.get('attachments', []):
                aid = doc_id(att['id'])
                relative = 'files/' + did + '/' + aid
                got = self.src.download('/documents/' + did + '/attachments/' + aid, self.work / relative)
                if got['size'] != att['size']:
                    raise MigrationError('添付サイズが一致しません')
                record['files'].append({**att, **got, 'path': relative, 'destination_id': None})
            self.save('source/' + did + '.json', record)
            records.append(record)
        after = inventory(self.src, sp['id'])
        if common.digest(inv) != common.digest(after):
            raise MigrationError('取得中に移行元が変化しました')
        self.snapshot = {'schema': SCHEMA, 'run_id': common.uuid.uuid4().hex, 'identity': common.identity(self.config),
                         'source_project': sp, 'destination_project': dp, 'inventory': inv,
                         'records': records, 'options': self.config['options']}
        self.save('trash.json', {'count': sum(p['trash'] for p in inv['flat'].values()),
                                'documents': [p for p in inv['flat'].values() if p['trash']], 'raw_tree': inv['tree']})
        self.save('snapshot.json', self.snapshot)
        self.load()
        self.preview()
        common.say(f'ドキュメント準備完了: {len(records)}件。previewとcheck_reportを確認してください')

    def load(self):
        self.snapshot = common.read_json(self.work / 'snapshot.json')
        if (self.snapshot['identity'] != common.identity(self.config) or
                self.snapshot['options'] != self.config['options'] or self.snapshot['schema'] != SCHEMA):
            raise MigrationError('設定が準備時と異なります。既存データを保持してください')
        binding = {'snapshot': common.digest(self.snapshot), 'issues': common.digest(self.issue_mapping), 'converter': SCHEMA}
        self.journal = common.Journal(self.work / 'state.json', binding)
        self.dst.owned_ids = set(self.mapping().values())

    def mapping(self):
        return {k.split(':', 1)[1]: v['result']['id'] for k, v in self.journal.data['ops'].items()
                if k.startswith('document:') and v['status'] == 'done'}

    def selected(self, limit):
        # Snapshot uses tree preorder, so any prefix includes all parents and
        # preceding siblings, preserving addLast order across limited runs.
        return self.snapshot['records'][:limit] if limit else self.snapshot['records']

    def render(self, record):
        converter = Converter()
        body = converter.convert(record['document'].get('json'))
        body, links = rewrite_links(body, self.config, self.mapping(), self.issue_mapping)
        if self.config['options']['neutralize_mentions']:
            body = common.text_copy(body, True)
        return body, converter.problems, links

    def preview(self):
        reports = []
        for r in self.snapshot['records']:
            did = str(r['document']['id'])
            body, problems, links = self.render(r)
            path = self.work / 'preview' / (did + '.md')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(body + '\n', encoding='utf-8')
            reports.append({'id': did, 'title': r['document']['title'], 'conversion_problems': problems,
                            'links': links, 'unmigrated_comments': len(r['comments']),
                            'unmigrated_replies': sum(len(c.get('replies', [])) for c in r['comments']),
                            'unmigrated_attachments': r['files'],
                            'metadata_backup_only': ['createdUser', 'updatedUser', 'created', 'updated'],
                            'neutralize_mentions': self.config['options']['neutralize_mentions']})
        self.save('conversion_report.json', reports)
        return reports

    def check(self):
        self.load()
        errors = []
        sp, dp = self.project(self.src, 'source'), self.project(self.dst, 'destination')
        if sp['id'] != self.snapshot['source_project']['id'] or dp['id'] != self.snapshot['destination_project']['id']:
            errors.append('プロジェクトIDが準備時から変化しました')
        if common.digest(inventory(self.src, sp['id'])) != common.digest(self.snapshot['inventory']):
            errors.append('移行元の一覧・本文・ツリーが準備時から変化しました')
        for r in self.snapshot['records']:
            did = str(r['document']['id'])
            if self.src.get('/documents/' + did) != r['document'] or self.src.get('/documents/' + did + '/comments') != r['comments']:
                errors.append(did + ': 詳細・コメントが準備時から変化しました')
            for f in r['files']:
                path = self.work / f['path']
                if not path.is_file() or common.file_digest(path) != f['sha256'] or path.stat().st_size != f['size']:
                    errors.append(did + ': 保存添付が不一致')
        preview = self.preview()
        errors += [p['id'] + ': 未対応の本文形式あり' for p in preview if p['conversion_problems']]
        inv = inventory(self.dst, dp['id'])
        known = set(self.mapping().values())
        pending = {self.marker(k.split(':', 1)[1]) for k, v in self.journal.data['ops'].items()
                   if k.startswith('document:') and v['status'] == 'pending'}
        for row in inv['rows']:
            if str(row['id']) not in known and not any(m in row.get('plain', '') for m in pending):
                errors.append('移行先に管理外のドキュメントあり（自動統合しません）')
        if any(p['trash'] for p in inv['flat'].values()):
            errors.append('移行先にゴミ箱内のドキュメントがあります')
        self.save('check_report.json', {'errors': errors, 'documents': len(preview),
                                        'partial': any(p['unmigrated_comments'] or p['unmigrated_attachments'] or
                                                       any(not l['resolved'] for l in p['links']) for p in preview),
                                        'metadata_backup_only': True})
        if errors:
            raise MigrationError('ドキュメント事前チェック失敗。check_report.jsonを確認してください')
        common.say('DOCUMENT CHECK OK（作成者・日時等はバックアップのみ。conversion_report.json参照）')

    def migrate(self, apply=False, limit=0, retry_pending=False):
        if not apply:
            raise MigrationError('書き込みには --apply が必要です')
        self.check()
        for record in self.selected(limit):
            document = record['document']
            did = str(document['id'])
            key = 'document:' + did
            previous = self.journal.get(key)
            if previous and previous['status'] == 'done':
                target = previous['result']
            else:
                body, problems, links = self.render(record)
                content = body + '\n\n' + Converter.escape(self.marker(did))
                form = {'projectId': self.snapshot['destination_project']['id'], 'title': document['title'],
                        'content': content, 'addLast': 'true'}
                if document.get('emoji'):
                    form['emoji'] = document['emoji']
                parent = record['position']['parent']
                if parent:
                    if parent not in self.mapping():
                        raise MigrationError('親ドキュメントが未作成')
                    form['parentId'] = self.mapping()[parent]
                meta = (previous or {}).get('meta') or {'form': form, 'links': links}
                def recover(_):
                    rows = document_list(self.dst, self.snapshot['destination_project']['id'])
                    hits = [r for r in rows if self.marker(did) in r.get('plain', '')]
                    if len(hits) > 1:
                        raise MigrationError('移行マーカーが重複しています')
                    return {'id': doc_id(hits[0]['id'])} if hits else None
                def action():
                    result = self.dst.post('/documents', meta['form'])
                    return {'id': doc_id(result['id'])}
                target = self.journal.run(key, action, recover, meta=meta, retry_pending=retry_pending)
            tid = target['id']
            self.dst.owned_ids.add(tid)
            tags = sorted(set(t['name'] for t in document.get('tags', [])))
            if tags:
                def recover_tags(_):
                    actual = {t['name'] for t in self.dst.get('/documents/' + tid).get('tags', [])}
                    return {'names': tags} if set(tags) <= actual else None
                def add_tags():
                    actual = {t['name'] for t in self.dst.get('/documents/' + tid).get('tags', [])}
                    missing = sorted(set(tags) - actual)
                    if missing:
                        self.dst.post('/documents/' + tid + '/tags', {'tagNames[]': missing})
                    return {'names': tags}
                self.journal.run('tags:' + did, add_tags, recover_tags, retry_pending=retry_pending)
            self.save('document_mapping.json', self.mapping())
        common.say('選択したドキュメントの作成終了。verifyと画面確認を行ってください')

    def verify(self, limit=0):
        self.load()
        dp = self.project(self.dst, 'destination')
        if dp['id'] != self.snapshot['destination_project']['id']:
            raise MigrationError('移行先プロジェクトIDが不一致')
        inv = inventory(self.dst, dp['id'])
        results = []
        for r in self.selected(limit):
            did = str(r['document']['id'])
            tid = self.mapping().get(did)
            problems = []
            if not tid or tid not in inv['flat'] or inv['flat'][tid]['trash']:
                problems.append('未移行または削除済み')
            else:
                t = self.dst.get('/documents/' + tid)
                pos = inv['flat'][tid]
                expected = self.journal.get('document:' + did)['meta']['form']
                converter = Converter()
                actual = converter.convert(t.get('json'))
                if converter.problems or normalize(actual) != normalize(expected['content']):
                    problems.append('本文変換結果（サーバー整形差も要確認）')
                if t['title'] != r['document']['title']:
                    problems.append('タイトル')
                if (t.get('emoji') or '') != (r['document'].get('emoji') or ''):
                    problems.append('絵文字')
                if t['projectId'] != dp['id']:
                    problems.append('プロジェクトID')
                if pos['parent'] != self.mapping().get(r['position']['parent']) or pos['order'] != r['position']['order']:
                    problems.append('階層・並び順')
                if {x['name'] for x in t.get('tags', [])} != {x['name'] for x in r['document'].get('tags', [])}:
                    problems.append('タグ')
            results.append({'source_id': did, 'destination_id': tid, 'ok': not problems, 'problems': problems,
                            'links': (self.journal.get('document:' + did) or {}).get('meta', {}).get('links', []),
                            'comments_backup_only': len(r['comments']), 'attachments_backup_only': r['files']})
        source_unchanged = common.digest(inventory(self.src, self.snapshot['source_project']['id'])) == common.digest(self.snapshot['inventory'])
        backup_errors = []
        for r in self.snapshot['records']:
            did = str(r['document']['id'])
            if self.src.get('/documents/' + did) != r['document'] or self.src.get('/documents/' + did + '/comments') != r['comments']:
                source_unchanged = False
            for f in r['files']:
                path = self.work / f['path']
                if not path.is_file() or common.file_digest(path) != f['sha256'] or path.stat().st_size != f['size']:
                    source_unchanged = False
                    backup_errors.append({'document_id': did, 'attachment_id': f['id'], 'problem': '保存添付の欠落・ハッシュ不一致'})
        report = {'source_count': len(self.snapshot['records']), 'checked': len(results),
                  'success': sum(r['ok'] for r in results), 'failed': sum(not r['ok'] for r in results),
                  'unmigrated': len(self.snapshot['records']) - len(self.mapping()),
                  'source_unchanged': source_unchanged, 'backup_errors': backup_errors, 'status': 'partial',
                  'reason': '作成者・日時はバックアップのみ。コメント・添付・未解決リンクは個別結果参照',
                  'results': results}
        self.save('verification_report.json', report)
        if report['failed'] or not source_unchanged:
            raise MigrationError('照合に不一致があります。verification_report.jsonを確認してください')
        common.say(f"DOCUMENT VERIFY OK: {report['success']}件（完全移行ではありません）")


def main(argv=None):
    p = argparse.ArgumentParser(description='Backlogドキュメント移行（課題移行と独立）')
    p.add_argument('command', choices=['prepare', 'preview', 'check', 'migrate', 'verify'])
    p.add_argument('--config', default='config.json')
    p.add_argument('--work-dir', help='既定: 課題work_dir名に-documentsを付けた別フォルダー')
    p.add_argument('--issue-mapping', help='任意: 既存課題移行のissue_mapping.csv')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--limit', type=int, default=0, help='ツリー順の先頭N件。0は全件')
    p.add_argument('--retry-pending', action='store_true')
    args = p.parse_args(argv)
    if args.limit < 0 or (args.limit and args.command not in ('migrate', 'verify')):
        p.error('--limitはmigrate/verifyで0以上を指定します')
    if (args.apply or args.retry_pending) and args.command != 'migrate':
        p.error('--apply/--retry-pendingはmigrate専用です')
    secrets = set()
    work = None
    try:
        os.umask(0o077)
        config = common.load_config(Path(args.config))
        for side, default in [('source', 'BACKLOG_SRC_API_KEY'), ('destination', 'BACKLOG_DST_API_KEY')]:
            direct = config[side].get('api_key', '')
            if isinstance(direct, str):
                secrets.add(direct.strip())
            secrets.add(os.environ.get(config[side].get('api_key_env', default), '').strip())
        original = Path(config['work_dir']).resolve()
        candidate = Path(args.work_dir).expanduser().resolve() if args.work_dir else original.with_name(original.name + '-documents')
        if candidate == original or original in candidate.parents or candidate in original.parents:
            raise MigrationError('課題の作業先と重なる保存先は指定できません')
        work = candidate
        src0, dst0 = common.make_clients(config)
        src = DocumentAPI(src0.url, src0.key, writable=False, options=config['options'])
        dst = DocumentAPI(dst0.url, dst0.key, writable=True, options=config['options'])
        with common.work_lock(work):
            migrator = DocumentMigrator(config, src, dst, work, args.issue_mapping)
            if args.command == 'preview':
                migrator.load()
                migrator.preview()
            elif args.command == 'migrate':
                migrator.migrate(args.apply, args.limit, args.retry_pending)
            elif args.command == 'verify':
                migrator.verify(args.limit)
            else:
                getattr(migrator, args.command)()
        return 0
    except KeyboardInterrupt:
        common.say('中断しました。ドキュメント作業先を保持してください')
        return 130
    except (MigrationError, OSError, ValueError, KeyError, TypeError) as e:
        message = str(e)
        for key in sorted(secrets, key=len, reverse=True):
            if key:
                message = message.replace(key, '[REDACTED]')
        if work and work.is_dir():
            common.save_json(work / 'last_error.json', {'at': common.stamp(), 'command': args.command,
                                                       'error': message})
        common.say('ERROR: ' + message)
        return 1


if __name__ == '__main__':
    sys.exit(main())
