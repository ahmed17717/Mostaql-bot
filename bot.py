import json, os, re, html, sys, time
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

BASE = "https://mostaql.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}
MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "إبريل": 4, "مايو": 5,
    "يونيو": 6, "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10,
    "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")


def norm(s):
    return s.translate(DIGITS).strip()


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def list_projects(url):
    soup = get(url)
    out = {}
    for a in soup.select('a[href*="/project/"]'):
        m = re.search(r"/project/(\d+)", a["href"])
        title = a.get_text(strip=True)
        if m and title:
            link = a["href"] if a["href"].startswith("http") else BASE + a["href"]
            out.setdefault(m.group(1), {"id": m.group(1), "title": title, "link": link.split("?")[0]})
    return list(out.values())


def value_after(strings, label, stop=None):
    """First text node after the label node."""
    for i, s in enumerate(strings):
        if s == label and i + 1 < len(strings):
            return strings[i + 1]
    return None


def parse_date(text):
    m = re.search(r"(\d{1,2})\s+(\S+)\s+(\d{4})", norm(text or ""))
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    return datetime(int(m.group(3)), month, int(m.group(1))) if month else None


def parse_details(soup):
    strings = [s for s in soup.stripped_strings if s not in ("/", "|")]
    d = {}
    # category = breadcrumb item right after "المشاريع"
    d["category"] = ""
    for i, s in enumerate(strings):
        if s == "المشاريع" and i + 1 < len(strings):
            d["category"] = strings[i + 1]
            break
    # skills = text between "المهارات" and "صاحب المشروع"
    skills = []
    if "المهارات" in strings:
        i = strings.index("المهارات") + 1
        while i < len(strings) and strings[i] != "صاحب المشروع":
            skills.append(strings[i])
            i += 1
    d["skills"] = skills
    d["budget"] = value_after(strings, "الميزانية") or "-"
    d["duration"] = value_after(strings, "مدة التنفيذ") or "-"
    rate_txt = value_after(strings, "معدل التوظيف") or ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", norm(rate_txt))
    d["rate"] = float(m.group(1)) if m else None      # None = "لم يحسب بعد"
    d["rate_txt"] = rate_txt
    d["registered"] = parse_date(value_after(strings, "تاريخ التسجيل"))
    # client name = first string after "صاحب المشروع"
    d["client"] = value_after(strings, "صاحب المشروع") or "-"
    return d


def evaluate(d, cfg, now):
    """Return (ok, reason, note)."""
    cat_ok = any(c in d["category"] for c in cfg["categories"])
    skill_hit = next((s for s in d["skills"]
                      for p in cfg["skill_patterns"] if re.search(p, s, re.I)), None)
    if not (cat_ok or skill_hit):
        return False, "category/skills", ""
    reason = d["category"] if cat_ok else f"مهارة: {skill_hit}"

    # client filter
    if d["rate"] is not None:
        if d["rate"] > 0:
            return True, reason, ""
        return False, "hiring rate 0%", ""
    # rate not calculated yet -> only if client is recent
    if d["registered"] is None:
        return True, reason, "⚠️ لم أتمكن من قراءة تاريخ تسجيل العميل"
    if d["registered"] >= now - timedelta(days=cfg["new_client_max_age_days"]):
        return True, reason, ""
    return False, "old client without rating", ""


def send(token, chat, p, d, reason, note):
    reg = d["registered"].strftime("%Y-%m-%d") if d["registered"] else "?"
    lines = [
        f"🆕 <b>{html.escape(p['title'])}</b>",
        f"📂 {html.escape(reason)}",
        f"💰 {html.escape(d['budget'])}  |  ⏱ {html.escape(d['duration'])}",
        f"🛠 {html.escape('، '.join(d['skills'])[:200])}",
        f"👤 {html.escape(d['client'])} — توظيف: {html.escape(d['rate_txt'])} — تسجيل: {reg}",
    ]
    if note:
        lines.append(note)
    lines.append(f"🔗 {p['link']}")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    r.raise_for_status()


def main():
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    cfg = json.load(open("config.json", encoding="utf-8"))
    try:
        seen, first_run = set(json.load(open("seen.json"))), False
    except FileNotFoundError:
        seen, first_run = set(), True

    projects = list_projects(cfg["list_url"])
    if not projects:
        print("Parsed 0 projects - blocked or HTML changed", file=sys.stderr)
        sys.exit(1)

    new = sorted([p for p in projects if p["id"] not in seen], key=lambda x: int(x["id"]))
    if first_run:
        seen.update(p["id"] for p in new)       # seed silently
        new = []
    now = datetime.now()

    for p in new[: cfg["max_details_per_run"]]:
        try:
            d = parse_details(get(p["link"]))
        except Exception as e:                  # retry next run
            print("detail fetch failed", p["id"], e, file=sys.stderr)
            continue
        ok, reason, note = evaluate(d, cfg, now)
        print(p["id"], "SEND" if ok else f"skip ({reason})", "|", p["title"])
        if ok:
            send(token, chat, p, d, reason, note)
        seen.add(p["id"])
        time.sleep(1.5)

    json.dump(sorted(seen, key=int)[-1000:], open("seen.json", "w"))


if __name__ == "__main__":
    main()
