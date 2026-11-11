"""Conservative ProseMirror-style JSON to Markdown conversion.

Only the public heading/paragraph/text example is confirmed by Nulab. Other
standard nodes are supported provisionally and require a real-data preview.
Unknown nodes/marks/meaningful attributes are reported, never silently accepted.
"""
import json
import re


class Converter:
    def __init__(self):
        self.problems = []

    def problem(self, path, message):
        self.problems.append({"path": path, "message": message})

    def convert(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                self.problem("$", "本文JSONを解析できません")
                return ""
        if not isinstance(value, dict) or value.get("type") != "doc":
            self.problem("$", "未確認の本文形式（plainへの自動置換はしません）")
            return ""
        try:
            return self.node(value, "$").strip()
        except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
            self.problem("$", "未確認のJSON構造・属性値（プレビューを原文と照合してください）")
            return ""

    @staticmethod
    def escape(text):
        # Keep bare URLs intact so Markdown escaping cannot hide old-space
        # links from the mapping/report stage.
        return ''.join(part if part.startswith(('https://', 'http://')) else
                       re.sub(r'([\\`*_{}\[\]<>#!|~.()+\-=])', r'\\\1', part)
                       for part in re.split(r'(https?://[^\s<>\[\]()]*)', text))

    def node(self, node, path, raw=False):
        if not isinstance(node, dict):
            self.problem(path, "ノードがオブジェクトではありません")
            return ""
        typ = node.get("type")
        for key in set(node) - {"type", "attrs", "content", "text", "marks"}:
            self.problem(path, f"未対応ノード項目: {key}")
        attrs = node.get("attrs") or {}
        allowed = {"heading": {"level", "id"}, "paragraph": {"id"},
                   "orderedList": {"start"}, "codeBlock": {"language"},
                   "image": {"src", "alt", "title"},
                   "tableCell": {"colspan", "rowspan", "colwidth"},
                   "tableHeader": {"colspan", "rowspan", "colwidth"}}
        for key, value in attrs.items():
            if key not in allowed.get(typ, set()) and value is not None:
                self.problem(path, f"未対応属性: {typ}.{key}")
        children = node.get("content", [])
        if not isinstance(children, list):
            self.problem(path, "contentが配列ではありません")
            return ""
        rendered = [self.node(c, f"{path}.content[{i}]", raw or typ == "codeBlock") for i, c in enumerate(children)]
        body = "".join(rendered)
        if typ == "text":
            text = node.get("text", "")
            if not isinstance(text, str):
                self.problem(path, "textが文字列ではありません")
                return ""
            marks = node.get("marks", [])
            body = text if raw or any(m.get("type") == "code" for m in marks) else self.escape(text)
            for m in marks:
                kind, ma = m.get("type"), m.get("attrs") or {}
                if kind == "bold":
                    body = f"**{body}**"
                elif kind == "italic":
                    body = f"*{body}*"
                elif kind == "strike":
                    body = f"~~{body}~~"
                elif kind == "code":
                    fence = '`' * (max([len(x) for x in re.findall(r'`+', text)] or [0]) + 1)
                    body = fence + ' ' + text + ' ' + fence
                elif kind == "link":
                    href = ma.get("href", "")
                    body = f"[{body}](<{href}>)"
                    if ma.get("title"):
                        self.problem(path, "リンクtitle属性は未対応")
                else:
                    self.problem(path, f"未対応の書式: {kind}")
                for key, value in ma.items():
                    if key not in ({"href", "target", "rel", "class"} if kind == "link" else set()) and value is not None:
                        self.problem(path, f"未対応の書式属性: {kind}.{key}")
            return body
        if typ == "doc":
            return body
        if typ == "paragraph":
            return body + "\n\n"
        if typ == "heading":
            level = attrs.get("level", 1)
            if type(level) is not int or not 1 <= level <= 6:
                self.problem(path, "見出しレベルが不正")
                level = 1
            return '#' * level + ' ' + body + '\n\n'
        if typ == "hardBreak":
            return '  \n'
        if typ == "horizontalRule":
            return '\n---\n\n'
        if typ == "blockquote":
            return '\n'.join('> ' + line for line in body.strip().splitlines()) + '\n\n'
        if typ in ("bulletList", "orderedList"):
            start = attrs.get("start", 1)
            result = []
            for i, item in enumerate(rendered):
                prefix = f"{start + i}. " if typ == "orderedList" else '- '
                lines = item.strip().splitlines() or ['']
                result.append(prefix + lines[0] + '\n' + '\n'.join(' ' * len(prefix) + s for s in lines[1:]))
            return '\n'.join(result).rstrip() + '\n\n'
        if typ == "listItem":
            return body
        if typ == "codeBlock":
            fence = '`' * max(3, max([len(x) for x in re.findall(r'`+', body)] or [0]) + 1)
            return f"{fence}{attrs.get('language') or ''}\n{body}\n{fence}\n\n"
        if typ == "image":
            self.problem(path, "画像は旧URL参照のまま。添付・埋め込み表示の復元は未対応")
            return f"![{self.escape(attrs.get('alt') or '')}](<{attrs.get('src', '')}>)"
        if typ == "table":
            rows = []
            for row in children:
                cells = row.get("content", [])
                rows.append([self.node(cell, path + '.tableCell').strip().replace('\n', '<br>') for cell in cells])
            if not rows:
                return ''
            if not all(c.get('type') == 'tableHeader' for c in children[0].get('content', [])):
                self.problem(path, "Markdown表の先頭行は見出しになります")
            width = len(rows[0])
            if any(len(r) != width for r in rows):
                self.problem(path, "列数が異なる表")
            lines = ['| ' + ' | '.join(r) + ' |' for r in rows]
            lines.insert(1, '| ' + ' | '.join(['---'] * width) + ' |')
            return '\n'.join(lines) + '\n\n'
        if typ in ("tableRow", "tableCell", "tableHeader"):
            if attrs.get('colspan', 1) != 1 or attrs.get('rowspan', 1) != 1 or attrs.get('colwidth'):
                self.problem(path, "表の結合セル・列幅は未対応")
            return body
        self.problem(path, f"未対応ノード: {typ}（埋め込み・メンションを含む）")
        return body


def normalize(markdown):
    return '\n'.join(line.rstrip() for line in markdown.strip().splitlines())
