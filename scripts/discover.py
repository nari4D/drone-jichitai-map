#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discover.py — 発見層: 自治体サイトから「ドローン関連ページ」を自動発見する
（watchlist に無いページを候補として報告。分類確定は人の承認を経る）

2系統でカバーする:
  [1] sitemap 監視（sitemap がある自治体）
      robots.txt の "Sitemap:" → /sitemap.xml → /sitemap_index.xml
      - 初回      : ベースライン保存のみ（本文は取得しない = サイトに優しい）
      - 2回目以降 : 「新規URL」「lastmod が前回以降」だけを取得して判定
      保存形式はサイト特性で自動選択:
        lastmod あり → 最終実行日だけ保存（軽量）
        lastmod なし → URLハッシュ集合を保存（新規URL検出用）

  [2] 浅いクロール（sitemap が無い自治体のフォールバック）
      公式トップから同一ドメイン内を深さ制限つきでBFS。
      公園・みどり・施設・海岸・ルール等のヒントに一致するリンクを優先し、
      取得件数を上限で打ち切る。robots.txt の Disallow を尊重。
      → sitemap が無いサイトでも「既存の公園ルールページ」を拾える（バックログにも有効）

出力: reports/discover_YYYY-MM-DD.md（該当箇所つき）→ 人 or LLM が公式を逐語確認して反映

モード:
  通常              : python scripts/discover.py [--pref 東京都] [--limit 5]
  sitemap有無の全数調査: python scripts/discover.py --probe

