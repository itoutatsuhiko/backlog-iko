"""Synthetic public-schema fixtures; these are not captured Backlog responses."""
import copy
import contextlib
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
import backlog_migrate as b
import backlog_documents as d
from document_markdown import Converter


def text(s, **kwargs):
    return {'type': 'text', 'text': s, **kwargs}


def node(typ, *content, **attrs):
    return {'type': typ, 'content': list(content), **({'attrs': attrs} if attrs else {})}


def body(s):
    return node('doc', node('paragraph', text(s)))


def doc(did, title=None):
    return {'id': did, 'projectId': 1, 'title': title or did, 'json': body('本文'), 'plain': '本文',
            'emoji': '📝', 'tags': [{'id': 3, 'name': '設計'}], 'attachments': [],
            'createdUser': {'id': 1}, 'updatedUser': {'id': 1}, 'created': '2026-01-01', 'updated': '2026-01-02'}


class Fake:
    def __init__(self, destination=False, size=3):
        self.destination = destination
        self.pid = 2 if destination else 1
        self.rows = {} if destination else {f'd{i}': doc(f'd{i}') for i in range(size)}
        self.positions = {} if destination else {f'd{i}': {'parent': 'd0' if i else None, 'order': i-1 if i else 0} for i in range(size)}
        self.comments = {k: [] for k in self.rows}
        self.calls = []
        self.owned_ids = set()
        self.lost = None
        self.counter = 0
        self.trash = []

    def tree(self):
        def children(parent):
            return [{'id': did, 'name': self.rows[did]['title'], 'children': children(did)}
                    for did, pos in sorted(self.positions.items(), key=lambda v: v[1]['order']) if pos['parent'] == parent]
        return {'projectId': self.pid, 'activeTree': {'id': 'Active', 'children': children(None)},
                'trashTree': {'id': 'Trash', 'children': self.trash}}

    def get(self, path, params=None):
        self.calls.append(('GET', path, copy.deepcopy(params)))
        params = params or {}
        if path.startswith('/projects/'):
            return {'id': self.pid}
        if path == '/documents/tree':
            return self.tree()
        if path == '/documents/count':
            return {'count': len(self.rows)}
        if path == '/documents':
            return copy.deepcopy(list(self.rows.values())[params['offset']:params['offset']+params['count']])
        parts = path.split('/')
        if len(parts) == 4 and parts[3] == 'comments':
            return copy.deepcopy(self.comments.get(parts[2], []))
        return copy.deepcopy(self.rows[parts[2]])

    def post(self, path, form):
        assert self.destination
        self.calls.append(('POST', path, copy.deepcopy(form)))
        if path == '/documents':
            self.counter += 1
            did = f'n{self.counter}'
            row = doc(did, form['title'])
            row.update(projectId=2, emoji=form.get('emoji'), tags=[])
            # Synthetic parser for paragraph-only lifecycle fixtures.
            unescaped = re.sub(r'\\(.)', r'\1', form['content'])
            row['json'] = node('doc', *[node('paragraph', text(p)) for p in unescaped.split('\n\n')])
            row['plain'] = unescaped
            self.rows[did] = row
            parent = form.get('parentId')
            self.positions[did] = {'parent': parent, 'order': sum(p['parent'] == parent for p in self.positions.values())}
            result = copy.deepcopy(row)
            kind = 'document'
        else:
            did = path.split('/')[2]
            self.rows[did]['tags'] += [{'id': i, 'name': name} for i, name in enumerate(form['tagNames[]'])]
            result, kind = self.rows[did]['tags'], 'tags'
        if self.lost == kind:
            self.lost = None
            raise b.APIError('lost response', uncertain=True, method='POST')
        return result

    def download(self, path, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'ABC')
        return {'size': 3, 'sha256': b.file_digest(target)}


