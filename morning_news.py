#!/usr/bin/env python3
"""
Утренняя сводка новостей.

1. Забирает свежие новости из RSS-лент (config.json).
2. Отдаёт заголовки Claude (через твою подписку Claude Code, команда `claude -p`),
   он выбирает главное и пишет короткие пересказы на русском.
3. Вставляет результат в template.html и сохраняет как news.html.

Облачная версия: запускается на GitHub по расписанию (.github/workflows/news.yml)
и кладёт страницу в site/index.html, откуда она публикуется на GitHub Pages.
"""

import concurrent.futures
import datetime as dt
import email.utils
import glob
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET

IS_WINDOWS = os.name == "nt"
# в Windows консоль и логи по умолчанию не в UTF-8 — без этого ломается кириллица
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
TEMPLATE_PATH = os.path.join(BASE, "template.html")
OUTPUT_PATH = os.path.join(BASE, os.environ.get("NEWS_OUTPUT", "site/index.html"))
LOG_DIR = os.path.join(BASE, "logs")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FETCH_TIMEOUT = 20

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def log(msg):
    print(dt.datetime.now().strftime("%H:%M:%S"), msg, flush=True)


# ----------------------------------------------------------------- загрузка

def http_get(url, timeout=FETCH_TIMEOUT, tries=2):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def clean_text(s, limit=None):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:  # noqa: BLE001
        d = None
    if d is None:
        try:
            iso = re.sub(r"\.\d+", "", s.replace("Z", "+00:00"))
            d = dt.datetime.fromisoformat(iso)
        except Exception:  # noqa: BLE001
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_feed_xml(data):
    """Возвращает список dict(title, link, date, desc). Понимает RSS 2.0, RSS 1.0 и Atom."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return parse_feed_regex(data)
    out = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        it = {"title": "", "link": "", "date": None, "desc": ""}
        for ch in node:
            name = local(ch.tag)
            text = (ch.text or "").strip()
            if name == "title":
                it["title"] = clean_text(text)
            elif name == "link":
                href = ch.attrib.get("href")
                rel = ch.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    it["link"] = href
                elif text and not it["link"]:
                    it["link"] = text
            elif name in ("pubdate", "published", "updated", "date") and not it["date"]:
                it["date"] = parse_date(text)
            elif name in ("description", "summary") and not it["desc"]:
                it["desc"] = text
            elif name == "encoded" and not it["desc"]:
                it["desc"] = text
        if not it["link"]:
            guid = node.find("guid")
            if guid is not None and (guid.text or "").startswith("http"):
                it["link"] = guid.text.strip()
        if it["title"] and it["link"]:
            out.append(it)
    return out


def parse_feed_regex(data):
    """Запасной разбор для лент с битым XML."""
    text = data.decode("utf-8", "replace")
    m = re.search(r'encoding="([^"]+)"', text[:200])
    if m and m.group(1).lower() not in ("utf-8", "utf8"):
        try:
            text = data.decode(m.group(1), "replace")
        except LookupError:
            pass
    out = []
    for block in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", text, re.S | re.I):
        def tag(names):
            for n in names:
                mm = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (n, n), block, re.S | re.I)
                if mm:
                    return re.sub(r"^<!\[CDATA\[|\]\]>$", "", mm.group(1).strip())
            return ""
        link = tag(["link"])
        if not link:
            mm = re.search(r'<link[^>]+href="([^"]+)"', block)
            link = mm.group(1) if mm else ""
        title = clean_text(tag(["title"]))
        if title and link:
            out.append({"title": title, "link": link.strip(),
                        "date": parse_date(tag(["pubDate", "published", "updated", "dc:date"])),
                        "desc": tag(["description", "summary"])})
    return out


def fetch_feed(feed):
    data = http_get(feed["url"])
    return parse_feed_xml(data)


def collect(cfg):
    st = cfg["settings"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=st["hours_back"])
    feeds = cfg["feeds"]
    ok, failed = [], []
    candidates = []
    seen = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(fetch_feed, f): f for f in feeds}
        results = {}
        for fut in concurrent.futures.as_completed(futs):
            f = futs[fut]
            try:
                results[f["url"]] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[f["url"]] = None
                log("  нет ответа: %s (%s)" % (f["name"], type(e).__name__))

    for f in feeds:  # в порядке конфига, чтобы id были стабильными
        items = results.get(f["url"])
        if items is None:
            failed.append(f["name"])
            continue
        fresh = [i for i in items if i["date"] is None or i["date"] >= cutoff]
        fresh.sort(key=lambda i: i["date"] or cutoff, reverse=True)
        fresh = fresh[: st["max_items_per_feed"]]
        ok.append(f["name"])
        for it in fresh:
            key = (f["section"], it["title"].lower())
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "id": len(candidates) + 1,
                "section": f["section"],
                "region": f["region"],
                "source": f["name"],
                "title": it["title"],
                "desc": clean_text(it["desc"], 180),
                "link": it["link"],
                "date": it["date"],
            })
    return candidates, ok, failed


# ----------------------------------------------------------------- рынки

def fetch_cbr(date=None):
    url = "https://www.cbr.ru/scripts/XML_daily.asp"
    if date:
        url += "?date_req=" + date.strftime("%d/%m/%Y")
    root = ET.fromstring(http_get(url, timeout=12))
    rates = {}
    for v in root.findall("Valute"):
        code = v.findtext("CharCode")
        val = float(v.findtext("Value").replace(",", ".")) / float(v.findtext("Nominal"))
        rates[code] = val
    return root.attrib.get("Date"), rates


def fetch_markets():
    out = []  # (name, value_str, change_pct or None)
    try:
        date_str, today = fetch_cbr()
        d = dt.datetime.strptime(date_str, "%d.%m.%Y").date() - dt.timedelta(days=1)
        _, prev = fetch_cbr(d)
        for code, label in (("USD", "USD/₽"), ("EUR", "EUR/₽"), ("CNY", "CNY/₽")):
            if code in today:
                ch = (today[code] / prev[code] - 1) * 100 if code in prev else None
                out.append((label + " ЦБ", fmt_num(today[code], 2) + " ₽", ch))
    except Exception as e:  # noqa: BLE001
        log("  курсы ЦБ недоступны (%s)" % type(e).__name__)
    try:
        j = json.loads(http_get(
            "https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX.json"
            "?iss.meta=off&iss.only=marketdata&marketdata.columns=CURRENTVALUE,LASTCHANGEPRC",
            timeout=8, tries=1))
        val, ch = j["marketdata"]["data"][0]
        if val:
            out.append(("IMOEX", fmt_num(val, 0), ch))
    except Exception as e:  # noqa: BLE001
        log("  Мосбиржа недоступна (%s)" % type(e).__name__)
    try:
        j = json.loads(http_get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum"
            "&vs_currencies=usd&include_24hr_change=true", timeout=10))
        out.append(("BTC", "$" + fmt_num(j["bitcoin"]["usd"], 0), j["bitcoin"].get("usd_24h_change")))
        out.append(("ETH", "$" + fmt_num(j["ethereum"]["usd"], 0), j["ethereum"].get("usd_24h_change")))
    except Exception as e:  # noqa: BLE001
        log("  крипта недоступна (%s)" % type(e).__name__)
    return out


def fmt_num(x, digits):
    s = "{:,.{d}f}".format(x, d=digits)
    return s.replace(",", " ").replace(".", ",")


# ----------------------------------------------------------------- Claude

def find_claude(cfg):
    p = cfg["settings"].get("claude_path")
    if p and os.path.exists(os.path.expanduser(p)):
        return os.path.expanduser(p)
    p = shutil.which("claude")
    if p:
        return p
    home = os.path.expanduser("~")
    exe = ".exe" if IS_WINDOWS else ""
    places = [os.path.join(home, ".local", "bin", "claude" + exe),
              os.path.join(home, ".claude", "local", "claude" + exe)]
    if IS_WINDOWS:
        places.append(os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"))
    else:
        places += ["/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    for p in places:
        if os.path.exists(p):
            return p
    # бинарник внутри расширения Claude Code для Cursor / VS Code — берём самую новую версию
    found = []
    for editor in (".cursor", ".vscode"):
        found += glob.glob(os.path.join(home, editor, "extensions", "anthropic.claude-code-*",
                                        "resources", "native-binary", "claude" + exe))

    def ver(path):
        m = re.search(r"claude-code-([\d.]+)", path)
        return tuple(int(x) for x in m.group(1).strip(".").split(".")) if m else ()
    found.sort(key=ver, reverse=True)
    return found[0] if found else None


SECTION_HINTS = {
    "world": "международная политика, конфликты, экономика и события мира (без внутренних новостей РФ)",
    "russia": "внутренние новости России: политика, экономика, общество, регионы",
    "auto": "автоиндустрия: новые модели, продажи, электромобили, автопроизводители, авторынок России",
    "invest": "рынки и инвестиции: акции, индексы, ставки ЦБ и ФРС, валюты, нефть, отчёты компаний, IPO, крипта",
}


def build_prompt(cfg, candidates):
    top = cfg["settings"]["top"]
    parts = []
    for key, sec in cfg["sections"].items():
        rows = [c for c in candidates if c["section"] == key]
        parts.append("\n### Раздел %s (%s) — %d новостей" % (key, sec["title"], len(rows)))
        for c in rows:
            reg = "РФ" if c["region"] == "ru" else "мир"
            line = "[%d] (%s, %s) %s" % (c["id"], c["source"], reg, c["title"])
            if c["desc"] and c["desc"].lower() != c["title"].lower():
                line += " — " + c["desc"]
            parts.append(line)

    rules = "\n".join("- %s: ровно до %d сюжетов. Тема: %s." % (k, top[k], SECTION_HINTS.get(k, ""))
                      for k in cfg["sections"])
    today = dt.datetime.now().strftime("%d.%m.%Y")

    return """Ты — редактор утренней сводки новостей. Сегодня %s.
