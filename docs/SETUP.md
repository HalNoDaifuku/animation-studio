# アニメ会社データベース監視(新しい仕組み)セットアップ

会社ごとのYAML(`db/companies/*.yaml`)と共通設定(`common.yaml`)から、
**サイト監視(urlwatch)** と **SNS監視(RSSHub)** を動かし、結果を **Discord Bot** で **職種ごとのチャンネル**へ送ります。
いまの定期監視(`.urlwatch/urls.yaml` など)とは別のファイル・別のキャッシュで動くので、**今の監視は壊れません**。

## ファイル構成

| パス | 役割 |
|---|---|
| `common.yaml` | 職種・SNSフィルター・Web誤検知対策の共通設定 |
| `db/companies/*.yaml` | アニメ会社ごとの設定(アニメ会社だけを置く) |
| `db/portals/*.yaml` | アニメ会社ではない就活・求人サイト、SNSだけの窓口などの設定(同じ形式) |
| `scripts/verify_files.py` | 展開したファイルが、渡したZIPと一致するかの照合(`docs/FILES.sha256` と比較) |
| `config/discord.yaml` | 職種 → Discordチャンネルの対応(最初は全部 null) |
| `scripts/check_companies.py` | ① 設定ファイルのエラーチェック |
| `scripts/rsshub_notify.py` | ② 会社ファイル → RSSHub URL → 新着を Discord へ |
| `scripts/urlwatch_notify.py` | ③ 会社ファイル → urlwatch 実行 → 差分を Discord へ |
| `scripts/discord_setup_channels.py` | 職種ごとのチャンネルを Bot で自動作成 |
| `tests/` | 偽のDiscord/RSSHub/Webサーバーを使った自動テスト |
| `.github/workflows/watch-test.yml` | 手動実行のテスト用ワークフロー |

## 1. Discord Bot を作る

1. https://discord.com/developers/applications → New Application → 左の **Bot** でトークンを発行(**Reset Token**)。
2. 特権インテントは**不要**(送信だけなので Message Content 等はオフのまま)。
3. **OAuth2 → URL Generator**: scope は `bot`、権限は **View Channels / Send Messages / Manage Channels**(チャンネル自動作成を使うときだけ)。
   チャンネルを自分で作る場合は Manage Channels は不要。生成したURLでサーバーに招待。
4. トークンは**絶対にファイルへ書かない**。GitHub の Settings → Secrets に `DISCORD_BOT_TOKEN` として登録(ローカルでは環境変数)。

## 2. チャンネルを用意する

- 自動: `DISCORD_BOT_TOKEN=… DISCORD_GUILD_ID=<サーバーID> python scripts/discord_setup_channels.py`(計画だけ表示) → 良ければ `--apply`。
  出力された YAML を `config/discord.yaml` に貼る。会社ファイルで使われている職種 + 未分類 + 監視エラーのチャンネルを作ります。
- 手動: 開発者モードをオン → チャンネルを右クリック →「チャンネルIDをコピー」→ `config/discord.yaml` に `"…"` で囲んで書く。

### アニメ会社以外(就活サイト・求人ポータル・SNSだけの窓口など)
`db/companies/` には**アニメ会社だけ**を置きます。アニメ会社ではないもの(就活・求人サイト、就活情報のSNSアカウントだけの窓口など)は、**`db/portals/`** に同じ形式のYAMLで置きます(例: `db/portals/tukujob.yaml`)。
- サイトだけでも、SNSだけでも構いません。サイトだけなら `sns.accounts` は全部 null、SNSだけなら `monitors` を書かない(または空)にします。
- `db/portals/` のファイルは、所在地(`location`)を省略できます。
- `type` は省略すると `portal` になります(`common.yaml` の `company_types` に足せば、別の種別も使えます)。
- 職種が決まらない通知は、`config/discord.yaml` の `channels.types.portal` のチャンネルへ送られます(未設定なら fallback)。
- フォルダと type が食い違うと(db/companies/ に portal、db/portals/ に studio)、設定チェックがエラーにします。

