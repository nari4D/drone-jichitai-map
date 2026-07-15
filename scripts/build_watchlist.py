#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_watchlist.py — data/watchlist.json を再生成する（全都道府県対応 v2 構造）

各県の data/regulations_<コード>_<県>.json から自動生成する
（唯一のデータソースは regulations 側）。生成ルール:
  common_urls    = _meta.全域共通[].url
  municipalities = official_hp のキー全て
      watch_urls = [official_hp] + その自治体の sources[].url（詳細調査済みのみ）
      category_current = municipalities[name].category（無ければ unknown）

対象県の追加は scripts/prefectures.py だけ触ればよい。
使い方: python scripts/build_watchlist.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefectures import PREFECTURES, regulations_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

KEYWORDS_POSITIVE = [
    "ドローン", "無人航空機", "無人飛行機", "マルチコプター", "ラジコン",
    "UAV", "小型無人機", "飛行禁止", "撮影許可", "行為許可",
]
KEYWORDS_CATEGORY_HINT = {
    "prohibited": ["禁止", "認められません", "許可できません", "おことわり", "不可"],
    "restricted": ["許可が必要", "申請", "届出", "事前連絡", "承認", "相談"],
}

def dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_pref(pref):
    """regulations ファイルから監視ブロックを生成（全県で共通ロジック）。"""
    reg = json.load(open(DATA / regulations_file(pref), encoding="utf-8"))
    common = [s["url"] for s in reg["_meta"].get("全域共通", []) if s.get("url")]
    detailed = reg.get("municipalities", {})
    munis = {}
    for name, hp in reg["official_hp"].items():
        if name in detailed:
            info = detailed[name]
            watch = dedup([hp] + [s.get("url") for s in info.get("sources", [])])
            cat = info.get("category", "unknown")
        else:  # unknown_simple 等
            watch = dedup([hp])
            cat = "unknown"
        munis[name] = {"category_current": cat, "official_hp": hp, "watch_urls": watch}
    return {"common_urls": common, "municipalities": munis}


def main():
    out = {
        "_description": "自動巡回の監視対象（全都道府県対応 v2）。各URLを定期取得し、"
                        "ドローン関連キーワード周辺の文の差分・新規出現・リンク切れを検出する。",
        "_generator": "scripts/build_watchlist.py",
        "_keywords_positive": KEYWORDS_POSITIVE,
        "_keywords_category_hint": KEYWORDS_CATEGORY_HINT,
        "prefectures": {p["name"]: build_pref(p) for p in PREFECTURES},
    }
    path = DATA / "watchlist.json"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    n_urls = 0
    for pref, blk in out["prefectures"].items():
        c = len(blk["common_urls"])
        m = len(blk["municipalities"])
        u = sum(len(x["watch_urls"]) for x in blk["municipalities"].values())
        n_urls += c + u
        print(f"{pref}: 共通{c} / 自治体{m} / 監視URL{c + u}")
    print(f"合計監視URL(重複除く前): {n_urls}  → {path}")


if __name__ == "__main__":
    main()