Ниже — свежие заголовки из RSS-лент за последние сутки, у каждого номер в квадратных скобках.

Задача: для каждого раздела выбери самые важные сюжеты дня и напиши по ним сводку на русском языке.

Правила:
%s
- Важность — по реальному значению для читателя в России, а не по количеству упоминаний одного шума.
- Один сюжет, о котором пишут несколько источников, — это ОДИН пункт; перечисли в "ids" номера всех подходящих заголовков (до 4).
- В разделах auto и invest старайся смешивать мировые и российские сюжеты, если российские есть.
- Сортируй по важности: первый пункт раздела — главный.
- "title": короткий ясный заголовок на русском (до 90 символов), без кликбейта.
- "summary": 1–3 предложения: что произошло и почему это важно. Только факты из заголовков и описаний, ничего не выдумывай; если деталей мало — пиши коротко.
- "region": "ru" если сюжет про Россию, иначе "world".
- "brief": 4–5 самых главных событий дня по всем разделам, по одному предложению каждое.
- Используй только номера из списка ниже.

Ответь ТОЛЬКО валидным JSON без пояснений и без markdown, строго в формате:
{"brief": ["..."], "sections": {"world": [{"title": "...", "summary": "...", "region": "world", "ids": [1, 2]}], "russia": [], "auto": [], "invest": []}}

