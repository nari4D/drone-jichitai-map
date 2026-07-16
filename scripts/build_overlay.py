# -*- coding: utf-8 -*-
"""build_overlay.py — 自治体別ドローン規制オーバーレイ生成（全都道府県対応）

入力:
  data/boundaries_<コード>_<県>.json
    = 国土交通省 国土数値情報 N03(行政区域) 2025 を市区町村単位に統合(dissolve)し簡略化したもの
      プロパティ: N03_001(都県) N03_003(郡/政令市) N03_004(区市町村) N03_007(コード)
  data/regulations_<コード>_<県>.json = 規制データ（唯一の一次ソース）
出力（GitHub Pages 配信用に docs/ へ）:
  docs/overlay_<コード>_<県>.geojson   地図描画用
  docs/regulations_<コード>_<県>.json  サイトが読む規制データ（data/ から複製）

対象県の追加は scripts/prefectures.py だけ触ればよい。
"""
import json
import shutil
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefectures import (PREFECTURES, boundaries_file, overlay_file,  # noqa: E402
                         regulations_file)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"


def build(pref):
    boundaries = json.load(open(DATA / boundaries_file(pref), encoding="utf-8"))
    reg = json.load(open(DATA / regulations_file(pref), encoding="utf-8"))
    cats = reg["_meta"]["categories"]
    official = reg["official_hp"]

    # 個別に確認していない自治体は、県の一括調査日を確認日とする。
    batch_day = reg["_meta"]["確認日"]
    munis = dict(reg["municipalities"])
    for name in reg.get("unknown_simple", []):
        munis[name] = {"category": "unknown",
                       "summary": reg["unknown_simple_summary"],
                       "確認日": batch_day,
                       "sources": reg.get("default_sources", [])}

    out_features, skipped = [], []
    for f in boundaries["features"]:
        p = f["properties"]
        seirei = p.get("N03_003") or ""
        local = p.get("N03_004") or ""
        if seirei.endswith("市"):           # 政令市(横浜市中区 等) → 市単位で継承
            city, disp = seirei, f"{seirei}{local}"
        else:                                # 区市町村・郡の町村
            city, disp = local, local

        r = munis.get(city)
        if r is None:                        # 所属未定地など、規制対象外はスキップ
            skipped.append(disp or local)
            continue
        c = cats[r["category"]]

        props = {
            "name": disp, "自治体": city, "区分": c["label"],
            "規制概要": r["summary"], "公式HP": official.get(city, ""),
            # その自治体を最後に確認した日。県全体の一括調査日ではない
            # （1件だけ再確認しても全県が最新に見えてしまうため）。
            "確認日": r.get("確認日", batch_day), "_pref": pref["name"],
            "_cat": r["category"],
            "_color": c["line"], "_opacity": 0.9, "_weight": 1.5,
            "_fillColor": c["fill"], "_fillOpacity": c["fillOpacity"],
        }
        for i, s in enumerate(r.get("sources", []), 1):
            props[f"出典{i}"] = s["label"]
            props[f"出典{i}_URL"] = s["url"]
            # 逐語引用があれば地図にも渡す。出典URLだけでは根拠そのものを確認できない。
            if s.get("quote"):
                props[f"出典{i}_引用"] = s["quote"]
        if r.get("note"):
            props["備考"] = r["note"]
        out_features.append({"type": "Feature", "properties": props, "geometry": f["geometry"]})

    out = {"type": "FeatureCollection",
           "name": f"{pref['name']} 自治体別ドローン規制(公園・海岸・施設等)",
           "features": out_features}
    json.dump(out, open(DOCS / overlay_file(pref), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    # サイトが読む規制データを data/ から複製する。以前は手作業コピーで、
    # data/ を更新して docs/ を忘れるとサイトだけ古いまま気づけなかった。
    shutil.copyfile(DATA / regulations_file(pref), DOCS / regulations_file(pref))

    uniq = {ft["properties"]["自治体"]: ft["properties"]["区分"] for ft in out_features}
    print(f"[{pref['name']}] features:{len(out_features)} 自治体:{len(uniq)} "
          f"→ docs/{overlay_file(pref)} + docs/{regulations_file(pref)}")
    for k, v in Counter(uniq.values()).items():
        print(f"    {v:2d}  {k}")
    if skipped:
        print(f"    skip(規制対象外):{len(skipped)} {sorted(set(skipped))[:6]}")


if __name__ == "__main__":
    for pref in PREFECTURES:
        build(pref)
