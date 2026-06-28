# つりノート

ANGLERSの公開釣果一覧を事前収集し、SQLiteへ蓄積したデータを統計表示するWebサイトです。個別投稿、投稿本文、投稿者名、写真は保存・表示しません。

## 構成

- `analysis2.py`: 全国のANGLERS釣果一覧を収集してSQLiteへ保存
- `spots.json`: 個別収集時のポイント設定
- `server.py`: 静的サイトと集計APIを配信
- `data/anglers_catches.db`: 収集データベース。初回実行時に自動作成
- `data/statistics.json`: 静的公開用の集計済みJSON
- `japan-map-data.js`: 47都道府県のSVG境界パス
- `scripts/build_japan_map.py`: 地図境界データの生成スクリプト
- `app.js`: APIデータの絞り込みと統計表示
- `index.html`, `styles.css`: サイト画面
- `fish_name_aliases.json`: 魚種名の統合辞書。本体のデータファイル
- `fish_name_aliases.py`: `fish_name_aliases.json` を読み込む補助モジュール

日本地図の原典と再生成方法は `data/MAP_SOURCE.md` に記載しています。

## 魚種名の統合辞書

`fish_name_aliases.json` に、表記ゆれや別名を代表表記へ寄せるための辞書を置いています。

- JSON形式: `{"別名": "代表表記"}`
- `load_fish_name_aliases()`: JSONファイルを読み込む関数
- `normalize_fish_name(name)`: 読み込んだ辞書を使って1件の魚種名を正規化する関数

この辞書は、現時点ではまだ自動適用していません。まずは手動で追加・調整するための管理ファイルです。

## セットアップ

```bash
python3 -m pip install -r requirements.txt
python3 analysis2.py init
```

`init` はSQLiteのテーブルとインデックスを作成します。既存データは削除しません。

## 収集対象の設定

`spots.json` にポイントを追加します。

```json
{
  "area_id": 1202,
  "prefecture": "富山県",
  "spot_name": "黒部漁港",
  "lat": 36.884,
  "lng": 137.414,
  "enabled": true
}
```

- `area_id`: ANGLERSの `/areas/{area_id}/fishings` に含まれるエリアID
- `lat`: 地図上でマーカーを配置する緯度
- `lng`: 地図上でマーカーを配置する経度
- `enabled`: `true` のポイントだけを収集対象にする設定

## 全国ポイントの自動発見

ANGLERSのエリアAPIを空ページまで取得し、ポイントID、名称、都道府県、緯度、経度をSQLiteへ保存します。

```bash
python3 analysis.py discover --delay 2 --jitter 1
```

- `--discovery-pages`: 発見処理で取得する最大ページ数。通常は終端の空ページで自動停止
- `--center-lat`: APIの距離順ソートに使う中心緯度。全国取得結果そのものは変えない
- `--center-lng`: APIの距離順ソートに使う中心経度。全国取得結果そのものは変えない

2026年6月14日時点では、20件単位で250ページを超えるため、約5,000ポイントあります。

## データ収集

```bash
python3 analysis.py collect --pages 40 --delay 2 --jitter 1
```

- `--pages`: ポイントごとに取得する最大ページ数
- `--delay`: 全HTTPリクエスト間で必ず確保する最低待機秒数。初期値は2秒
- `--jitter`: 待機時間へ0秒から指定秒数までのランダムな揺らぎを加える設定。初期値は1秒
- `--timeout`: 1回のHTTPリクエストを待つ最大秒数
- `--stop-before`: 指定日より古いデータが現れたページで終了する日付
- `--database`: SQLiteデータベースの保存先
- `--spots`: 収集対象設定JSONのパス

同じ釣果を再収集しても、収集元の結果IDを一意キーとしているため重複保存されません。

待機制御はページ間だけでなく、ポイント切り替え時とエラー後の再試行にも適用されます。HTTP 429や503などで `Retry-After` ヘッダーが返された場合は、その指定時間以上待ってから再試行します。

## 全ポイントの公開釣行記収集

先に全国ポイントを発見し、その後に全ポイントを収集します。

```bash
python3 analysis.py discover --delay 2 --jitter 1
python3 analysis.py collect-all --pages 0 --delay 2 --jitter 1
```

`collect-all` は各ページの処理後に `collection_progress` テーブルへ次のページ番号を保存します。停止や通信エラーが発生しても、同じコマンドを再実行すると未完了ポイントの続きから再開します。

`--pages 0` はページ数に上限を設けず、各ポイントの公開釣行記一覧で次ページがなくなるまで収集する設定です。

発見APIで座標が返らないポイントも収集対象としてデータベースへ登録します。座標がないポイントの釣果は保存されますが、位置を決められないため全国地図には表示されません。

ここで収集するのは `/areas/{area_id}/fishings` に公開されている釣行記の全ページです。ポイント詳細に表示される累計釣果投稿数の全履歴とは一致しません。

特定ポイントだけを確認する場合:

```bash
python3 analysis.py collect-all --area-id 1202 --pages 0 --delay 2 --jitter 1
```

完了済みポイントを1ページ目から再処理する場合:

```bash
python3 analysis.py collect-all --pages 0 --delay 2 --jitter 1 --restart-complete
```

`--restart-complete` は、公開釣行記の全ページ収集済み進捗を無視して再処理する設定です。重複レコードは保存されません。

## 全国の日次更新

初回収集後は、各ポイントの先頭数ページだけを再取得します。

