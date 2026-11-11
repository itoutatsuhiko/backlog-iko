# Backlog移行ツール 引き継ぎ（2026-11-10）

## 目的と確定条件

Macから別スペースへ約400課題を移行する。Git移行は不要。元・先の状態は9〜12個で、移行先の既存IDを利用する。公式ツールの状態上限エラーの根本原因は未確定。

実スペースへの接続は未承認。本作業はローカル確認・模擬APIテストと公開公式ドキュメントの閲覧のみ。APIキーの有無を接続承認と解釈しない。移行元はGETのみ、移行先の状態追加・改名・削除は禁止。本番書き込みは対象・件数・操作の説明と明示的承認が必要。少数件と全件は別承認。

## 確認した配置・作業状態

- ルート: このリポジトリのルート（backlog_api_migrator）
- `backlog_migrate.py`（VERSION=1.0.0）、`README_JA.md`、`config.example.json`、`api_keys.sh`、`START_HERE.txt`、`TEST_RESULTS.txt`、`.gitignore`、`tests/test_migration.py` を確認。
- 対象ルート・上位階層に適用されるAGENTS.mdは見つからなかった。
- 初回調査時はGitリポジトリではなく、過去の変更履歴は比較できなかった。その後、ユーザーの依頼によりローカルGit管理を開始。共有先へのアップロードは未実施。
- 調査時点でconfig.json、work/、スナップショット、state.json、実添付、別名設定ファイルは見当たらなかった。他の場所での過去の実行有無は未確認。
- APIキーの環境変数は調査・表示していない。api_keys.shには入力処理のみが記載されている。

## 実装の把握とレビュー結果

標準ライブラリのみのCLI。BacklogAPIが認証・通信・待機・書き込み許可を制限し、MapperがIDと属性値を変換し、Journalがpending/doneを保存し、Migratorがprepare/check/migrate/verifyを担当する。

- 課題は100件ずつoffsetで取得、重複・件数APIとの一致を検査。コメントも100件ずつminIdで取得し、重複を除去して件数を確認する。
- 状態等は一意の同名対応。ユーザーはNulab ID、メールで対応し、表示名のみでは決めない。明示したusers=-1のみ担当者未設定にする。
- カスタム属性は型・選択肢・必須項目・種別適用を確認し、リストのその他入力・未知の型は停止する。
- 親の再帰的な作成順を確保し循環を拒否。limitには必要な親が追加される。
- 添付はサイズ・SHA-256を保存し、1ファイル1移行コメントに添付。元コメントとの所属は復元しない。
- 操作前にpendingを保存。応答消失後は末尾マーカーで復旧し、一意に確認できなければ停止。POSTの通信断・5xxは無条件再送しない。--retry-pendingは人手確認後に限る。
- 設定・対応表・スナップショットのハッシュを処理記録へ結び付ける。--apply、排他ロック、既存課題の拒否、読み取り専用元クライアントを維持。
- 登録者・日時は本文で補足。標準監査履歴・本文リンク書換・継続同期は対象外。Wiki・ドキュメント・共有ファイルも未実装。
- neutralize_mentionsはメールアドレス等も変換し得る。原文はローカルに保存。全通知の抑止ではない。

主要処理と引き継ぎの重大な不一致は見つからなかった。ただしREADMEの「設定インポート済み」は今回の実環境で確認した事実ではない。Mac未検証という旧記述は、今回のオフライン確認範囲では更新された（実APIは引き続き未検証）。TEST_RESULTS.txtの33件は配布時点の記録として保持した。

## 今回の修正

1. 最終エラー表示が標準の2環境変数のみを伏せていたため、設定のapi_key_envで指定した環境変数の値も伏せるよう修正。回帰テストを追加。移行仕様・書き込み許可リストは変更していない。
2. .gitignoreをconfig*.json、*.config.json、.env*に拡張し、config.example.jsonは除外しない。任意の名前の設定やwork_dirをすべて自動保護するものではない。将来Git管理を始める前に、実際の設定名・保存先を追加確認すること。

## 実施した検証

- macOS、python3: Python 3.14.0。
- python3 backlog_migrate.py --help: 成功。
- python3 -m unittest discover -s tests -v: 修正前33件成功、修正後34件成功。
- 401課題・251コメントのページング、12状態、親子、属性、添付、少数件後の全件再開、応答消失と重複防止、未対応ID、ハッシュ不一致、キー秘匿等を模擬APIで確認。
- Python 3.14でテスト内HTTPErrorオブジェクトの暗黙クリーンアップにResourceWarningが2件出る。失敗はないが、応答のclose処理は今後の改善候補。
- 実スペースへのprepare/check/migrate/verifyは一切実行していない。模擬テスト成功は本番移行の保証ではない。

## 公開公式仕様との照合

2026-11-10に以下の公開ドキュメントを閲覧。ヘッダー認証、parentChild=0、コメントminId/count、既存状態へのPATCH、添付アップロード後のattachmentId[]、カスタム属性定義、レート制限の基本仕様と整合を確認した。全API・全形式・契約制限の網羅確認ではない。

- https://developer.nulab.com/ja/docs/backlog/auth/
- https://developer.nulab.com/ja/docs/backlog/api/2/get-issue-list/
- https://developer.nulab.com/ja/docs/backlog/api/2/get-comment-list/
- https://developer.nulab.com/ja/docs/backlog/api/2/add-issue/
- https://developer.nulab.com/ja/docs/backlog/api/2/update-issue/
- https://developer.nulab.com/ja/docs/backlog/api/2/add-comment/
- https://developer.nulab.com/ja/docs/backlog/api/2/get-custom-field-list/
- https://developer.nulab.com/ja/docs/backlog/api/2/post-attachment-file/
- https://developer.nulab.com/ja/docs/backlog/rate-limit/

## 未確認事項・次の作業

1. ローカル準備: 元・先の実URLとプロジェクトキー、テスト先か本番先か、設定インポート状況、既存課題・公式ツール途中データの有無を確認する。例示値を実値として使わない。
2. ローカル準備: API実行者の権限、担当者の参加、属性・整形ルール・親子設定、容量制限、元・先の更新停止期間、通知・外部連携の調整を行う。現在はconfig.jsonのsource.api_keyとdestination.api_keyへの直接記載に対応。直接指定を優先し、空・省略時のみ従来の環境変数から取得する。共有サンプルのキーは空のままにする。
3. 接続承認後の読み取り: prepare（元・先APIのGETとローカル保存）。対応表をレビューしてcheck（GETとローカルレポート更新）。既存workが発見されたら初期化せず先に確認する。
4. 別途承認後の書き込み: migrate --apply --limit 3。必要な親を含む実対象と操作を承認前に具体化する。verify --deep --limit 3はGETとローカル照合・一時保存。画面確認後、全件は改めて承認を得る。
5. Wiki・ドキュメント・共有ファイルは方式・最新公式仕様・制限を整理してから追加検討。公式ツールの課題除外で状態数エラーを必ず回避できるとは断定しない。
