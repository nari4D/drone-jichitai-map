#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classify_prepare.py — 分類層(1/2): 判定パケットを作る

data/discovery/queue.json の pending から古い順に N 件取り、公式ページ本文を取得して
ローカルに保存し、LLM が読む判定パケット data/classify/pending.json を書き出す。

なぜ本文を保存するか:
  次段の classify_apply.py が「LLMの引用が本当にそのページに存在するか」を
  保存済み本文と突き合わせて機械検証するため。LLM の自己申告は信用しない。
  本文がここに固定されるので、LLM は出典を捏造できない。

公式ドメインの門番:
  取得先は「その自治体の official_hp と同一ホスト」または lg.jp / go.jp /
  pref.*.jp に限る。ブログ・行政書士サイト・ニュースは構造的に入り得ない
  （発見層も同一ドメインしか辿らないが、ここでも二重に閉める）。

使い方:
  py -3.13 scripts/classify_prepare.py [--budget 20]
  py -3.13 scripts/classify_prepare.py --recheck-unknown --pref 東京都 [--budget 10]
      … 発見待ちではなく、既に unknown の自治体の公式トップを直接読み直す
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover import (DATA, QUEUE, TODAY, STRONG_KEYWORDS, get, key_of,  # noqa: E402
                      page_text)
from prefectures import regulations_files  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CLS = DATA / "classify"
PAGES = CLS / "pages"
PENDING = CLS / "pending.json"

# 都道府県名 → regulations ファイル名（対応表の実体は prefectures.py の1か所だけ）
PREF_FILES = regulations_files()

# LLM に渡す本文の上限。自治体ページは長くないが、稀に巨大な一覧ページがある。
MAX_CHARS = 12000
# ドローン語の周辺だけを切り出す窓幅（前後）。判定に必要なのは該当箇所とその文脈。
WINDOW = 700


def official_hosts():
    """自治体名 → 公式HPのホスト（門番用）。"""
    hosts = {}
    for pref, fname in PREF_FILES.items():
        reg = json.load(open(DATA / fname, encoding="utf-8"))
        for name, hp in reg.get("official_hp", {}).items():
            hosts[(pref, name)] = urlparse(hp).netloc
    return hosts


def is_official(url, expect_host):
    """公式一次情報か。自治体の公式ホスト、または lg.jp/go.jp/pref.*.jp のみ許可。"""
    host = urlparse(url).netloc
    if not host:
        return False
    if expect_host and host == expect_host:
        return True
    return bool(re.search(r"\.(lg|go)\.jp$", host) or re.match(r"^www\.pref\.[a-z]+\.jp$", host))


def excerpts(text):
    """ドローン語の周辺だけを抜き出す。該当箇所が無ければ空（＝判定に回さない）。"""
    spans = []
    for k in STRONG_KEYWORDS:
        for m in re.finditer(re.escape(k), text):
            spans.append((max(0, m.start() - WINDOW), min(len(text), m.end() + WINDOW)))
    if not spans:
        return ""
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return "\n…\n".join(text[s:e] for s, e in merged)[:MAX_CHARS]


def queue_items(budget):
    if not QUEUE.exists():
        return []
    q = json.load(open(QUEUE, encoding="utf-8"))
    items = sorted(q.get("pending", []), key=lambda i: i.get("found", ""))
    return items[:budget] if budget else items


def unknown_items(pref_filter, budget):
    """既に unknown の自治体の公式トップを再読込対象にする（バックログ消化用）。"""
    out = []
    for pref, fname in PREF_FILES.items():
        if pref_filter and pref != pref_filter:
            continue
        reg = json.load(open(DATA / fname, encoding="utf-8"))
        detailed = reg.get("municipalities", {})
        for name, hp in reg.get("official_hp", {}).items():
            cat = detailed.get(name, {}).get("category", "unknown")
            if cat != "unknown":
                continue
            out.append({"pref": pref, "muni": name, "url": hp,
                        "via": "recheck", "found": TODAY})
    return out[:budget] if budget else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=20, help="1回で判定に回す件数の上限")
    ap.add_argument("--recheck-unknown", action="store_true",
                    help="発見キューではなく、unknown 自治体の公式トップを読み直す")
    ap.add_argument("--pref", default="", help="--recheck-unknown 時の県しぼり")
    args = ap.parse_args()

    PAGES.mkdir(parents=True, exist_ok=True)
    hosts = official_hosts()
    items = (unknown_items(args.pref, args.budget) if args.recheck_unknown
             else queue_items(args.budget))

    packet, skipped = [], []
    for it in items:
        pref, name, url = it["pref"], it["muni"], it["url"]
        if not is_official(url, hosts.get((pref, name))):
            skipped.append((url, "公式ドメインでない"))
            continue
        html = get(url)
        if not html:
            skipped.append((url, "取得失敗"))
            continue
        text = page_text(html)
        ex = excerpts(text)
        if not ex:
            # ドローン語が本文に無い＝判定材料が無い。禁止と断定する余地は無いので回さない。
            skipped.append((url, "ドローン語なし"))
            continue
        pid = key_of(url)
        (PAGES / f"{pid}.txt").write_text(text, encoding="utf-8")
        packet.append({"id": pid, "pref": pref, "muni": name, "url": url,
                       "via": it.get("via", ""), "excerpt": ex})
        print(f"  + [{pref}] {name} {url}", file=sys.stderr)

    out = {"date": TODAY, "items": packet}
    CLS.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"判定パケット: {len(packet)} 件 → {PENDING}", file=sys.stderr)
    for url, why in skipped:
        print(f"  - skip({why}): {url}", file=sys.stderr)
    # 0件なら後続(LLM判定)を回す必要が無い
    sys.exit(0 if packet else 3)


if __name__ == "__main__":
    main()
