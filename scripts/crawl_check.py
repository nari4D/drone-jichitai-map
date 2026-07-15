#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawl_check.py v2 — 自治体ドローン規制ページの巡回・差分検知(神奈川県 + 東京都)

役割:
  1. data/watchlist.json の全URLを取得
  2. 「ドローン関連キーワードの前後の文」だけを抽出して正規化・比較
     （ページ全体ハッシュではなく、規制に関係する文だけを見るので誤検知が激減）
  3. 変化したページ・新規出現したドローン規制文・リンク切れを検出
  4. reports/diff_YYYY-MM-DD.md に「実際に変わった文」を出力
     （人 or LLM が最終判定するための材料。分類の確定はここでは行わない）

v2 の改良:
  - 条件付きGET（ETag / Last-Modified）で未変更ページはネットワーク側でスキップ（軽く・行儀よく）
  - PDF 本文抽出に対応（pdfminer.six。未導入環境では自動スキップ）
  - 差分の単位を「ページ全体」から「キーワード周辺の文」に変更 → レポートに変化文を提示
  - 複数県対応の watchlist 構造（prefectures[県].municipalities）。旧構造も読める

設計思想（重要・不変）:
  - このスクリプトは「変化の検出」までを全自動で行う。
  - regulations.json の category(色)の確定はしない。誤分類を避けるため、
    差分レポートを生成し、人 or LLM 判定→承認(PR)ステップに渡す。

依存: requests, beautifulsoup4 （必須） / pdfminer.six （PDF対応。任意）
  pip install requests beautifulsoup4 pdfminer.six
GitHub Actions で日次/週次実行を想定（.github/workflows/ 参照）。
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("依存パッケージが必要です: pip install requests beautifulsoup4", file=sys.stderr)
    raise

# PDF は任意依存。無ければ PDF はスキップして続行する。
try:
    from pdfminer.high_level import extract_text as _pdf_extract_text
    from io import BytesIO
    HAVE_PDF = True
except Exception:
    HAVE_PDF = False

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAP = DATA / "snapshots"
REPORTS = ROOT / "reports"
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).strftime("%Y-%m-%d")

UA = "drone-regulation-monitor/2.0 (public-interest map project; contact via repo issues)"
TIMEOUT = 25
WINDOW = 1  # キーワードを含む文の前後何文を残すか

SNAP.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- watchlist 読み込み
def load_watchlist():
    """新旧どちらの watchlist 構造も (pref, owner, url) の一覧に正規化して返す。"""
    watch = json.load(open(DATA / "watchlist.json", encoding="utf-8"))
    pos_kw = watch["_keywords_positive"]
    targets = []  # (pref, owner, url)
    seen = set()

    def add(pref, owner, url):
        key = (url,)
        if url and key not in seen:
            seen.add(key)
            targets.append((pref, owner, url))

    if "prefectures" in watch:  # v2 構造
        for pref, block in watch["prefectures"].items():
            for u in block.get("common_urls", []):
                add(pref, f"（{pref}共通）", u)
            for name, info in block.get("municipalities", {}).items():
                for u in info.get("watch_urls", []) or [info.get("official_hp")]:
                    add(pref, name, u)
    else:  # v1 後方互換
        pref = watch.get("_prefecture", "神奈川県")
        for u in watch.get("common_prefecture_urls", []):
            add(pref, f"（{pref}共通）", u)
        for name, info in watch.get("municipalities", {}).items():
            for u in info.get("watch_urls", []):
                add(pref, name, u)

    return targets, pos_kw


def url_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 取得
def fetch(url, prev):
    """条件付きGETで取得。戻り値 dict:
       {status:'ok'|'304'|'dead', text, etag, last_modified, is_pdf}"""
    headers = {"User-Agent": UA}
    if prev:
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        print(f"  ! fetch error {url}: {e}", file=sys.stderr)
        return {"status": "dead", "text": None, "etag": None, "last_modified": None, "is_pdf": False}

    if r.status_code == 304:
        return {"status": "304", "text": None,
                "etag": prev.get("etag"), "last_modified": prev.get("last_modified"), "is_pdf": False}
    if r.status_code != 200:
        return {"status": "dead", "text": None, "etag": None, "last_modified": None, "is_pdf": False}

    ctype = (r.headers.get("Content-Type") or "").lower()
    is_pdf = "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")
    if is_pdf:
        text = extract_pdf(r.content)
    else:
        r.encoding = r.apparent_encoding or r.encoding
        text = r.text
    return {"status": "ok", "text": text,
            "etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified"),
            "is_pdf": is_pdf}


