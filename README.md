# Backlog 課題移行ツール

別スペースへ課題・コメント・添付・親子関係をコピーするPython製CLIです。
Python 3.10以上、Mac / Linux、標準ライブラリのみで動作します。

**操作方法・制限は [README_JA.md](README_JA.md) を参照してください。**
短い操作手順は [START_HERE.txt](START_HERE.txt)、検証・引き継ぎ記録は
[HANDOFF.md](HANDOFF.md) にあります。

## 共同利用の準備

リポジトリ取得後、そのフォルダーで実行します。設定ファイルが既にある場合はコピーせず、既存設定を確認してください。

```bash
cp config.example.json config.json
```

各利用者が `config.json` の移行元・先のURL、プロジェクトキー、`api_key` を編集します。
`source.api_key` は移行元、`destination.api_key` は移行先のキーです。
保存後は移行コマンドを実行できます。`source ./api_keys.sh` は不要です。

従来の環境変数方式も利用できます。`api_key` が空または省略された場合だけ環境変数から読み取ります。
詳しくは [README_JA.md](README_JA.md) の設定手順を参照してください。

`config.json`、`config*.json`、`*.config.json`、`config.local/`、
`work*/`、`.env*`、ログ、PythonのキャッシュはGit管理から除外します。
`config.example.json` は共有するため管理対象です。サンプルには実環境の情報やキーを記載しないでください。

別名の設定は `config.test.json` / `config.production.json`、作業先は
`work-test/` / `work-production/` のように除外規則に合う名前を使います。
任意の別名や別の保存先を使う場合は、先に `.gitignore` へ追加してください。
除外されたファイルを `git add -f` で追加しないでください。

設定・スナップショット・添付・処理記録は各移行作業で保管します。
同じ移行を複数人・複数端末から同時実行しないでください。
途中再開には同じ設定・対応表・work全体を使い、別の移行先には別のworkを使います。
Gitには移行データを保存しないため、作業データの保管・引き継ぎは社内の安全な方法で別途行います。

## オフライン検証

```bash
python3 backlog_migrate.py --help
python3 -m unittest discover -s tests -v
```

この検証は実Backlogへ接続しません。`prepare` / `check` / `verify` は実環境からの
読み取りとローカル保存、`migrate --apply` は移行先への書き込みを行います。
実行前に対象と承認範囲を確認してください。

## 変更の共有

変更後はテストを実行し、`git status` と `git diff --cached` で
設定・キー・業務データが含まれていないことを確認してコミットします。
共有リポジトリのURLとアクセス権は別途決めてください。業務用ツールは非公開リポジトリを推奨します。
