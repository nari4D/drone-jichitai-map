# -*- coding: utf-8 -*-
"""prefectures.py — 対象都道府県の定義（唯一の対応表）

県を追加するときに触るのはこのファイルだけ。以前は build_overlay / build_watchlist /
classify_prepare がそれぞれ独自の対応表を持っていて、県を足すたびに3か所直す必要があった。

ファイル名の規約: <種類>_<JISコード>_<ローマ字>.<拡張子>
  data/regulations_14_kanagawa.json   規制データ（唯一の一次ソース）
  data/boundaries_14_kanagawa.json    市区町村界（国交省 N03）
  docs/regulations_14_kanagawa.json   サイト配信用のコピー（build_overlay.py が複製）
  docs/overlay_14_kanagawa.geojson    地図描画用（build_overlay.py が生成）

JISコードを入れるのは、全国47都道府県に広げたときにファイルが自然に並ぶため。
どの県も無印にしない（無印にすると、その県が暗黙の「デフォルト」になってしまう）。
"""

# 表示順。全国展開したらコード順に並べ替えてよいが、現状はサイトのタブ順に合わせている。
PREFECTURES = [
    {"code": "14", "name": "神奈川県", "slug": "kanagawa"},
    {"code": "13", "name": "東京都", "slug": "tokyo"},
]


def _stem(pref):
    return f"{pref['code']}_{pref['slug']}"


def regulations_file(pref):
    return f"regulations_{_stem(pref)}.json"


def boundaries_file(pref):
    return f"boundaries_{_stem(pref)}.json"


def overlay_file(pref):
    return f"overlay_{_stem(pref)}.geojson"


def by_name(name):
    for p in PREFECTURES:
        if p["name"] == name:
            return p
    raise KeyError(f"未知の都道府県: {name}")


# 都道府県名 → regulations ファイル名（従来の PREF_FILES 相当）
def regulations_files():
    return {p["name"]: regulations_file(p) for p in PREFECTURES}
