"""
Парсер QA-вакансий: Хабр Карьера + hh.ru + Telegram-каналы
-> оценка под резюме через DeepSeek -> отправка в Telegram.

Запускается по расписанию в GitHub Actions (см. .github/workflows/parse.yml).
Локально: python main.py   (без токенов просто печатает результат в консоль)
"""

import hashlib
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
SEEN_FILE = BASE_DIR / "seen.json"
SEEN_LIMIT = 10000  # сколько последних ID и отпечатков помнить

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("job-parser")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


# ---------- утилиты ----------

def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> list:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("seen.json повреждён — начинаю с пустого")
    return []


def save_seen(seen: list) -> None:
    SEEN_FILE.write_text(
        json.dumps(seen[-SEEN_LIMIT:], ensure_ascii=False, indent=0), encoding="utf-8"
    )


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fingerprint(v: dict) -> str:
    """Отпечаток содержимого: ловит одну и ту же вакансию в разных источниках/репостах."""
    if v["uid"].startswith("tg:"):
        base = v["text"][:300]
    else:
        base = f"{v['title']}|{v['company']}"
    norm = re.sub(r"[^a-zа-я0-9]+", "", base.lower().replace("ё", "е"))
    return "fp:" + hashlib.md5(norm.encode()).hexdigest()[:16]


def make_vacancy(uid, source, title, url, company="", salary_text="",
                 salary_max=None, published=None, text="") -> dict:
    return {
        "uid": uid, "source": source, "title": (title or "").strip(), "url": url,
        "company": company or "", "salary_text": salary_text or "",
        "salary_max": salary_max, "published": published, "text": text or "",
    }


# ---------- источник: Хабр Карьера ----------

def fetch_habr(cfg: dict) -> list:
    out = []
    for query in cfg["queries"]:
        for page in range(1, cfg.get("pages", 1) + 1):
            r = requests.get(
                "https://career.habr.com/api/frontend/vacancies",
                params={"q": query, "type": "all", "sort": "date", "page": page,
                        **({"remote": "true"} if cfg.get("remote_only") else {})},
                headers=HEADERS, timeout=30,
            )
            r.raise_for_status()
            for v in r.json().get("list", []):
                sal = v.get("salary") or {}
                pred = v.get("predictedSalary") or {}
                salary_text = sal.get("formatted") or (
                    f"прогноз Хабра: {pred['formatted']}" if pred.get("formatted") else "")
                rub = sal.get("currency") in (None, "rur")
                salary_max = (sal.get("to") or sal.get("from")) if rub else None
                skills = ", ".join(s["title"] for s in v.get("skills", []))
                grade = (v.get("salaryQualification") or {}).get("title", "")
                out.append(make_vacancy(
                    uid=f"habr:{v['id']}", source="Хабр Карьера",
                    title=v.get("title"),
                    url="https://career.habr.com" + v.get("href", ""),
                    company=(v.get("company") or {}).get("title", ""),
                    salary_text=salary_text, salary_max=salary_max,
                    published=parse_dt((v.get("publishedDate") or {}).get("date")),
                    text=f"Грейд: {grade}\nНавыки: {skills}\n"
                         f"Можно удалённо: {'да' if v.get('remoteWork') else 'нет'}\n"
                         f"Города: {', '.join(l['title'] for l in v.get('locations') or []) or '—'}",
                ))
            time.sleep(1)
    return out