class ConversionTests(unittest.TestCase):
    def test_heading_marks_list_table_link(self):
        value = node('doc', node('heading', text('見出し'), level=2),
                     node('paragraph', text('太字', marks=[{'type': 'bold'}]), text('斜体', marks=[{'type': 'italic'}]),
                          text('外部', marks=[{'type': 'link', 'attrs': {'href': 'https://example.com'}}])),
                     node('bulletList', node('listItem', node('paragraph', text('項目')))),
                     node('table', node('tableRow', node('tableHeader', node('paragraph', text('列')))),
                          node('tableRow', node('tableCell', node('paragraph', text('値'))))))
        c = Converter()
        md = c.convert(value)
        self.assertFalse(c.problems)
        for wanted in ('## 見出し', '**太字**', '*斜体*', '- 項目', '| --- |', '[外部](<https://example.com>)'):
            self.assertIn(wanted, md)

    def test_code_quote_ordered_and_escape(self):
        c = Converter()
        md = c.convert(node('doc', node('codeBlock', text('x```y'), language='python'),
                            node('blockquote', node('paragraph', text('引用'))),
                            node('orderedList', node('listItem', node('paragraph', text('*文字*'))), start=3)))
        self.assertFalse(c.problems)
        self.assertIn('````python', md)
        self.assertIn('> 引用', md)
        self.assertIn('3. \\*文字\\*', md)

    def test_unknown_node_mark_and_attribute_block(self):
        for value in (node('doc', node('embed')), node('doc', node('paragraph', text('x', marks=[{'type': 'underline'}]))),
                      node('doc', node('paragraph', text('x'), textAlign='center')), {'plain': 'hello'}):
            c = Converter()
            c.convert(value)
            self.assertTrue(c.problems)
            self.assertIn('path', c.problems[0])

    def test_image_is_reported(self):
        c = Converter()
        self.assertIn('![', c.convert(node('doc', node('image', src='https://example.com/a.png'))))
        self.assertTrue(c.problems)

    def test_json_string(self):
        import json
        c = Converter()
        self.assertEqual(c.convert(json.dumps(body('hello'))), 'hello')
        self.assertFalse(c.problems)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.cfg = b.load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
        self.src, self.dst = Fake(), Fake(True)
        self.m = d.DocumentMigrator(self.cfg, self.src, self.dst, self.work)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.temp.cleanup()

    def test_page_201_and_detail_readonly_prepare(self):
        self.src = Fake(size=201)
        self.m.src = self.src
        self.m.prepare()
        self.assertEqual(len(b.read_json(self.work / 'snapshot.json')['records']), 201)
        self.assertEqual(sum(c[1] == '/documents' for c in self.src.calls), 6)
        self.assertTrue(all(c[0] == 'GET' for c in self.src.calls + self.dst.calls))

    def test_hierarchy_order_limit_resume_verify_no_duplicates(self):
        self.m.prepare()
        self.m.migrate(True, 2)
        self.assertEqual(len(self.dst.rows), 2)
        self.m.verify(2)
        self.m.migrate(True)
        before = len([c for c in self.dst.calls if c[0] == 'POST'])
        self.m.migrate(True)
        self.assertEqual(len([c for c in self.dst.calls if c[0] == 'POST']), before)
        self.m.verify()
        self.assertEqual(self.dst.positions['n2'], {'parent': 'n1', 'order': 0})
        self.assertEqual(self.dst.positions['n3'], {'parent': 'n1', 'order': 1})
        self.assertEqual(self.dst.rows['n1']['tags'][0]['name'], '設計')

    def test_lost_document_response_recovers(self):
        self.m.prepare()
        self.dst.lost = 'document'
        with self.assertRaises(b.APIError):
            self.m.migrate(True)
        self.m.migrate(True)
        self.assertEqual(len(self.dst.rows), 3)
        self.m.verify()

    def test_lost_tag_response_recovers(self):
        self.m.prepare()
        self.dst.lost = 'tags'
        with self.assertRaises(b.APIError):
            self.m.migrate(True)
        self.m.migrate(True)
        self.assertEqual(len(self.dst.rows['n1']['tags']), 1)

    def test_pending_without_marker_does_not_retry(self):
        self.m.prepare()
        self.m.journal.pending('document:d0', {'form': {}})
        with self.assertRaisesRegex(b.MigrationError, '結果未確定'):
            self.m.migrate(True)
        self.assertFalse(any(c[0] == 'POST' for c in self.dst.calls))

    def test_backup_comments_replies_attachments_and_trash(self):
        self.src.comments['d0'] = [{'id': 'c', 'createdUser': {'id': 1}, 'replies': [{'id': 'reply'}]}]
        self.src.rows['d0']['attachments'] = [{'id': 1, 'name': '添付.txt', 'size': 3}]
        self.src.trash = [{'id': 'deleted', 'name': '削除文書', 'children': []}]
        self.m.prepare()
        report = b.read_json(self.work / 'conversion_report.json')[0]
        self.assertEqual(report['unmigrated_comments'], 1)
        self.assertEqual(report['unmigrated_replies'], 1)
        self.assertEqual(report['unmigrated_attachments'][0]['destination_id'], None)
        self.assertEqual(b.read_json(self.work / 'trash.json')['count'], 1)
        self.m.check()
        (self.work / 'files/d0/1').write_bytes(b'BAD')
        with self.assertRaises(b.MigrationError):
            self.m.check()

    def test_unknown_conversion_stops_all_writes(self):
        self.src.rows['d1']['json'] = node('doc', node('mention'))
        self.m.prepare()
        with self.assertRaises(b.MigrationError):
            self.m.migrate(True)
        self.assertFalse(self.dst.rows)

    def test_source_changes_block(self):
        self.m.prepare()
        self.src.rows['d0']['updated'] = 'new'
        with self.assertRaises(b.MigrationError):
            self.m.check()

    def test_existing_same_title_is_not_merged(self):
        self.m.prepare()
        self.dst.rows['existing'] = {**doc('existing'), 'projectId': 2}
        self.dst.positions['existing'] = {'parent': None, 'order': 0}
        with self.assertRaises(b.MigrationError):
            self.m.migrate(True)
        self.assertEqual(len(self.dst.rows), 1)

    def test_apply_required_and_preparation_not_overwritten(self):
        self.m.prepare()
        with self.assertRaises(b.MigrationError):
            self.m.migrate(False)
        with self.assertRaises(b.MigrationError):
            self.m.prepare()

    def test_verify_detects_order_and_title_changes(self):
        self.m.prepare()
        self.m.migrate(True)
        self.dst.rows['n1']['title'] = 'changed'
        self.dst.positions['n2']['order'] = 7
        with self.assertRaises(b.MigrationError):
            self.m.verify()
        report = b.read_json(self.work / 'verification_report.json')
        # Reordering one sibling also moves the other sibling's position.
        self.assertEqual(report['failed'], 3)

    def test_link_mapping_only_created_documents(self):
        url = self.cfg['source']['url']
        out, report = d.rewrite_links(f'{url}/document/d0 {url}/document/d1 https://example.com', self.cfg, {'d0': 'n1'}, {})
        self.assertIn('/document/n1', out)
        self.assertIn('/document/d1', out)
        self.assertEqual(sum(not r['resolved'] for r in report), 1)

    def test_issue_mapping_validates_scope(self):
        path = self.work / 'issues.csv'
        path.write_text('source_key,destination_key,source_url,destination_url,complete\nOLD-1,NEW-1,https://wrong/view/OLD-1,https://wrong/view/NEW-1,True\n')
        with self.assertRaises(b.MigrationError):
            d.DocumentMigrator(self.cfg, self.src, self.dst, self.work, path)

    def test_forward_links_are_reported_and_not_rewritten_later(self):
        url = self.cfg['source']['url']
        self.src.rows['d0']['json'] = body(url + '/document/d2')
        self.m.prepare()
        self.m.migrate(True)
        self.assertIn(url + '/document/d2', self.dst.rows['n1']['plain'])
        self.m.verify()
        result = b.read_json(self.work / 'verification_report.json')['results'][0]
        self.assertFalse(result['links'][0]['resolved'])

    def test_verify_detects_body_and_tag_changes(self):
        self.m.prepare()
        self.m.migrate(True)
        self.dst.rows['n1']['json'] = body('changed')
        self.dst.rows['n2']['tags'] = []
        with self.assertRaises(b.MigrationError):
            self.m.verify()
        report = b.read_json(self.work / 'verification_report.json')
        self.assertEqual(report['failed'], 2)