```bash
python3 analysis.py update-all --pages 3 --delay 2 --jitter 1
```

`update-all` は完了済みポイントも必ず1ページ目から処理します。結果IDが保存済みのレコードは追加されず、新しい釣果だけが保存されます。

約5,000ポイントを分割実行する場合:

```bash
python3 analysis.py update-all --pages 3 --delay 2 --jitter 1 --start-area-id 1 --end-area-id 1999
python3 analysis.py update-all --pages 3 --delay 2 --jitter 1 --start-area-id 2000 --end-area-id 3999
python3 analysis.py update-all --pages 3 --delay 2 --jitter 1 --start-area-id 4000
```

- `--start-area-id`: 指定ID以上のポイントだけを処理する下限
- `--end-area-id`: 指定ID以下のポイントだけを処理する上限

## 取得できる範囲

`analysis2.py collect`は、公式サイトマップに列挙された公開釣果詳細ページを先頭から処理します。古い釣果から最新の釣果まで同じ処理で保存し、釣果ページにあるポイント情報も自動登録します。事前の`discover`や別の履歴収集コマンドは不要です。

```bash
python3 analysis2.py collect \
  --database data/anglers_catches.db \
  --delay 10 \
  --jitter 5
```

`--delay`はリクエスト間の最低待機秒数、`--jitter`は待機時間へ加えるランダム秒数です。サイトマップ番号と次のURL位置はDBへ保存されるため、停止後に同じコマンドを再実行すると続きから再開します。

詳細収集を直近3年に限定する場合:

```bash
python3 analysis2.py collect \
  --database data/anglers_catches.db \
  --recent-years 3 \
  --delay 1 \
  --jitter 1
```

`--recent-years`は今日を基準に対象開始日を計算し、推定したサイトマップ内の境界位置から本収集を始めます。その前に、直前のサイトマップを末尾から逆順に確認します。開始日前の釣果が既定で10件連続すると逆順確認を終了し、途中で期間内の釣果が現れた場合は連続件数を0へ戻して再確認します。`--recent-boundary-margin`は終了判定に必要な連続期間外件数で、既定値は`10`です。`--recent-sitemap-margin`は逆順確認する直前サイトマップの最大数で、既定値は`1`です。`0`にすると逆順確認を行いません。

直近1年の公開釣果から、投稿ID順に10%だけを周期抽出する場合:

```bash
python3 analysis2.py collect \
  --database data/anglers_catches.db \
  --recent-years 1 \
  --sample-percent 10 \
  --delay 0.5 \
  --jitter 0.5
```

`--sample-percent`は、サイトマップ詳細収集で実際に詳細ページを開く割合です。URL末尾の投稿IDを数値として扱い、指定割合が投稿期間全体へほぼ均等に分散するよう周期抽出します。例えば`10`は約10件に1件、`25`は約4件に1件を取得します。既定値`100`は従来どおり全件取得します。抽出率ごとに進捗を別管理するため、同じ期間と割合で再実行すると続きから再開します。

サイトを先に公開するため、直近約4か月の簡易情報だけを素早く集める場合:

```bash
python3 analysis2.py collect \
  --database data/anglers_catches.db \
  --pages 0 \
  --delay 1 \
  --jitter 1 \
  --detail-mode skip
```

`--detail-mode skip`は詳細ページを開きません。各ポイントの公開釣行一覧から魚種、日付、ポイント、元ページURLを保存します。サイズ、重さ、匹数、正確な時刻は保存されません。公開一覧が返す範囲の制約により、対象期間は直近約4か月です。

少数の公開釣果IDだけで動作確認する場合:

```bash
python3 analysis2.py collect \
  --database data/anglers_catches.db \
  --catch-id 1184182 \
  --catch-id 1798593 \
  --delay 10 \
  --jitter 5
```

同じ簡易収集を`recent`という別名でも実行できます。

```bash
python3 analysis2.py recent \
  --database data/anglers_catches.db \
  --pages 0 \
  --delay 1 \
  --jitter 1 \
  --detail-mode skip
```

## 蓄積状況の確認

```bash
python3 analysis2.py status
```

保存済みポイント数、釣果件数、最古日、最新日を表示します。

## サイト起動

```bash
python3 server.py
```

ブラウザで `http://127.0.0.1:8000` を開きます。`server.py` はSQLiteを魚種・ポイント・年月単位に集計し、`/api/statistics` から画面へ返します。

既定では `analysis2.py` と同じ `data/anglers_catches.db` を参照します。別のDBを表示する場合は `python3 server.py --database <DBパス>` を使用します。

単純な `python3 -m http.server` では集計APIが動作しないため使用できません。

## 静的公開

本公開向けには、SQLiteから事前に集計済みJSONを書き出し、静的ホスティングへ配置します。

```bash
python3 server.py --database data/anglers_catches.db --export-statistics data/statistics.json
```

- `--database`: 集計元となるSQLiteファイル
- `--export-statistics`: 書き出すJSONの保存先。`app.js` は `data/statistics.json` を優先して読み込みます

書き出し後は、静的ファイルだけで表示できます。ローカル確認は次で可能です。

```bash
python3 -m http.server 8000
```

この状態では `app.js` が `data/statistics.json` を読み込むため、`server.py` のAPIは不要です。

検索エンジンへ載せたくない間は、`index.html` の `meta name="robots"` と `robots.txt` が `noindex` 相当の設定になっています。本公開時は両方を公開向けに切り替えてください。