### 送り先のルール
1. 通知の本文(SNSの投稿、サイトの差分で**増えた行**)に職種のキーワードがあれば、**その職種のチャンネルだけ**に送る(職種の除外語を含む場合はその職種を外す)。複数の職種があれば、その職種ぶんだけ送る。
2. 職種の候補は、監視URLの `roles`(空なら `common.yaml` の全職種)。会社の `roles` は、その会社だけ除外語を調整したいときの上書き。
3. 本文から職種が分からない通知や、職種にチャンネルが無い通知は、会社の `type` のチャンネル → `fallback`。全職種には送らない。同じチャンネルには1回だけ送る。
4. サイト監視のエラー・RSSHub取得エラー・urlwatch実行失敗は `errors`(未設定なら fallback / 職種のチャンネル)。

> 撮影の募集ページのように、職種専用のページは、監視URLに `roles: [compositing]` のように書くと、本文から職種が分からなくてもその職種へ送れます。
> 就活サイトなど(portal)は、職種では分けず、`types.portal` のチャンネルへ送ります。

## 3. 使い方(ローカル)

```bash
pip install -r requirements.txt
python scripts/check_companies.py                 # エラーチェック(--strict で警告もNG、--json も可)
python -m unittest discover -s tests              # 自動テスト

# RSSHub
export RSSHUB_BASE_URL=https://あなたのRSSHub   RSSHUB_ACCESS_KEY=…(使うなら)
python scripts/rsshub_notify.py --list            # 作られるURLを確認(アクセスキーは伏せる)
python scripts/rsshub_notify.py --dry-run         # 送信内容を表示(送信も既読保存もしない)
python scripts/rsshub_notify.py                   # 本番。初回は通知せず既読にするだけ

# urlwatch
python scripts/urlwatch_notify.py --generate-only # build/urls.yaml を作るだけ
python scripts/urlwatch_notify.py --dry-run       # キャッシュのコピーで実行し、送信内容を表示
python scripts/urlwatch_notify.py --skip-new --wayback
```

## 4. GitHub Actions で試す

Secrets に `DISCORD_BOT_TOKEN`、(RSSHubを使うなら)`RSSHUB_BASE_URL` と `RSSHUB_ACCESS_KEY` を登録 →
Actions → **watch-test** → Run workflow。最初は `job=check` と `dry_run=true` から。
状態(`.urlwatch/cache-companies.db`、`.rsshub-state/state.json`)は Actions のキャッシュに保存されます。

`.gitignore` に追加を推奨: `build/` `.rsshub-state/` `.urlwatch/cache-companies.db`

## 4.5 定期実行(本番)

`.github/workflows/watch.yml` が1時間おき(毎時17分)に、RSSHub → urlwatch の順で実行します(`watch-test` と状態を共有)。
- GitHub の定期実行は数分〜十数分遅れることがあります。
- 公開リポジトリは無料です。非公開リポジトリの場合も、1時間おきなら月約720分で、無料枠(月2000分)に収まります。
- 公開リポジトリは、60日間リポジトリに動きがないと定期実行が止まります(Actionsの画面で再開できます)。

## 5. 動作確認の範囲(正直なところ)

- **確認済み**: 偽サーバーによる自動テスト(Discordの429再試行・2000文字制限・User-Agent・Bot認証ヘッダー、ルーティング、RSSHub URL生成と既読管理、本物の urlwatch 2.29 の実行→差分→送信、設定の誤りの検出)。
- **未確認**(開発環境から外部サイトへ接続できないため): 本物の Discord への送信、本物の RSSHub・X のルート(`/twitter/user/…`、`/twitter/keyword/…`、`filter`/`filterout`)の挙動、検索クエリで `(A -"B")` の括弧が効くか、500字前後のクエリ上限。
  実際のRSSHubでは、まず `--list` のURLをブラウザで開いて確かめてください。ルートが違えば `common.yaml` の `sns.platforms` だけ直せば済みます。