def habr_description(url: str) -> str:
    """Полное описание вакансии с Хабра (для более точной оценки)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        block = soup.select_one(".vacancy-description__text") or soup.select_one(".basic-section")
        return block.get_text("\n", strip=True)[:4000] if block else ""
    except requests.RequestException as e:
        log.warning("Хабр: не удалось загрузить описание %s: %s", url, e)
        return ""


# ---------- источники-сайты со списком вакансий в HTML (hirehi, Finder) ----------

def fetch_html_listings(cfg: dict) -> list:
    """Универсальный сборщик: открываем страницы списка, берём ссылки на вакансии по шаблону.
    Полный текст вакансии подгружается позже — только для тех, что прошли предфильтр."""
    out = []
    for site in cfg["sites"]:
        pattern = re.compile(site["link_regex"])
        for page in range(1, site.get("pages", 1) + 1):
            url = site["url"] + (f"?page={page}" if page > 1 else "")
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
            except requests.RequestException as e:
                log.warning("%s: %s", site["name"], e)
                break
            soup = BeautifulSoup(r.text, "html.parser")

            def vacancy_ids(node) -> set:
                ids = set()
                for x in node.find_all("a", href=True):
                    mm = pattern.match(requests.compat.urljoin(site["url"], x["href"]).split("#")[0])
                    if mm:
                        ids.add(mm.group(1))
                return ids

            found_on_page = 0
            for a in soup.find_all("a", href=True):
                href = requests.compat.urljoin(site["url"], a["href"]).split("#")[0]
                m = pattern.match(href)
                title = a.get_text(" ", strip=True)
                if not m or len(title) < 4:
                    continue
                # текст карточки: поднимаемся к родителю, пока в нём одна ссылка на вакансию
                card, node = a, a
                for _ in range(5):
                    node = node.parent
                    if node is None or len(vacancy_ids(node)) > 1:  # в узле уже несколько вакансий
                        break
                    card = node
                out.append(make_vacancy(
                    uid=f"{site['name'].lower()}:{m.group(1)}", source=site["name"],
                    title=title[:150], url=href,
                    text=card.get_text(" · ", strip=True)[:600],
                ))
                out[-1]["needs_detail"] = True
                found_on_page += 1
            if not found_on_page:
                break
            time.sleep(1)
    # одна вакансия может встретиться в карточке дважды (ссылка на заголовке и на «подробнее») —
    # оставляем вариант с самым длинным текстом ссылки, это и есть заголовок
    best = {}
    for v in out:
        if v["uid"] not in best or len(v["title"]) > len(best[v["uid"]]["title"]):
            best[v["uid"]] = v
    return list(best.values())


def page_text(url: str) -> str:
    """Текст страницы вакансии для оценки (main/article, иначе body)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        block = soup.find("main") or soup.find("article") or soup.body
        text = re.sub(r"\n{2,}", "\n", block.get_text("\n", strip=True)) if block else ""
        return text[:4000]
    except requests.RequestException as e:
        log.warning("Не удалось загрузить %s: %s", url, e)
        return ""


# ---------- источник: hh.ru (через сайт, API для соискателей закрыт) ----------

def _find_vacancy_list(node):
    """Рекурсивно ищем в JSON страницы список объектов с ключом vacancyId."""
    if isinstance(node, list) and node and isinstance(node[0], dict) and "vacancyId" in node[0]:
        return node
    if isinstance(node, dict):
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = _find_vacancy_list(item)
            if found:
                return found
    return None


