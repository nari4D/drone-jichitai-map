# -*- coding: utf-8 -*-
"""自治体別ドローン規制オーバーレイ生成(神奈川県・東京都／複数県対応)

入力:
  data/boundaries_14_kanagawa.json  data/boundaries_13_tokyo.json
    = 国土交通省 国土数値情報 N03(行政区域) 2025 を市区町村単位に統合(dissolve)し簡略化したもの
      プロパティ: N03_001(都県) N03_003(郡/政令市) N03_004(区市町村) N03_007(コード)
  data/regulations.json  data/regulations_tokyo.json  = 規制データ(唯一の一次データソース)
出力(GitHub Pages 配信用に docs/ へ):
  docs/kanagawa_drone_jichitai.geojson  docs/tokyo_drone_jichitai.geojson
"""
import json
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

PREFS = [
    {"name": "神奈川県", "boundaries": "boundaries_14_kanagawa.json",
     "reg": "regulations.json", "common": "全県共通", "out": "kanagawa_drone_jichitai.geojson"},
    {"name": "東京都", "boundaries": "boundaries_13_tokyo.json",
     "reg": "regulations_tokyo.json", "common": "全都共通", "out": "tokyo_drone_jichitai.geojson"},
]


def build(pref):
    boundaries = json.load(open(DATA / pref["boundaries"], encoding="utf-8"))
    reg = json.load(open(DATA / pref["reg"], encoding="utf-8"))
    cats = reg["_meta"]["categories"]
    official = reg["official_hp"]

    munis = dict(reg["municipalities"])
    for name in reg.get("unknown_simple", []):
        munis[name] = {"category": "unknown",
                       "summary": reg["unknown_simple_summary"],
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
            "確認日": reg["_meta"]["確認日"], "_pref": pref["name"],
            "_cat": r["category"],
            "_color": c["line"], "_opacity": 0.9, "_weight": 1.5,
            "_fillColor": c["fill"], "_fillOpacity": c["fillOpacity"],
        }
        for i, s in enumerate(r.get("sources", []), 1):
            props[f"出典{i}"] = s["label"]
            props[f"出典{i}_URL"] = s["url"]
        if r.get("note"):
            props["備考"] = r["note"]
        out_features.append({"type": "Feature", "properties": props, "geometry": f["geometry"]})

    out = {"type": "FeatureCollection",
           "name": f"{pref['name']} 自治体別ドローン規制(公園・海岸・施設等)",
           "features": out_features}
    json.dump(out, open(DOCS / pref["out"], "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    uniq = {ft["properties"]["自治体"]: ft["properties"]["区分"] for ft in out_features}
    print(f"[{pref['name']}] features:{len(out_features)} 自治体:{len(uniq)} → docs/{pref['out']}")
    for k, v in Counter(uniq.values()).items():
        print(f"    {v:2d}  {k}")
    if skipped:
        print(f"    skip(規制対象外):{len(skipped)} {sorted(set(skipped))[:6]}")


if __name__ == "__main__":
    for pref in PREFS:
        build(pref)