def extract_pdf(content: bytes) -> str:
    if not HAVE_PDF:
        return ""  # pdfminer 未導入。呼び出し側で「PDF未対応」を記録
    try:
        return _pdf_extract_text(BytesIO(content)) or ""
    except Exception as e:
        print(f"  ! pdf parse error: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------- 本文正規化・文抽出
def normalize_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return clean(text)


def clean(text: str) -> str:
    # 更新日・掲載日など、誤検知の元になるノイズを除去
    text = re.sub(r"(更新日|掲載日|印刷|公開日)[:：].*", "", text)
    text = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", "", text)
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def split_sentences(text: str):
    # 改行と日本語の句点・！？で文に分割
    parts = re.split(r"(?<=[。！？!?])|\n", text)
    return [p.strip() for p in parts if p and p.strip()]


def keyword_windows(text: str, keywords, window=WINDOW):
    """キーワードを含む文と、その前後 window 文を抽出（規制に関係する文脈だけ残す）。"""
    sents = split_sentences(text)
    keep = set()
    for i, s in enumerate(sents):
        if any(k in s for k in keywords):
            for j in range(max(0, i - window), min(len(sents), i + window + 1)):
                keep.add(j)
    return [sents[i] for i in sorted(keep)]


def find_keywords(text, keywords):
    return sorted({k for k in keywords if k in text})


# ---------------------------------------------------------------- メイン
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ巡回（動作確認用）")
    ap.add_argument("--pref", default="", help="この県名だけ巡回（例: 東京都）")
    args = ap.parse_args()

    targets, pos_kw = load_watchlist()
    if args.pref:
        targets = [t for t in targets if t[0] == args.pref]
    if args.limit:
        targets = targets[: args.limit]

    changes, new_mentions, dead_links, pdf_skipped = [], [], [], []
    n_fetched = n_304 = 0

    for pref, owner, url in targets:
        snap_file = SNAP / f"{url_key(url)}.json"
        prev = json.load(open(snap_file, encoding="utf-8")) if snap_file.exists() else None

        res = fetch(url, prev)
        if res["status"] == "dead":
            dead_links.append((pref, owner, url))
            continue
        if res["status"] == "304":
            n_304 += 1
            continue  # 未変更。スナップショットはそのまま
        n_fetched += 1

        if res["is_pdf"] and not HAVE_PDF:
            pdf_skipped.append((pref, owner, url))

        text = res["text"] if res["is_pdf"] else normalize_html(res["text"])
        text = clean(text or "")
        snippets = keyword_windows(text, pos_kw)
        kws = find_keywords(text, pos_kw)
        digest = hashlib.sha256("\n".join(snippets).encode("utf-8")).hexdigest()

        if prev is None:
            new_mentions.append((pref, owner, url, kws))
        elif prev.get("digest") != digest:
            old = set(prev.get("snippets", []))
            new = set(snippets)
            added = [s for s in snippets if s not in old]
            removed = [s for s in prev.get("snippets", []) if s not in new]
            added_kw = sorted(set(kws) - set(prev.get("keywords", [])))
            changes.append((pref, owner, url, added, removed, added_kw))

        json.dump(
            {"url": url, "owner": owner, "pref": pref,
             "etag": res["etag"], "last_modified": res["last_modified"],
             "digest": digest, "keywords": kws, "snippets": snippets, "checked": TODAY},
            open(snap_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1,
        )

    write_report(targets, changes, new_mentions, dead_links, pdf_skipped, n_fetched, n_304)
    print(f"対象{len(targets)} / 取得{n_fetched} / 未変更304:{n_304} / "
          f"変化{len(changes)} / 新規{len(new_mentions)} / 切れ{len(dead_links)}")
    sys.exit(1 if (changes or dead_links) else 0)


def write_report(targets, changes, new_mentions, dead_links, pdf_skipped, n_fetched, n_304):
    report = REPORTS / f"diff_{TODAY}.md"
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"# ドローン規制ページ 巡回差分レポート {TODAY}\n\n")
        f.write(f"- 監視URL数: {len(targets)}（取得 {n_fetched} / 未変更304 {n_304}）\n")
        f.write(f"- 内容変化: {len(changes)} / 新規登録: {len(new_mentions)} / "
                f"到達不可: {len(dead_links)}\n")
        if pdf_skipped and not HAVE_PDF:
            f.write(f"- ⚠ PDF未対応環境のため {len(pdf_skipped)} 件のPDFは本文未取得"
                    f"（`pip install pdfminer.six` で解消）\n")
        f.write("\n")

        if dead_links:
            f.write("## ⚠ リンク切れ / 到達不可（要URL修正）\n\n")
            for pref, owner, url in dead_links:
                f.write(f"- **[{pref}] {owner}**: {url}\n")
            f.write("\n")

        if changes:
            f.write("## 🔄 内容が変化したページ（要確認）\n\n")
            for pref, owner, url, added, removed, added_kw in changes:
                f.write(f"### [{pref}] {owner}\n{url}\n\n")
                if added_kw:
                    f.write(f"- 新たに出現したキーワード: **{', '.join(added_kw)}**\n")
                if added:
                    f.write("- 追加された文:\n")
                    for s in added[:8]:
                        f.write(f"  - {s}\n")
                if removed:
                    f.write("- 削除された文:\n")
                    for s in removed[:8]:
                        f.write(f"  - {s}\n")
                f.write("\n")

        if new_mentions:
            f.write("## 🆕 初回スナップショット取得\n\n")
            for pref, owner, url, kws in new_mentions:
                f.write(f"- **[{pref}] {owner}**: {url} — 関連語: {', '.join(kws) or 'なし'}\n")
    print(f"レポート出力: {report}")


# TODO: LLM判定ステップ（別スクリプト or ここから呼ぶ）
#   judge_with_llm(owner, url, added_snippets) → {category, evidence(逐語), confidence}
#   出力は「提案」。regulations.json への反映は人間承認(PR)を必ず挟む。

if __name__ == "__main__":
    main()
