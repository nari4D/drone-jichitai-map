# data/ ディレクトリ

| ファイル | 役割 | 生成元 |
|---|---|---|
| regulations.json | **唯一の一次データソース**。規制内容・出典・色・公式HP | 手動+調査(このファイルを編集して更新) |
| watchlist.json | 自動巡回の監視対象URL(109件)とキーワード定義 | regulations.jsonから再生成可 |
| boundaries_14_kanagawa.json | 神奈川県 市区町村界(簡略化) | 国土数値情報N03/smartnews-smri |
| kanagawa_drone_jichitai.geojson | 地図描画用(build_overlay.pyの出力) | regulations.json + boundaries |
| snapshots/ | 巡回時の本文ハッシュ保存先(自動生成) | crawl_check.py |

## 更新フロー
1. crawl_check.py が snapshots/ と比較して reports/diff_*.md を出力
2. 差分を確認して regulations.json を編集
3. build_overlay.py を実行して geojson を再生成
4. watchlist は必要に応じて再生成(下記)

## watchlist.json 再生成コマンド
regulations.json を更新したら、scripts/ 内で watchlist を作り直せる
(元の生成ロジックは会話履歴参照。sources[].url と official_hp と全県共通URLを集約するだけ)。