class GuardTests(unittest.TestCase):
    def test_cli_does_not_touch_issue_work(self):
        with tempfile.TemporaryDirectory() as temp:
            issue_work = Path(temp) / 'work'
            issue_work.mkdir()
            state = issue_work / 'state.json'
            state.write_text('existing issue state')
            cfg = b.load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
            cfg['work_dir'] = str(issue_work)
            with patch.object(b, 'load_config', return_value=cfg), patch.object(b, 'make_clients') as clients, contextlib.redirect_stdout(io.StringIO()):
                old_umask = b.os.umask(0o077)
                try:
                    self.assertEqual(d.main(['prepare', '--work-dir', str(issue_work)]), 1)
                finally:
                    b.os.umask(old_umask)
            clients.assert_not_called()
            self.assertEqual(state.read_text(), 'existing issue state')
            self.assertEqual([p.name for p in issue_work.iterdir()], ['state.json'])

    def test_cli_error_file_masks_config_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = b.load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
            cfg['work_dir'] = str(Path(temp) / 'issues')
            cfg['source']['api_key'] = 'not-a-real-secret'
            output = io.StringIO()
            with patch.object(b, 'load_config', return_value=cfg), \
                    patch.object(b, 'make_clients', return_value=(Mock(url='https://a.backlog.jp', key='a'), Mock(url='https://b.backlog.jp', key='b'))), \
                    patch.object(d.DocumentMigrator, 'prepare', side_effect=b.APIError('not-a-real-secret')), \
                    contextlib.redirect_stdout(output):
                old_umask = b.os.umask(0o077)
                try:
                    self.assertEqual(d.main(['prepare']), 1)
                finally:
                    b.os.umask(old_umask)
            error = (Path(temp) / 'issues-documents/last_error.json').read_text()
            self.assertNotIn('not-a-real-secret', error + output.getvalue())
            self.assertIn('[REDACTED]', error)

    def test_document_allowlist_is_separate_and_owned(self):
        api = d.DocumentAPI('https://a.backlog.jp', 'dummy', writable=True, options=b.DEFAULT_OPTIONS)
        api._authorize_write('POST', '/documents')
        for method, path in [('POST', '/issues'), ('PATCH', '/documents/a'), ('DELETE', '/documents/a'),
                             ('POST', '/documents/a/comments'), ('POST', '/documents/a/tags'), ('POST', '/projects/1/statuses')]:
            with self.assertRaises(b.MigrationError):
                api._authorize_write(method, path)
        api.owned_ids.add('a')
        api._authorize_write('POST', '/documents/a/tags')
        api.writable = False
        with self.assertRaises(b.MigrationError):
            api._authorize_write('POST', '/documents')
        issue_api = b.BacklogAPI('https://a.backlog.jp', 'dummy', writable=True, options=b.DEFAULT_OPTIONS)
        with self.assertRaises(b.MigrationError):
            issue_api._authorize_write('POST', '/documents')

    def test_document_rate_limit_uses_existing_client(self):
        import urllib.error
        api = d.DocumentAPI('https://a.backlog.jp', 'dummy', writable=True, options=b.DEFAULT_OPTIONS)
        response = Mock()
        response.headers = {}
        response.read.return_value = b'{"id":"new"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        fail = urllib.error.HTTPError('https://a.backlog.jp', 429, 'rate', {}, io.BytesIO(b'{}'))
        api.opener = Mock()
        api.opener.open.side_effect = [fail, response]
        try:
            with patch.object(b.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(api.post('/documents', {'title': 'x'})['id'], 'new')
            self.assertEqual(api.opener.open.call_count, 2)
        finally:
            fail.close()


if __name__ == '__main__':
    unittest.main()
