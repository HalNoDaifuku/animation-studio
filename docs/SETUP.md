# アニメ会社データベース監視(新しい仕組み)セットアップ

会社ごとのYAML(`db/companies/*.yaml`)と共通設定(`common.yaml`)から、
**サイト監視(urlwatch)** と **SNS監視(RSSHub)** を動かし、結果を **Discord Bot** で **職種ごとのチャンネル**へ送ります。
いまの定期監視(`.urlwatch/urls.yaml` など)とは別のファイル・別のキャッシュで動くので、**今の監視は壊れません**。

## ファイル構成

| パス | 役割 |
|---|---|
| `config/common.yaml` | 職種・SNSフィルター・Web誤検知対策の共通設定 |
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

Secrets に `DISCORD_BOT_TOKEN`、`YOUTUBE_API_KEY`、(RSSHubを使うなら)`RSSHUB_BASE_URL` と `RSSHUB_ACCESS_KEY` を登録 →
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

## 取得エラーの再試行と、同じエラーの通知間隔

- SNS(RSSHub)とサイト(urlwatch)のどちらも、取得に失敗したものは「全部の確認が終わってから」待って、取り直します(すぐには取り直しません)。
  回数と待ち時間は `common.yaml` の `sns.fetch`(`retry_rounds` / `retry_wait_seconds`)と `web.retry`(`rounds` / `wait_seconds`)で変えられます。
- 取り直しても失敗したものだけを errors チャンネルへ通知し、エラーは省略せず、実行のたびにすべて通知します(`error_renotify_hours` が0のため。時間数を入れると、同じエラーをその時間は繰り返さなくなります)。
  記録は `.rsshub-state/state.json` と `.urlwatch/error-state.json` に入り、GitHub Actions のキャッシュで引き継ぎます。
- 接続タイムアウトが同じサイトで続く場合は、そのサイトが GitHub Actions のIP(海外・クラウド)からの接続を落としている可能性があります。
  再試行では直らないので、そのURLは `enabled: false` にして手動確認にするか、別の取得手段を検討してください。

- bot対策の確認ページ("One moment, please..." など)が返ってきたときは、変更とは扱わず「エラー」にして、全部の確認が終わってから取り直します(`common.yaml` の `web.bot_challenge`)。
  保存済みの内容は更新されないので、確認ページに変わった・元に戻った、という2回の誤検知も出ません。
- タイムアウト・再試行回数など通信の設定は、`common.yaml` の `network`(Discord・Wayback・urlwatch の実行上限)、`sns.fetch`、`web.retry` にまとめてあります。
- 前回から続けて失敗している(エラーを通知済みの)ジョブは、毎回取り直しません(待ち時間が無駄に延びるため)。直れば、次の回の最初の取得で通常どおり取れます。
- 報告に載らなかったジョブ(変更なし)は、件数だけを表示します。1件ずつ今のURLの状態を調べたいときは `python scripts/urlwatch_notify.py --explain-unreported` を使います(通信が増えるため、既定ではやりません)。

## X 以外のSNS(YouTube / Instagram / Bluesky)

- 会社ファイルの `sns.accounts` に `handle` を書いて `monitor: true` にすると、X と同じ仕組み(RSSHub)で監視されます。
  Bluesky は `bsky/posts/{handle}`(例: `someone.bsky.social`)です。
- YouTube は `@ID` を `handle` に書きます(@は不要)。`@ID` が分からないチャンネルは、チャンネルID(`UC` で始まる24文字)をそのまま `handle` に書けば、専用のルートで取得します。
- Instagram は、RSSHub 側の設定(ログイン情報など)が無いと取得できないことがあります。
  取得できない場合は errors チャンネルに出ます(エラーは毎回通知されます)。使えないときは `monitor: false` に戻してください。
- 通知の絞り込み(募集/応募/採用…)は X と同じです。

## 実行のしかた(定期実行について)

- `watch.yml` に `schedule`(GitHub の定期実行)は付けていません。cron-job.org などから、1時間おきに workflow_dispatch(GitHub API)で起動してください。
- 1回の実行の上限は55分です(`timeout-minutes: 55`)。1時間おきに起動しても、前の実行が終わるまで次は待つので、重なりません。
- 公開から30日より前の投稿は通知せず既読にします(`common.yaml` の `sns.fetch.max_age_days`、0で無効)。YouTube のように古い動画が一覧に出るフィードで、過去分が届くのを防ぎます。
- 実行が長くなりすぎないよう、SNS の再試行は「最初の取得に10分以上かかったとき」と「前回から続けて失敗しているフィード」では行いません(`common.yaml` の `sns.fetch.retry_time_budget_seconds`)。

## Actions のログ

ログには、何を確認して何を通知したかが分かる内容を出します。
- SNS: フィードごとの取得件数、通知した投稿(会社・アカウント・送り先の職種・公開日時・URL・本文の冒頭)、除外した古い投稿、取得エラー。
- サイト: 変更のあったページ(送り先の職種・URL・増えた行の冒頭)、エラー、変更のなかったページ名の一覧。
- 出さないもの: Discord の Bot トークン、RSSHub のURL・アクセスキー(フィードはキー名だけを出します)、Discord のチャンネルID(職種名、または末尾4桁だけ)。
- 外部ページの本文に含まれる `::` は、GitHub Actions のコマンドと解釈されないよう `: :` に変えて出します。

## YouTube は YouTube Data API で直接読む

RSSHub 経由だと YouTube の取得が遅くタイムアウトし、公式フィード(videos.xml)は GitHub Actions からだと 404 になったため、`common.yaml` の `sns.platforms.youtube.direct: youtube_api` で YouTube Data API(公式)を直接使います。
- GitHub の Secrets に **`YOUTUBE_API_KEY`**(Vercel に入れたものと同じキーでよい)を登録してください。未登録だと、YouTube は「YOUTUBE_API_KEY が未設定です」というエラーになります。
- 採用ワード・除外語での絞り込みは、これまでと同じ語をこちらで使います。
- `@ID` のチャンネルは、`channels.list(forHandle)` で一度だけチャンネルIDにして状態ファイルに控えます。1フィードあたり1〜2ユニットで、1日の無料枠(10,000)に対して十分小さい量です。
- エラーは HTTP 番号で出ます(403 はキーの制限・割当)。動画が1本もないチャンネルは 404 になるため、エラーにせず「新着なし」として扱います。キーはログに出しません。
- 待ち時間は `sns.fetch.youtube.timeout_seconds`。`direct` の行を消すと RSSHub 経由に戻ります。

## Instagram

RSSHub 経由の Instagram は bot 判定で 503 になりやすく、取得用のアカウントも制限される危険があるため、`common.yaml` の `sns.platforms.instagram.enabled: false` で全社まとめて止めています。再開するときは `true` にしてください(会社ごとの `monitor` 設定はそのまま残してあります)。