Заголовки:
%s
""" % (today, rules, "\n".join(parts))


def ask_claude(cfg, prompt):
    claude = find_claude(cfg)
    if not claude:
        raise RuntimeError("не найден Claude Code (команда claude)")
    cmd = [claude, "-p",
           "--model", cfg["settings"]["model"],
           "--output-format", "json",
           "--no-session-persistence",
           "--strict-mcp-config",
           "--tools", ""]
    log("  Claude: %s" % claude)
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900, cwd=BASE)
    if p.returncode != 0:
        raise RuntimeError("claude завершился с ошибкой: %s %s" % (p.stderr.strip()[:300], p.stdout.strip()[:300]))
    outer = json.loads(p.stdout)
    if outer.get("is_error"):
        raise RuntimeError("Claude вернул ошибку: %s" % str(outer.get("result"))[:300])
    text = outer.get("result", "")
    with open(os.path.join(LOG_DIR, "last_claude_answer.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude ответил не JSON")
    return json.loads(m.group(0))


# ----------------------------------------------------------------- HTML

def esc(s):
    return html.escape(str(s or ""), quote=True)


def card_html(item, lead):
    region = item.get("region") if item.get("region") in ("ru", "world") else "world"
    badge = "Россия" if region == "ru" else "Мир"
    links = "".join('<a href="%s" target="_blank" rel="noopener">%s ↗</a>' % (esc(s["link"]), esc(s["source"]))
                    for s in item["sources"])
    summary = '<p>%s</p>' % esc(item["summary"]) if item.get("summary") else ""
    return ('<article class="card%s"><div><span class="badge %s">%s</span></div>'
            '<h3>%s</h3>%s<div class="sources">%s</div></article>'
            % (" lead" if lead else "", region, badge, esc(item["title"]), summary, links))


def resolve(answer, candidates, cfg):
    """Превращает номера из ответа Claude в реальные ссылки (ссылки не берутся из текста модели)."""
    by_id = {c["id"]: c for c in candidates}
    result = {}
    for key in cfg["sections"]:
        items = []
        for it in (answer.get("sections", {}).get(key) or []):
            srcs, used, first = [], set(), None
            for i in it.get("ids") or []:
                try:
                    c = by_id.get(int(i))
                except (TypeError, ValueError):
                    c = None
                if c and c["source"] not in used:
                    used.add(c["source"])
                    first = first or c
                    srcs.append({"source": c["source"], "link": c["link"]})
            if not srcs or not it.get("title"):
                continue
            if it.get("region") not in ("ru", "world"):
                it["region"] = first["region"]
            items.append({"title": it["title"], "summary": it.get("summary", ""),
                          "region": it["region"], "sources": srcs})
        result[key] = items[: cfg["settings"]["top"].get(key, 8)]
    return result, [b for b in (answer.get("brief") or []) if isinstance(b, str)]


def fallback(candidates, cfg):
    """Если Claude недоступен — просто свежие заголовки, без пересказа."""
    result = {}
    for key in cfg["sections"]:
        rows = [c for c in candidates if c["section"] == key][: cfg["settings"]["top"].get(key, 8)]
        result[key] = [{"title": c["title"], "summary": c["desc"], "region": c["region"],
                        "sources": [{"source": c["source"], "link": c["link"]}]} for c in rows]
    return result, []


def render(cfg, sections, brief, markets, stats, notice):
    now = dt.datetime.now()
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = f.read()

    tickers = []
    for name, val, ch in markets:
        chg = ""
        if ch is not None:
            cls = "up" if ch >= 0 else "down"
            chg = '<span class="t-chg %s">%s%s%%</span>' % (cls, "+" if ch >= 0 else "−", fmt_num(abs(ch), 2))
        tickers.append('<div class="ticker"><span class="t-name">%s</span><span class="t-val">%s</span>%s</div>'
                       % (esc(name), esc(val), chg))

    nav, secs = [], []
    for key, sec in cfg["sections"].items():
        items = sections.get(key, [])
        nav.append('<a href="#%s">%s<span class="n">%d</span></a>' % (key, esc(sec["title"]), len(items)))
        cards = "".join(card_html(it, i == 0) for i, it in enumerate(items)) or \
            '<p class="empty">Сегодня по этому разделу ничего не нашлось.</p>'
        secs.append('<section class="section" id="%s"><div class="section-head"><h2>%s</h2>'
                    '<span class="sub">%s</span></div><div class="grid">%s</div></section>'
                    % (key, esc(sec["title"]), esc(sec["subtitle"]), cards))

    brief_html = ""
    if brief:
        brief_html = '<div class="brief"><h2>Коротко о главном</h2><ol>%s</ol></div>' % \
            "".join("<li>%s</li>" % esc(b) for b in brief)

    footer = ['<p>Просмотрено новостей: %d · источников ответило: %d из %d · сводку собрал Claude (%s)</p>'
              % (stats["candidates"], len(stats["ok"]), len(stats["ok"]) + len(stats["failed"]),
                 esc(cfg["settings"]["model"]))]
    if stats["failed"]:
        footer.append("<p>Не ответили сегодня: %s</p>" % esc(", ".join(stats["failed"])))
    footer.append("<p>Шаблон: template.html · источники и настройки: config.json</p>")

    date_long = "%s, %d %s" % (WEEKDAYS[now.weekday()].capitalize(), now.day, MONTHS[now.month - 1])
    out = (tpl.replace("{{DATE_LONG}}", esc(date_long))
              .replace("{{DATE_SHORT}}", now.strftime("%d.%m.%Y"))
              .replace("{{UPDATED}}", "Обновлено %s в %s" % (now.strftime("%d.%m.%Y"), now.strftime("%H:%M")))
              .replace("{{TICKERS}}", "\n".join(tickers))
              .replace("{{NAV}}", "\n".join(nav))
              .replace("{{NOTICE}}", '<div class="notice">%s</div>' % esc(notice) if notice else "")
              .replace("{{BRIEF}}", brief_html)
              .replace("{{SECTIONS}}", "\n".join(secs))
              .replace("{{FOOTER}}", "\n".join(footer)))
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    tmp = OUTPUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(out)
    os.replace(tmp, OUTPUT_PATH)


# ----------------------------------------------------------------- main

def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    log("Собираю RSS (%d лент)…" % len(cfg["feeds"]))
    candidates, ok, failed = collect(cfg)
    log("  свежих новостей: %d, лент ответило: %d, не ответило: %d" % (len(candidates), len(ok), len(failed)))

    log("Курсы и рынки…")
    markets = fetch_markets()

    notice = ""
    if not candidates:
        notice = "Ни одна лента не ответила — загляни во вкладку Actions на GitHub и запусти сборку ещё раз (Run workflow)."
        sections, brief = {k: [] for k in cfg["sections"]}, []
    else:
        log("Отправляю Claude на отбор и пересказ…")
        try:
            answer = ask_claude(cfg, build_prompt(cfg, candidates))
            sections, brief = resolve(answer, candidates, cfg)
            log("  готово: " + ", ".join("%s=%d" % (k, len(v)) for k, v in sections.items()))
        except Exception as e:  # noqa: BLE001
            log("  Claude не сработал: %s" % e)
            notice = "Claude сегодня не ответил (%s) — ниже просто свежие заголовки без пересказа." % str(e)[:160]
            sections, brief = fallback(candidates, cfg)

    render(cfg, sections, brief, markets,
           {"candidates": len(candidates), "ok": ok, "failed": failed}, notice)
    log("Сохранено: %s" % OUTPUT_PATH)

    if cfg["settings"].get("open_after_build") and "--no-open" not in sys.argv:
        webbrowser.open(pathlib.Path(OUTPUT_PATH).as_uri())


if __name__ == "__main__":
    main()