設計思想（不変）: 発見までを自動化し、分類・掲載の確定は人の承認(PR)を経る。
出典は公式一次情報（自治体公式ドメイン）のみ。
依存: requests, beautifulsoup4
"""
import argparse
import hashlib
import json
import re
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

try:
    import requests
    from bs4 import BeautifulSoup
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    print("依存パッケージが必要です: pip install requests beautifulsoup4", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DISC = DATA / "discovery"
REPORTS = ROOT / "reports"
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).strftime("%Y-%m-%d")

UA = "drone-regulation-monitor/2.0 (public-interest map project; contact via repo issues)"
TIMEOUT = 20
MAX_SITEMAP_CHILDREN = 30

# クロール時に優先して辿るリンクのヒント。
# 自治体がドローンを規制する場所は公園だけではない。実データでも
#   海岸・海水浴場(葉山/逗子)、漁港(横須賀/片瀬)、港湾・海上公園(東京/川崎)、
#   湖・園地・国立公園(箱根 芦ノ湖/大涌谷)、ダム・湖(宮ヶ瀬)、屋内スポーツ施設(足立)、
#   撮影・ロケ窓口(逗子フィルムコミッション)
# に規定が載っている。よって場所・施設・手続きの各系統を広く拾う。
# 重み付き: ドローン/撮影の直球 > 規制されうる「場所」 > 汎用語。
# 汎用語(施設/利用/スポーツ等)を同点にすると、リンク数の多いスポーツ配下等に
# クロールが吸い込まれて肝心のページに到達しない（実測で確認）。
HINT_WEIGHTS = [
    (5, [  # ドローン・撮影の直球（規定が載る窓口）
        "ドローン", "drone", "無人", "mujin", "uav", "ラジコン",
        "撮影", "satsuei", "satuei", "ロケ", "film", "フィルム",
    ]),
    (3, [  # 規制されうる「場所」— 公園に限らない
        "公園", "koen", "kouen", "park", "緑地", "みどり", "midori", "広場", "hiroba", "遊園",
        "海岸", "kaigan", "海浜", "kaihin", "海水浴", "kaisui", "ビーチ", "beach",
        "港湾", "kowan", "漁港", "gyoko", "gyokou", "みなと", "minato",
        "河川", "kasen", "ダム", "dam", "湖", "水辺", "親水",
        "観光", "kanko", "kankou", "園地", "展望", "自然", "shizen", "キャンプ", "camp",
        "城址", "城跡", "史跡",
    ]),
    (1, [  # 汎用語（単独では弱い手がかり）
        "施設", "shisetsu", "sisetsu", "スポーツ", "sports", "運動", "undo", "体育", "taiiku",
        "利用", "riyou", "riyo", "ルール", "rule", "許可", "kyoka", "申請", "shinsei", "禁止",
    ]),
]
SECTION_HINTS = [h for _, hs in HINT_WEIGHTS for h in hs]
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".xls", ".xlsx",
            ".doc", ".docx", ".csv", ".mp4", ".mp3", ".ppt", ".pptx")

# 発見の必須条件: ドローン(飛行するもの)を特定する語が1つ以上あること。
# ・「行為許可」「撮影許可」等はドローン非言及の公園ページにも頻出するため単独では不可。
# ・「ラジコン」単独も不可。地上を走るラジコン（例: 横浜市の遊歩道「ラジコンやスケート
#   ボードなどの走行」）を誤検知するため。飛行を示す複合語のみ採用する。
#   実データ上、本物のドローン規制は必ず「ドローン」等を伴う
#   （江戸川「ドローン、ラジコン等」／八王子「ラジコン飛行機やドローン等」）。
STRONG_KEYWORDS = ("ドローン", "無人航空機", "無人飛行機", "マルチコプター",
                   "小型無人機", "UAV", "ラジコン飛行機", "ラジコンヘリ", "ラジコン機")

DISC.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)


def key_of(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def last_checked_of(hp: str) -> str:
    """その自治体を最後にチェックした日。未チェックは最古扱い（＝優先的に処理）。"""
    f = DISC / f"{key_of(hp)}.json"
    if f.exists():
        try:
            return json.load(open(f, encoding="utf-8")).get("checked", "0000-00-00")
        except Exception:
            return "0000-00-00"
    return "0000-00-00"


def get(url, sleep=1.0):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if sleep:
            time.sleep(sleep)
        if r.status_code == 200:
            r.encoding = r.apparent_encoding or r.encoding
            return r.text
        return None
    except Exception as e:
        print(f"  ! error {url}: {e}", file=sys.stderr)
        return None


def origin_of(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def load_robots(origin):
    rp = RobotFileParser()
    rp.set_url(origin + "/robots.txt")
    try:
        rp.read()
        return rp
    except Exception:
        return None


def allowed(rp, url):
    if rp is None:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


# ---------------------------------------------------------------- [1] sitemap
def find_sitemaps(base_url, sleep=1.0):
    origin = origin_of(base_url)
    found = []
    robots = get(f"{origin}/robots.txt", sleep=sleep)
    if robots:
        for line in robots.splitlines():
            m = re.match(r"\s*sitemap\s*:\s*(\S+)", line, re.I)
            if m:
                found.append(m.group(1).strip())
    if found:
        return found
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        body = get(origin + path, sleep=sleep)
        if body and "<" in body and ("urlset" in body or "sitemapindex" in body):
            return [origin + path]
    return []


def parse_sitemap(xml_text):
    if not xml_text:
        return [], []
    soup = BeautifulSoup(xml_text, "html.parser")
    if soup.find("sitemapindex"):
        return [], [t.get_text(strip=True) for t in soup.select("sitemap > loc")][:MAX_SITEMAP_CHILDREN]
    entries = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        lm = u.find("lastmod")
        entries.append((loc.get_text(strip=True),
                        lm.get_text(strip=True)[:10] if lm else None))
    return entries, []


def collect_entries(sitemap_urls):
    entries, seen = [], set()
    queue = list(sitemap_urls)
    while queue and len(seen) <= MAX_SITEMAP_CHILDREN:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        ents, children = parse_sitemap(get(sm))
        entries.extend(ents)
        queue.extend(c for c in children if c not in seen)
    return entries


# ---------------------------------------------------------------- [2] 浅いクロール
def link_score(href, anchor):
    blob = (href + " " + anchor).lower()
    return sum(w for w, hs in HINT_WEIGHTS for h in hs if h.lower() in blob)


def shallow_crawl(hp, max_pages=40, max_depth=2):
    """公式トップから同一ドメインを浅くBFS。公園・施設系リンクを優先して辿る。
       戻り値: [(url, text)]（取得できたページ）"""
    origin = origin_of(hp)
    host = urlparse(hp).netloc
    rp = load_robots(origin)
    seen, out = {hp}, []
    # (depth, score, url) を優先度付きで処理
    frontier = [(0, 99, hp)]

    while frontier and len(out) < max_pages:
        frontier.sort(key=lambda x: (-x[1], x[0]))    # ベストファースト: スコア高い順→浅い順
        depth, _, url = frontier.pop(0)
        if not allowed(rp, url):
            continue
        html = get(url)
        if not html:
            continue
        text = page_text(html)
        out.append((url, text))
        if depth >= max_depth:
            continue
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            continue
        for a in soup.find_all("a", href=True):
            nxt = urljoin(url, a["href"].split("#")[0])
            if not nxt or nxt in seen:
                continue
            p = urlparse(nxt)
            if p.netloc != host or p.scheme not in ("http", "https"):
                continue
            if nxt.lower().endswith(SKIP_EXT):
                continue
            sc = link_score(nxt, a.get_text(" ", strip=True))
            if sc == 0:                                 # 公園・施設系ヒント無しは辿らない
                continue
            seen.add(nxt)
            frontier.append((depth + 1, sc, nxt))
    return out


# ---------------------------------------------------------------- 本文チェック
def page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"[ \t　]+", " ", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def drone_hits(text, keywords):
    """ドローン特定語が1つも無ければ空を返す（＝発見としない。ノイズ抑制）。"""
    hits = sorted({k for k in keywords if k in text})
    if not any(k in text for k in STRONG_KEYWORDS):
        return []
    return hits


def snippet_around(text, keywords, width=60):
    for k in keywords:
        i = text.find(k)
        if i >= 0:
            return text[max(0, i - width):i + width].replace("\n", " ").strip()
    return ""


# ---------------------------------------------------------------- targets
def load_targets():
    watch = json.load(open(DATA / "watchlist.json", encoding="utf-8"))
    kw = watch["_keywords_positive"]
    known, targets = set(), []
    for pref, blk in watch["prefectures"].items():
        known.update(blk.get("common_urls", []))
        for name, info in blk["municipalities"].items():
            known.update(info.get("watch_urls", []))
            if info.get("official_hp"):
                targets.append((pref, name, info["official_hp"]))
    return targets, kw, known


# ---------------------------------------------------------------- B: 全数調査
def probe(targets):
    """全自治体の sitemap 有無を実測してカバー率を出す。"""
    have, none = [], []
    for pref, name, hp in targets:
        sms = find_sitemaps(hp, sleep=0.2)     # ドメインが毎回違うので待ちは短く
        (have if sms else none).append((pref, name, hp, sms[0] if sms else ""))
        print(f"  {'OK ' if sms else '-- '} [{pref}] {name}", file=sys.stderr)

    path = REPORTS / f"sitemap_probe_{TODAY}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# sitemap 有無の全数調査 {TODAY}\n\n")
        total = len(targets)
        f.write(f"- 対象: {total} 自治体\n")
        f.write(f"- **sitemap あり: {len(have)}（{len(have)*100//max(total,1)}%）** "
                f"→ sitemap監視でカバー\n")
        f.write(f"- **sitemap なし: {len(none)}（{len(none)*100//max(total,1)}%）** "
                f"→ 浅いクロールでカバー\n\n")
        by = {}
        for pref, name, hp, sm in have:
            by.setdefault(pref, {"o": [], "x": []})["o"].append(name)
        for pref, name, hp, sm in none:
            by.setdefault(pref, {"o": [], "x": []})["x"].append(name)
        for pref, d in by.items():
            f.write(f"## {pref}\n- あり({len(d['o'])}): {'、'.join(d['o']) or 'なし'}\n")
            f.write(f"- なし({len(d['x'])}): {'、'.join(d['x']) or 'なし'}\n\n")
    print(f"\nレポート: {path}", file=sys.stderr)
    print(f"sitemap あり {len(have)} / なし {len(none)} / 計 {len(targets)}", file=sys.stderr)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pref", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-per-domain", type=int, default=30, help="sitemap差分の本文取得上限")
    ap.add_argument("--crawl-pages", type=int, default=60, help="sitemap無しサイトのクロール上限")
    ap.add_argument("--crawl-depth", type=int, default=3, help="クロールの深さ上限")
    ap.add_argument("--no-crawl", action="store_true", help="sitemap無しサイトのクロールを行わない")
    ap.add_argument("--probe", action="store_true", help="sitemap有無の全数調査だけ行う")
    ap.add_argument("--budget", type=int, default=0,
                    help="1回の実行で処理する自治体数の上限。"
                         "最終チェックが古い順に選ぶ＝全国をローリングで一巡する（無料で全国スケールさせる要）")
    args = ap.parse_args()

    targets, keywords, known = load_targets()
    if args.pref:
        targets = [t for t in targets if t[0] == args.pref]
    if args.limit:
        targets = targets[: args.limit]
    if args.budget:
        # 未チェック→古い順に処理。毎回この上限で打ち切ることで1回の実行時間を一定に保ち、
        # 実行を重ねるうちに全自治体を一巡する（1回で全部やらないから無料枠・時間上限に収まる）
        targets.sort(key=lambda t: last_checked_of(t[2]))
        targets = targets[: args.budget]

    if args.probe:
        probe(targets)
        return

    discoveries, baselines, no_sitemap, truncated, crawled = [], [], [], [], []

    for pref, name, hp in targets:
        print(f"[{pref}] {name} …", file=sys.stderr)
        state_file = DISC / f"{key_of(hp)}.json"
        prev = json.load(open(state_file, encoding="utf-8")) if state_file.exists() else None
        sms = find_sitemaps(hp)
        entries = collect_entries(sms) if sms else []

        # ---------- [2] sitemap 無し → 浅いクロール ----------
        if not entries:
            no_sitemap.append((pref, name, hp))
            if args.no_crawl:
                # クロールしない場合でも「確認した」ことは必ず記録する。
                # 記録しないとローリング選択でこの自治体が永久に最古のまま選ばれ続け、
                # 他の自治体が処理されない（飢餓）
                json.dump({"hp": hp, "mode": "nositemap", "checked": TODAY},
                          open(state_file, "w", encoding="utf-8"),
                          ensure_ascii=False, separators=(",", ":"))
                continue
            seen_before = set((prev or {}).get("seen", []))
            pages = shallow_crawl(hp, max_pages=args.crawl_pages, max_depth=args.crawl_depth)
            found_here = 0
            for url, text in pages:
                if url in known or key_of(url) in seen_before:
                    continue
                hits = drone_hits(text, keywords)
                if hits:
                    discoveries.append((pref, name, url, hits, snippet_around(text, hits), "crawl"))
                    found_here += 1
            crawled.append((pref, name, len(pages), found_here))
            json.dump({"hp": hp, "mode": "crawl", "checked": TODAY,
                       "seen": sorted(seen_before | {key_of(u) for u, _ in pages})},
                      open(state_file, "w", encoding="utf-8"),
                      ensure_ascii=False, separators=(",", ":"))
            continue

        # ---------- [1] sitemap 監視 ----------
        has_lastmod = any(lm for _, lm in entries)
        state = {"hp": hp, "sitemaps": sms, "checked": TODAY,
                 "mode": "lastmod" if has_lastmod else "urlset"}
        if not has_lastmod:
            state["hashes"] = sorted({key_of(loc) for loc, _ in entries})

        if prev is None or prev.get("mode") == "crawl":
            json.dump(state, open(state_file, "w", encoding="utf-8"),
                      ensure_ascii=False, separators=(",", ":"))
            baselines.append((pref, name, len(entries), state["mode"]))
            continue

        last = prev.get("checked", "1970-01-01")
        if prev.get("mode") == "lastmod":
            cands = [loc for loc, lm in entries if lm and lm > last]
        else:
            old = set(prev.get("hashes", []))
            cands = [loc for loc, _ in entries if key_of(loc) not in old]
        cands = [u for u in cands if u not in known]
        if len(cands) > args.max_per_domain:
            truncated.append((pref, name, len(cands)))
            cands = cands[: args.max_per_domain]

        for url in cands:
            html = get(url)
            if not html:
                continue
            text = page_text(html)
            hits = drone_hits(text, keywords)
            if hits:
                discoveries.append((pref, name, url, hits, snippet_around(text, hits), "sitemap"))

        json.dump(state, open(state_file, "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))

    write_report(targets, discoveries, baselines, no_sitemap, truncated, crawled)
    print(f"対象{len(targets)} / 発見{len(discoveries)} / ベースライン{len(baselines)} / "
          f"sitemap無し{len(no_sitemap)}(うちクロール{len(crawled)})", file=sys.stderr)
    sys.exit(1 if discoveries else 0)


def write_report(targets, discoveries, baselines, no_sitemap, truncated, crawled):
    path = REPORTS / f"discover_{TODAY}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 発見層レポート（新規ドローン関連ページ） {TODAY}\n\n")
        f.write(f"- 対象自治体: {len(targets)}\n")
        f.write(f"- **新規発見: {len(discoveries)}** / 初回ベースライン: {len(baselines)} / "
                f"sitemap無し: {len(no_sitemap)}（うちクロール実施 {len(crawled)}）\n\n")

        if discoveries:
            f.write("## 🆕 新規に見つかったドローン関連ページ（watchlist未登録・要確認）\n\n")
            for pref, name, url, hits, snip, src in discoveries:
                f.write(f"### [{pref}] {name} （経路: {src}）\n{url}\n\n")
                f.write(f"- 検出語: **{', '.join(hits)}**\n")
                if snip:
                    f.write(f"- 該当箇所: {snip}\n")
                f.write("- → 公式ページを逐語確認のうえ regulations に反映（分類確定は人が承認）\n\n")

        if crawled:
            f.write("## 🕸 浅いクロールを実施（sitemap無しサイト）\n\n")
            for pref, name, n, found in crawled:
                f.write(f"- [{pref}] {name}: {n} ページ巡回 / 発見 {found}\n")
            f.write("\n")

        if truncated:
            f.write("## ⚠ 候補が多く打ち切り（次回持ち越し）\n\n")
            for pref, name, n in truncated:
                f.write(f"- [{pref}] {name}: 候補 {n} 件\n")
            f.write("\n")

        if baselines:
            f.write("## 📌 初回ベースライン取得（次回から差分監視）\n\n")
            for pref, name, n, mode in baselines:
                f.write(f"- [{pref}] {name}: {n} URL（方式: {mode}）\n")
            f.write("\n")

        if no_sitemap:
            f.write("## ℹ sitemap が無い自治体（クロールでカバー）\n\n")
            for pref, name, hp in no_sitemap:
                f.write(f"- [{pref}] {name}: {hp}\n")
    print(f"レポート出力: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
