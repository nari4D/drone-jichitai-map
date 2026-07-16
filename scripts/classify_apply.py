#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classify_apply.py — 分類層(2/2): LLMの判定を機械検証して regulations に反映

data/classify/verdicts.json（LLMが書く）を読み、1件ずつ検証してから反映する。
**LLM の自己申告は一切信用しない。** 通すのは以下を全て満たすものだけ:

  1. id が pending.json に存在する（勝手な自治体を足せない）
  2. category が prohibited / restricted / unknown のいずれか
  3. quote が保存済み本文 pages/<id>.txt に **逐語で存在する**（空白無視で突き合わせ）
  4. quote にドローン語（ドローン/無人航空機/…）が含まれる
  5. quote が十分に長い（短すぎる引用は何にでも一致してしまう）
  6. confidence が high / medium

1つでも欠けたら **禁止と断定せず unknown に倒す**（＝データを変更しない）。
これにより「根拠のない断定」は構造的に発生しない。プロンプトのお願いではなく、
ここの決定論的なチェックが精度の担保である。

反映は昇格のみ（prohibited > restricted > unknown）:
  手で逐語確認済みの既存データを、自動判定が勝手に格下げ・上書きしないため。
  規制が緩和された場合の格下げは監視層(crawl_check.py)が拾って人が判断する。

使い方: py -3.13 scripts/classify_apply.py [--dry-run]
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover import DATA, QUEUE, TODAY, STRONG_KEYWORDS  # noqa: E402
from classify_prepare import CLS, PAGES, PENDING, PREF_FILES  # noqa: E402

VERDICTS = CLS / "verdicts.json"
CLASSIFIED = DATA / "discovery" / "classified.json"

VALID_CATEGORIES = ("prohibited", "restricted", "unknown")
RANK = {"unknown": 0, "restricted": 1, "prohibited": 2}
MIN_QUOTE = 12          # これ未満の引用は根拠として弱い（"ドローン禁止" 等でも12字に届く）
OK_CONFIDENCE = ("high", "medium")


def squash(s):
    """空白・改行を落として比較する。page_text の整形差で逐語一致が壊れるのを防ぐ。"""
    return re.sub(r"[\s　]+", "", s or "")


def verify(v, pending_by_id):
    """検証を通れば (item, category, quote, summary)。落ちたら (item|None, 理由)。"""
    pid = v.get("id")
    item = pending_by_id.get(pid)
    if not item:
        return None, f"未知のid: {pid}"

    cat = v.get("category")
    if cat not in VALID_CATEGORIES:
        return item, f"不正な区分: {cat}"
    if cat == "unknown":
        return item, "LLMがunknownと判定"

    if v.get("confidence") not in OK_CONFIDENCE:
        return item, f"確信度が低い: {v.get('confidence')}"

    quote = (v.get("quote") or "").strip()
    if len(squash(quote)) < MIN_QUOTE:
        return item, f"引用が短すぎる: {quote!r}"
    if not any(k in quote for k in STRONG_KEYWORDS):
        return item, "引用にドローン語が無い"

    page = PAGES / f"{pid}.txt"
    if not page.exists():
        return item, "保存済み本文が無い"
    if squash(quote) not in squash(page.read_text(encoding="utf-8")):
        # ここが最重要。引用を捏造しても、保存済みの公式本文に無ければ落ちる。
        return item, "引用が本文に存在しない(捏造の疑い)"

    summary = (v.get("summary") or "").strip()
    if not summary:
        return item, "要約が空"
    return item, (cat, quote, summary)


