# data/ ディレクトリ

ファイル名は `<種類>_<JISコード>_<ローマ字>.json` で統一（神奈川県=14、東京都=13）。
どの都道府県も無印にしない — 無印にするとその県が暗黙のデフォルトになるため。
対象県の追加は `scripts/prefectures.py` だけ触ればよい。

| ファイル | 役割 | 生成元 |
|---|---|---|
| `regulations_<コード>_<県>.json` | **唯一の一次データソース**。規制内容・逐語引用つき出典・区分色・公式HP | 公式ページの逐語確認（分類層が自動更新） |
| `boundaries_<コード>_<県>.json` | 市区町村界（市区町村単位に統合・5%簡略化） | 国土交通省 国土数値情報 N03（2025）※民間の再配布版は使わない |
| `watchlist.json` | 自動巡回の監視対象URLとキーワード定義 | `build_watchlist.py` が regulations から再生成 |
| `discovery/` | 発見層の状態（sitemap差分のベースライン・巡回済みURL）と分類キュー `queue.json` | `discover.py`（Actionsが自動コミット） |
| `discovery/classified.json` | 分類の監査ログ（区分＋逐語引用＋却下理由） | `classify_apply.py` |
| `snapshots/` | 巡回時の本文ハッシュ保存先 | `crawl_check.py` |
| `classify/` | 分類層の作業ファイル（判定パケット・取得本文） | `classify_prepare.py` ※`.gitignore` 済み |

## 更新フロー（全自動）

```
discover.py      新しいドローン関連ページを発見 → discovery/queue.json に投入
classify_prepare 公式ページ本文を取得・保存 → 判定パケット
Claude (Actions) 保存済み本文だけを読み、逐語引用つきで区分を判定
classify_apply   引用が本文に存在するか機械検証 → 通ったものだけ regulations_*.json へ反映
build_watchlist  watchlist.json を再生成
build_overlay    docs/overlay_*.geojson を生成 + regulations_*.json を docs/ へ複製
```

人手での承認は挟まない。精度は逐語引用の機械照合で守る（`scripts/classify_apply.py`）。
`docs/` 配下の規制データは `build_overlay.py` が複製するので、手でコピーしないこと。