def fetch_hh(cfg: dict) -> list:
    r = requests.get(
        "https://hh.ru/search/vacancy",
        params={"text": cfg["query"], "search_field": "name",
                "work_format": cfg.get("work_formats", ["REMOTE"]),  # список → повторяющийся параметр
                "order_by": "publication_time", "search_period": 3,
                "items_on_page": 50},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    m = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', r.text, re.S)
    if not m:
        raise RuntimeError("hh: не найден блок HH-Lux-InitialState (сайт поменял вёрстку или капча)")
    items = _find_vacancy_list(json.loads(html.unescape(m.group(1)))) or []

    out = []
    for v in items:
        comp = v.get("compensation") or {}
        cur = comp.get("currencyCode") or comp.get("currency")
        lo, hi = comp.get("from"), comp.get("to")
        salary_text = ""
        if lo or hi:
            salary_text = " – ".join(f"{x:,}".replace(",", " ") for x in (lo, hi) if x) + f" {cur or ''}"
        snippet = v.get("snippet") or {}
        pub = v.get("publicationTime")
        pub = pub.get("$") if isinstance(pub, dict) else pub
        company = v.get("company") or {}
        out.append(make_vacancy(
            uid=f"hh:{v['vacancyId']}", source="hh.ru", title=v.get("name"),
            url=f"https://hh.ru/vacancy/{v['vacancyId']}",
            company=company.get("visibleName") or company.get("name", ""),
            salary_text=salary_text,
            salary_max=(hi or lo) if cur in (None, "RUR", "RUB") else None,
            published=parse_dt(pub),
            text=f"Требования: {strip_tags(snippet.get('req'))}\n"
                 f"Обязанности: {strip_tags(snippet.get('resp'))}",
        ))
    return out


# ---------- источник: Telegram-каналы (публичное веб-превью t.me/s/) ----------

def fetch_telegram(cfg: dict) -> list:
    out = []
    for ch in cfg["channels"]:
        try:
            r = requests.get(f"https://t.me/s/{ch}", headers=HEADERS, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("TG @%s: %s", ch, e)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for msg in soup.select("div.tgme_widget_message[data-post]"):
            text_el = msg.select_one(".tgme_widget_message_text")
            if not text_el:
                continue
            text = text_el.get_text("\n", strip=True)
            time_el = msg.select_one("time[datetime]")
            post = msg["data-post"]  # вида "jobforqa/1494"
            out.append(make_vacancy(
                uid=f"tg:{post}", source=f"TG @{ch}",
                title=text.split("\n", 1)[0][:120], url=f"https://t.me/{post}",
                published=parse_dt(time_el["datetime"]) if time_el else None,
                text=text[:3000],
            ))
        time.sleep(1)
    return out


# ---------- предфильтр (бесплатно, до DeepSeek) ----------

def prefilter(v: dict, cfg: dict) -> bool:
    is_tg = v["uid"].startswith("tg:")
    haystack = (v["text"] if is_tg else v["title"]).lower()
    if not any(k in haystack for k in cfg["include_keywords"]):
        return False
    if any(k in v["title"].lower() for k in cfg["exclude_keywords"]):
        return False
    if v["salary_max"] and v["salary_max"] < cfg["min_salary_rub"]:
        return False
    if v["published"]:
        age = datetime.now(timezone.utc) - v["published"]
        if age > timedelta(hours=cfg["max_age_hours"]):
            return False
    return True


# ---------- оценка через DeepSeek ----------

SYSTEM_PROMPT = """Ты — помощник по поиску работы. Тебе дают профиль кандидата и одну вакансию
(или пост из Telegram-канала). Оцени, насколько вакансия подходит кандидату.

Правила:
- Если это не вакансия (реклама, подборка, опрос, совет) — is_vacancy=false, score=0.
- Определи формат работы (format): "удалёнка", "гибрид", "офис" или "неясно".
  Удалёнка — оценивай как обычно. Гибрид — подходит (кандидат попробует договориться
  об удалёнке), но снизь оценку на 1. Только офис без вариантов — score не выше 3.
- Подходят грейды Junior, Junior+, Middle, Middle+, если зарплата от 120 000 ₽/мес
  или не указана. Зарплата явно ниже 120 000 ₽ (или ниже ~1300 $) — score не выше 3.
- Кандидату НЕ нужны вакансии автоматизации (AQA, SDET, написание автотестов на Java/Python/JS
  как основная задача) — score не выше 2. Fullstack QA, где автотесты обязательны, — не выше 4.
  Если автоматизация только «будет плюсом» — оценивай как обычно.
- Оценивай реальное совпадение стека и задач с опытом кандидата, не придумывай факты.

Ответь СТРОГО JSON без пояснений:
{"is_vacancy": bool, "format": "удалёнка"|"гибрид"|"офис"|"неясно",
 "city": "город для гибрида/офиса или пусто", "score": 0-10,
 "title": "должность", "company": "компания или пусто", "salary": "зарплата или пусто",
 "grade": "Junior/Junior+/Middle/Middle+/Senior/Lead/неясно",
 "match": "что совпадает, до 12 слов", "gaps": "чего не хватает, до 12 слов или пусто"}"""


def score_vacancy(v: dict, resume: str, ds_cfg: dict, api_key: str) -> dict | None:
    user_msg = (
        f"ПРОФИЛЬ КАНДИДАТА:\n{resume}\n\n---\nВАКАНСИЯ ({v['source']}):\n"
        f"Название: {v['title']}\nКомпания: {v['company']}\nЗарплата: {v['salary_text']}\n"
        f"Ссылка: {v['url']}\n\n{v['text']}"
    )
    payload = {
        "model": ds_cfg["model"],
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_msg}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 400,
    }
    url = f"{ds_cfg['base_url'].rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        if r.status_code == 400 and "response_format" in r.text:
            # некоторые прокси не поддерживают JSON-режим — просим JSON просто промптом
            payload.pop("response_format")
            r = requests.post(url, headers=headers, json=payload, timeout=90)
        if r.status_code != 200:
            log.warning("LLM ответил %s: %s", r.status_code, r.text[:300])
            return None
        content = r.json()["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", content, re.S)  # на случай ```json ... ``` вокруг ответа
        return json.loads(m.group(0) if m else content)
    except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
        log.warning("LLM не смог оценить %s: %s", v["url"], e)
        return None


# ---------- отправка в Telegram ----------

def format_label(s: dict) -> str:
    fmt = (s.get("format") or "неясно").lower()
    city = s.get("city") or ""
    if fmt == "удалёнка":
        return "🏠 Удалёнка"
    if fmt == "гибрид":
        return f"🏙 ГИБРИД{' · ' + city if city else ''} — попробуй договориться"
    if fmt == "офис":
        return f"🏢 Офис{' · ' + city if city else ''}"
    return "❔ Формат не указан"


def format_message(v: dict, s: dict | None) -> str:
    e = html.escape
    if s is None:  # без DeepSeek или при ошибке оценки
        lines = [f"⚪️ <b>{e(v['title'])}</b>"]
        meta = " · ".join(x for x in (v["company"], v["salary_text"]) if x)
        if meta:
            lines.append(e(meta))
        lines.append(f"📍 {e(v['source'])}\n🔗 {e(v['url'])}")
        return "\n".join(lines)

    score = int(s.get("score", 0))
    dot = "🟢" if score >= 8 else "🟡" if score >= 6 else "🟠"
    title = s.get("title") or v["title"]
    company = s.get("company") or v["company"]
    salary = s.get("salary") or v["salary_text"] or "зп не указана"
    lines = [
        f"{dot} <b>{score}/10 — {e(title)}</b>",
        f"🏢 {e(company or '—')} · {e(s.get('grade', ''))}",
        f"💰 {e(salary)} · {e(format_label(s))}",
    ]
    if s.get("match"):
        lines.append(f"✅ {e(s['match'])}")
    if s.get("gaps"):
        lines.append(f"⚠️ {e(s['gaps'])}")
    lines.append(f"📍 {e(v['source'])}\n🔗 {e(v['url'])}")
    return "\n".join(lines)


def send_telegram(text: str, token: str, chat_id: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    if r.status_code != 200:
        log.warning("Telegram ответил %s: %s", r.status_code, r.text[:200])


# ---------- данные для дашборда (docs/vacancies.json → GitHub Pages) ----------

DASHBOARD_FILE = BASE_DIR / "docs" / "vacancies.json"
DASHBOARD_LIMIT = 1000  # сколько последних вакансий держать в дашборде


def dashboard_row(v: dict, s: dict | None) -> dict:
    s = s or {}
    return {
        "id": v["uid"],
        "source": v["source"],
        "title": s.get("title") or v["title"],
        "company": s.get("company") or v["company"],
        "salary": s.get("salary") or v["salary_text"],
        "grade": s.get("grade", ""),
        "format": s.get("format", ""),
        "city": s.get("city", ""),
        "score": int(s["score"]) if "score" in s else None,
        "match": s.get("match", ""),
        "gaps": s.get("gaps", ""),
        "url": v["url"],
        "published": v["published"].isoformat() if v["published"] else None,
        "found": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def save_dashboard(new_rows: list) -> None:
    rows = []
    if DASHBOARD_FILE.exists():
        try:
            rows = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8")).get("vacancies", [])
        except json.JSONDecodeError:
            log.warning("vacancies.json повреждён — пересоздаю")
    known = {r["id"] for r in rows}
    rows = [r for r in new_rows if r["id"] not in known] + rows  # новые сверху
    DASHBOARD_FILE.parent.mkdir(exist_ok=True)
    DASHBOARD_FILE.write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "vacancies": rows[:DASHBOARD_LIMIT]},
        ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- главный сценарий ----------

def main() -> None:
    cfg = load_config()
    # Резюме берём из секрета RESUME_TEXT (репо публичный), локально — из resume.md
    resume = os.getenv("RESUME_TEXT", "").strip()
    if not resume and (BASE_DIR / "resume.md").exists():
        resume = (BASE_DIR / "resume.md").read_text(encoding="utf-8")
    if not resume:
        log.warning("Резюме не найдено (нет секрета RESUME_TEXT и файла resume.md) — оценка будет грубее")
    ds_key = os.getenv("DEEPSEEK_API_KEY", "")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

    fetchers = {"habr": fetch_habr, "hh": fetch_hh, "telegram": fetch_telegram,
                "sites": fetch_html_listings}
    vacancies = []
    for name, fn in fetchers.items():
        src_cfg = cfg["sources"].get(name, {})
        if not src_cfg.get("enabled"):
            continue
        try:
            found = fn(src_cfg)
            log.info("%s: найдено %d", name, len(found))
            vacancies += found
        except Exception as e:  # один упавший источник не роняет остальные
            log.error("%s: ошибка — %s", name, e)

    seen = load_seen()
    seen_set = set(seen)
    fresh, run_keys = [], set()
    for v in vacancies:
        v["fp"] = fingerprint(v)
        keys = {v["uid"], v["fp"]}
        if keys & seen_set or keys & run_keys:
            # дубль: уже видели раньше или в этом же запуске (репост, второй запрос)
            if v["uid"] not in seen_set:
                seen.append(v["uid"])
                seen_set.add(v["uid"])
            continue
        run_keys |= keys
        fresh.append(v)
    candidates = [v for v in fresh if prefilter(v, cfg)]
    log.info("Новых: %d, прошли предфильтр: %d", len(fresh), len(candidates))

    sent = 0
    dashboard_rows = []
    for v in candidates:
        if sent >= cfg["max_messages_per_run"]:
            log.info("Достигнут лимит сообщений за запуск — остальное в следующий раз")
            break
        if v["uid"].startswith("habr:"):
            v["text"] += "\n\n" + habr_description(v["url"])
        elif v.get("needs_detail"):
            v["text"] += "\n\n" + page_text(v["url"])

        s = score_vacancy(v, resume, cfg["deepseek"], ds_key) if ds_key else None
        if s is None or s.get("is_vacancy", True):
            dashboard_rows.append(dashboard_row(v, s))  # в дашборд идут все вакансии, даже с низкой оценкой
        if s is not None:
            log.info("%s/10 — %s", s.get("score"), v["title"])
            if not s.get("is_vacancy", True) or int(s.get("score", 0)) < cfg["min_score"]:
                seen += [v["uid"], v["fp"]]
                continue

        msg = format_message(v, s)
        if tg_token and tg_chat:
            send_telegram(msg, tg_token, tg_chat)
            time.sleep(1)
        else:
            print("\n" + msg)
        seen += [v["uid"], v["fp"]]
        sent += 1

    # всё, что не прошло предфильтр, тоже запоминаем, чтобы не проверять повторно
    candidate_ids = {v["uid"] for v in candidates}
    for v in fresh:
        if v["uid"] not in candidate_ids:
            seen += [v["uid"], v["fp"]]
    save_seen(seen)
    save_dashboard(dashboard_rows)
    log.info("Отправлено: %d, в дашборд добавлено: %d", sent, len(dashboard_rows))


if __name__ == "__main__":
    main()