def apply_one(reg, item, cat, quote, summary):
    """regulations に反映（昇格のみ・出典は追記）。変更したら True。"""
    name = item["muni"]
    munis = reg.setdefault("municipalities", {})
    cur = munis.get(name)

    src = {"label": f"{name} 公式（自動分類 {TODAY} 確認）", "url": item["url"], "quote": quote}

    if cur is None:
        munis[name] = {"category": cat, "summary": summary, "確認日": TODAY, "sources": [src]}
        # 詳細が付いた自治体は unknown_simple から外す
        us = reg.get("unknown_simple")
        if isinstance(us, list) and name in us:
            us.remove(name)
        return True

    changed = False
    if RANK[cat] > RANK.get(cur.get("category", "unknown"), 0):
        cur["category"] = cat
        changed = True
    # 要約は既存（手で逐語確認したもの）を優先し、自動では上書きしない
    if not cur.get("summary"):
        cur["summary"] = summary
        changed = True
    urls = {s.get("url") for s in cur.setdefault("sources", [])}
    if item["url"] not in urls:
        cur["sources"].append(src)
        changed = True
    if changed:
        # 確認日は「触った自治体だけ」に付ける。_meta.確認日(県の一括調査日)は書き換えない。
        # 以前は _meta 側を今日に更新しており、1件しか確認していなくても全自治体が
        # 今日確認済みに見えてしまっていた。
        cur["確認日"] = TODAY
    return changed


def dequeue(done_urls, log_rows):
    """処理済みを pending から外し、監査ログに残す。"""
    if QUEUE.exists():
        q = json.load(open(QUEUE, encoding="utf-8"))
        q["pending"] = [i for i in q.get("pending", []) if i.get("url") not in done_urls]
        json.dump(q, open(QUEUE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    log = []
    if CLASSIFIED.exists():
        try:
            log = json.load(open(CLASSIFIED, encoding="utf-8"))
        except Exception:
            log = []
    log.extend(log_rows)
    json.dump(log, open(CLASSIFIED, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="検証だけして書き込まない")
    args = ap.parse_args()

    if not VERDICTS.exists():
        print(f"判定ファイルが無い: {VERDICTS}", file=sys.stderr)
        sys.exit(3)
    if not PENDING.exists():
        print(f"判定パケットが無い: {PENDING}", file=sys.stderr)
        sys.exit(3)

    pending = json.load(open(PENDING, encoding="utf-8"))
    pending_by_id = {i["id"]: i for i in pending.get("items", [])}
    verdicts = json.load(open(VERDICTS, encoding="utf-8"))
    if isinstance(verdicts, dict):
        verdicts = verdicts.get("verdicts", [])

    regs = {p: json.load(open(DATA / f, encoding="utf-8")) for p, f in PREF_FILES.items()}
    applied, rejected, done_urls, log_rows = [], [], set(), []

    for v in verdicts:
        item, res = verify(v, pending_by_id)
        if item is None:
            print(f"  ! {res}", file=sys.stderr)
            rejected.append(("-", res))
            continue
        tag = f"[{item['pref']}] {item['muni']}"
        done_urls.add(item["url"])
        if isinstance(res, str):
            # 検証に落ちた＝unknown に倒す（データは変えない）
            print(f"  - {tag}: unknown ({res})", file=sys.stderr)
            rejected.append((tag, res))
            log_rows.append({"date": TODAY, "muni": item["muni"], "pref": item["pref"],
                             "url": item["url"], "result": "unknown", "reason": res})
            continue
        cat, quote, summary = res
        changed = (True if args.dry_run
                   else apply_one(regs[item["pref"]], item, cat, quote, summary))
        if changed:
            print(f"  + {tag}: {cat}{' ※dry-run' if args.dry_run else ''}", file=sys.stderr)
            applied.append((tag, cat))
        log_rows.append({"date": TODAY, "muni": item["muni"], "pref": item["pref"],
                         "url": item["url"], "result": cat, "quote": quote})

    if not args.dry_run:
        for pref, reg in regs.items():
            json.dump(reg, open(DATA / PREF_FILES[pref], "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        dequeue(done_urls, log_rows)

    print(f"\n判定{len(verdicts)} / 反映{len(applied)} / unknownに倒した{len(rejected)}",
          file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
