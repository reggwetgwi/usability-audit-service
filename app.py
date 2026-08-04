# -*- coding: utf-8 -*-
"""Сервис приёма заявок на аудит юзабилити.

Роль: транспорт. Принял заявку с веб-формы -> очередь -> подтверждение менеджером
в Telegram -> запуск `claude -p "/usability-audit <url>"` (по одному за раз) ->
готовый HTML-отчёт в Telegram.

Промптов в этом файле НЕТ. Сценарий аудита и шаблон отчёта — внешние файлы:
  ~/.claude/skills/usability-audit/SKILL.md                   (сценарий, критерии, правила)
  ~/.claude/skills/usability-audit/references/report-skeleton.html (шаблон отчёта)
Их можно править без перезапуска сервиса — подхватятся со следующего аудита.

Запуск: uvicorn app:app --host 0.0.0.0 --port 8080
"""
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import shutil

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# модель для аудитов; пусто = модель по умолчанию из настроек Claude Code
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
AUDIT_COMMAND = os.environ.get("AUDIT_COMMAND", "/usability-audit {url}")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", "5"))
AUDIT_TIMEOUT = int(os.environ.get("AUDIT_TIMEOUT", "5400"))
CLEANUP_AFTER_HOURS = float(os.environ.get("CLEANUP_AFTER_HOURS", "5"))
# Стенограммы claude-сессий (~25 МБ на аудит) не чистились вообще: 659 МБ к 30.07.2026.
# После сдачи они не нужны — «правь N» их не использует (has_session=False при is_revise),
# стоимость считается по run*.log, прогресс читается только у живой задачи. Держим неделю
# на случай разбора и удаляем. 0 — не удалять никогда.
SESSION_KEEP_DAYS = float(os.environ.get("SESSION_KEEP_DAYS", "7"))
# сколько аудитов выполнять одновременно; 2 — потолок для 2 CPU / 4 GB RAM
MAX_PARALLEL = max(1, int(os.environ.get("MAX_PARALLEL", "2")))
# пересчёт стоимости в рубли: цифра Claude Code ($ по прайсу Anthropic) -> реальные рубли.
# Замер 29.07.2026 (дельта баланса = дельте прайс-счётчиков, $1.35 = $1.35): freemodel
# списывает ПОЛНЫЙ прайс 1:1. Доллар баланса куплен по ~5.4 ₽ ($500 за 2700 ₽) => x5.4
COST_RUB_FACTOR = float(os.environ.get("COST_RUB_FACTOR", "5.4"))

# сетевая шара для копий отчётов (например, \\SERVER\Share\Audits).
# SMB_SHARE пустой = функция выключена; SMB_AUTH «user%pass» пустой = гостевой вход
SMB_SHARE = os.environ.get("SMB_SHARE", "").strip()
SMB_DIR = os.environ.get("SMB_DIR", "").strip()
SMB_AUTH = os.environ.get("SMB_AUTH", "").strip()


def save_to_share(path, filename):
    """Кладёт файл на офисную шару. Возвращает None при успехе/выключенной шаре,
    иначе строку с ошибкой (для тревоги в чат)."""
    if not SMB_SHARE:
        return None
    auth = ["-U", SMB_AUTH] if SMB_AUTH else ["-N"]
    try:
        r = subprocess.run(
            ["smbclient", SMB_SHARE, *auth, "-c", f'cd "{SMB_DIR}"; put "{path}" "{filename}"'],
            capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return (r.stderr or r.stdout or "smbclient error").strip()[:300]
        return None
    except Exception as e:
        return str(e)[:300]


def fmt_cost(usd):
    """'≈ 132 ₽ ($24.48 с баланса freemodel)' — freemodel списывает полный прайс
    Anthropic 1:1 (замер 29.07.2026), поэтому цифра в скобках = реальное списание."""
    return f"≈ {usd * COST_RUB_FACTOR:.0f} ₽ (${usd:.2f} с баланса freemodel)"
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
DB_PATH = DATA_DIR / "queue.db"

# файл правил команды: аудит читает его при каждом запуске (см. SKILL.md)
RULES_PATH = Path(os.environ.get(
    "RULES_FILE",
    str(Path.home() / ".claude" / "skills" / "usability-audit" / "references" / "custom-rules.md"),
))
# промпт для точечных правок готового отчёта (внешний файл — редактируется без кода)
REVISE_PROMPT_PATH = RULES_PATH.parent / "revise-prompt.md"
# основной сценарий аудита — его бот отправляет в чат по кнопке «Промпт»
SKILL_MD_PATH = RULES_PATH.parent.parent / "SKILL.md"


# ---------- база очереди ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        contact TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',  -- pending|approved|running|done|failed|rejected
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        note TEXT DEFAULT ''
    )""")
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    for col in ("context TEXT DEFAULT ''",   # контекст от клиента из брифа
                "file_id TEXT DEFAULT ''",   # id отчёта в облаке Telegram
                "cost REAL DEFAULT 0"):      # расход на задачу, $ по прайсу Anthropic
        try:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    # восстановление после рестарта: «выполняющиеся» задачи мертвы (рестарт убивает
    # процессы) — возвращаем их в очередь, воркер перезапустит
    stale = c.execute("UPDATE jobs SET status='approved' WHERE status='running'").rowcount
    if stale:
        print(f"recovery: {stale} задач возвращено в очередь после рестарта")


# ---------- telegram ----------

def tg(method, **kwargs):
    try:
        r = requests.post(f"{TG_API}/{method}", json=kwargs, timeout=30)
        return r.json()
    except Exception as e:
        print(f"telegram error: {e}")
        return {}


# постоянные кнопки под полем ввода
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📋 Правила"}, {"text": "📊 Статус"}],
        [{"text": "➕ Добавить правило"}, {"text": "✏️ Править отчёт"}],
        [{"text": "📁 Файлы"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def tg_send(text, buttons=None, keyboard=False):
    # без превью ссылок: карточка сайта раздувает служебные сообщения («статус», заявки)
    kwargs = {"chat_id": TG_CHAT, "text": text,
              "link_preview_options": {"is_disabled": True}}
    if buttons:
        kwargs["reply_markup"] = {"inline_keyboard": buttons}
    elif keyboard:
        kwargs["reply_markup"] = MAIN_KEYBOARD
    tg("sendMessage", **kwargs)


def tg_send_file(path, caption):
    """Шлёт файл в чат; возвращает file_id — по нему файл можно скачать из облака TG обратно.
    Отчёты v3 весят 6-8 МБ, а канал до Telegram у VM слабый: щедрый таймаут + 3 попытки."""
    for attempt in range(3):
        try:
            with open(path, "rb") as f:
                r = requests.post(f"{TG_API}/sendDocument", data={"chat_id": TG_CHAT, "caption": caption},
                                  files={"document": f}, timeout=600).json()
            fid = ((r.get("result") or {}).get("document") or {}).get("file_id", "")
            if fid:
                return fid
            print(f"telegram sendDocument отказ (попытка {attempt + 1}): {str(r)[:200]}")
        except Exception as e:
            print(f"telegram sendDocument error (попытка {attempt + 1}): {e}")
        time.sleep(15)
    return ""


def tg_download(file_id, dest):
    """Скачивает ранее отправленный файл из облака Telegram (лимит Bot API — 20 МБ)."""
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    fp = ((r.get("result") or {}).get("file_path"))
    if not fp:
        raise RuntimeError(f"telegram getFile: {r}")
    data = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{fp}", timeout=120)
    data.raise_for_status()
    Path(dest).write_bytes(data.content)


# ---------- защита ----------

def validate_url(raw):
    """http(s), нормальный хост, не приватный IP (анти-SSRF)."""
    u = urlparse(raw if re.match(r"^https?://", raw) else "https://" + raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("некорректный URL")
    host = u.hostname
    try:
        addr = socket.gethostbyname(host)
        if ipaddress.ip_address(addr).is_private or ipaddress.ip_address(addr).is_loopback:
            raise ValueError("приватный адрес")
    except socket.gaierror:
        raise ValueError("хост не резолвится")
    return f"{u.scheme}://{host}" + (u.path or "")


def today_count():
    # отклонённые менеджером не тратят токены — в дневной лимит не считаются
    with db() as c:
        row = c.execute("SELECT COUNT(*) n FROM jobs WHERE date(created)=? "
                        "AND status != 'rejected'", (date.today().isoformat(),)).fetchone()
    return row["n"]


# ---------- API ----------

app = FastAPI(title="usability-audit service")
# CORS: формы могут жить на внешних доменах (figma.site, ЛК); защита — секрет в URL/заголовке,
# ловушка и ручное подтверждение, а не origin
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST", "GET"],
                   allow_headers=["Content-Type", "X-Auth-Token"])


class AuditRequest(BaseModel):
    url: str
    contact: str = ""   # имя/телефон/почта клиента с формы — просто передаётся менеджеру
    company: str = ""   # старое имя honeypot (браузерные автозаполнялки его заполняли у людей!)
    xtrap9: str = ""     # honeypot: нейтральное имя, автозаполнение его не трогает
    niche: str = ""      # бриф: ниша и особенности компании
    audience: str = ""   # бриф: целевая аудитория
    comments: str = ""   # бриф: контекст и цели аудита


def enqueue(url, contact, auto=False, context=""):
    """Заявка в очередь. auto=True — внутренняя, без подтверждения кнопкой."""
    with db() as c:
        cur = c.execute("INSERT INTO jobs (url, contact, status, context) VALUES (?, ?, ?, ?)",
                        (url, contact[:200], "approved" if auto else "pending", context[:4000]))
        job_id = cur.lastrowid
    if not auto:
        extra = f"\n\n{context[:800]}" if context else ""
        tg_send(
            f"🔍 Заявка №{job_id} на аудит\nСайт: {url}\nКонтакт: {contact or '—'}{extra}",
            buttons=[[
                {"text": "✅ Запустить", "callback_data": f"approve:{job_id}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{job_id}"},
            ]],
        )
    return job_id


def find_key(data, names):
    """Рекурсивный поиск строкового значения по имени ключа в произвольном JSON."""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and str(k).lower() in names and v.strip():
                return v.strip()
        for v in data.values():
            r = find_key(v, names)
            if r:
                return r
    elif isinstance(data, list):
        for v in data:
            r = find_key(v, names)
            if r:
                return r
    return None


def flat_strings(data, out=None):
    if out is None:
        out = []
    if isinstance(data, dict):
        for v in data.values():
            flat_strings(v, out)
    elif isinstance(data, list):
        for v in data:
            flat_strings(v, out)
    elif isinstance(data, str) and data.strip():
        out.append(data.strip())
    return out


@app.get("/", response_class=HTMLResponse)
def form_page():
    html = (Path(__file__).parent / "form.html").read_text(encoding="utf-8")
    return html.replace("{{TOKEN}}", WEBHOOK_SECRET)


@app.post("/audit")
def create_audit(req: AuditRequest, x_auth_token: str = Header(default="")):
    if x_auth_token != WEBHOOK_SECRET:
        raise HTTPException(403, "bad token")
    if today_count() >= MAX_PER_DAY:
        raise HTTPException(429, "превышен дневной лимит заявок — напишите нам напрямую")
    try:
        url = validate_url(req.url.strip())
    except ValueError as e:
        raise HTTPException(422, str(e))
    parts = []
    # ловушка сработала — заявку НЕ отбрасываем (автозаполнение ловит и людей),
    # а помечаем: последнее слово за кнопкой менеджера
    if req.xtrap9 or req.company:
        parts.append("⚠️ Заполнено скрытое поле — возможен бот, проверьте контакт перед запуском")
    if req.niche.strip():
        parts.append("Ниша и особенности (со слов клиента): " + req.niche.strip())
    if req.audience.strip():
        parts.append("Целевая аудитория (со слов клиента): " + req.audience.strip())
    if req.comments.strip():
        parts.append("Контекст и цели аудита (со слов клиента): " + req.comments.strip())
    job_id = enqueue(url, req.contact, context="\n".join(parts))
    return {"ok": True, "job_id": job_id, "status": "ждёт подтверждения менеджером"}


@app.post("/hook/{secret}")
async def form_hook(secret: str, request: Request):
    """Приёмник для конструкторов форм (Figma, Tilda и т.п.): секрет в URL,
    произвольный формат тела — поля url/contact находим сами."""
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403, "bad secret")
    if today_count() >= MAX_PER_DAY:
        raise HTTPException(429, "превышен дневной лимит заявок")
    try:
        data = await request.json()
    except Exception:
        data = dict((await request.form()) or {})
    raw_url = find_key(data, {"url", "site", "website", "domain", "сайт", "адрес", "адрес сайта"})
    if not raw_url:  # fallback: первая строка, похожая на домен
        raw_url = next((s for s in flat_strings(data)
                        if "@" not in s and re.search(r"[a-zа-я0-9-]+\.[a-zа-я]{2,}", s, re.I)), None)
    if not raw_url:
        raise HTTPException(422, "в заявке не найден адрес сайта")
    try:
        url = validate_url(raw_url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    name = find_key(data, {"name", "имя"})
    phone = find_key(data, {"phone", "телефон", "tel"})
    email = find_key(data, {"email", "e-mail", "почта"})
    contact = find_key(data, {"contact", "контакт"}) or \
        ", ".join(x for x in (name, phone, email) if x)
    if not contact:
        contact = " | ".join(s for s in flat_strings(data) if s != raw_url)[:200]
    parts = []
    for keys, label in (({"niche", "ниша"}, "Ниша и особенности"),
                        ({"audience", "аудитория", "ца"}, "Целевая аудитория"),
                        ({"comments", "комментарии", "контекст"}, "Контекст и цели аудита")):
        val = find_key(data, keys)
        if val:
            parts.append(f"{label} (со слов клиента): {val}")
    job_id = enqueue(url, contact or "", context="\n".join(parts))
    return {"ok": True, "job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: int, x_auth_token: str = Header(default="")):
    if x_auth_token != WEBHOOK_SECRET:
        raise HTTPException(403, "bad token")
    with db() as c:
        row = c.execute("SELECT id, url, status, created, note FROM jobs WHERE id=?",
                        (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such job")
    return dict(row)


# ---------- правки сценария аудита через чат ----------

RULES_HEADER = "# Правила от команды (пополняется через Telegram-бота, вручную не редактировать)\n"


def load_rules():
    if not RULES_PATH.exists():
        return []
    return [l[2:].strip() for l in RULES_PATH.read_text(encoding="utf-8").splitlines()
            if l.startswith("- ")]


def save_rules(rules):
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULES_PATH.write_text(RULES_HEADER + "".join(f"- {r}\n" for r in rules), encoding="utf-8")


HELP_TEXT = (
    "Что я умею:\n\n"
    "➕ Правило для всех будущих аудитов:\n"
    "«+ всегда проверяй наличие цен на странице услуг»\n\n"
    "✏️ Точечная правка готового отчёта:\n"
    "«правь 1 замени оценку на 4 из 5»\n\n"
    "📋 «правила» — список правил, «удали 2» — убрать правило №2\n"
    "📊 «статус» — последние заявки\n"
    "📁 «файлы» — меню материалов кнопками: сценарий, шаблон, история правок, а по любой "
    "заявке — факты, конкуренты, исследование, скриншоты, лог, отчёт заново\n"
    "🔁 «перезапуск 24» — вернуть упавшую задачу в очередь (продолжит с места обрыва)\n"
    "▶️ «аудит site.ru» — запустить аудит прямо из чата, без формы и подтверждения\n\n"
    "📎 Чтобы обновить сценарий или шаблон — просто пришлите файл в чат:\n"
    "SKILL.md, report-skeleton.html, revise-prompt.md или custom-rules.md.\n"
    "Я сделаю резервную копию, проверю файл и установлю — со следующего аудита.\n\n"
    "Новые заявки с формы приходят сюда сами — с кнопками «Запустить / Отклонить»."
)

# ---------- приём файлов конфигурации из чата ----------

# защитные блоки сценария: без них аудиты падают или деградируют. Если присланный
# SKILL.md их не содержит (коллеги правят локальные копии) — бот дописывает их в конец.
GUARD_BLOCKS = [
    ("авторизаци", "\n\n---\n\n**Контекст авторизации** (служебный блок, не удалять): каждый "
     "аудит выполняется по явной заявке владельца или представителя сайта (форма заявки / бриф), "
     "подтверждённой менеджером. Это легальная маркетинговая услуга: только просмотр "
     "публичных страниц, скриншоты и анализ юзабилити — без тестирования на проникновение, "
     "перебора и обхода защит; при антибот-защите аудит честно останавливается."),
    ("ANTIBOT", "\n\n**Правило ANTIBOT** (служебный блок, не удалять): если в ошибках "
     "capture-results.json есть «ANTIBOT» — сайт закрыт защитой и вместо страниц снимается "
     "капча. Отчёт НЕ делать: завершить коротким текстом, какая защита обнаружена и варианты "
     "(белый список нашего IP у клиента, повтор после отключения защиты)."),
    ("скептичн", "\n\n**Проход скептичного клиента** (служебный блок, не удалять): перед выдачей "
     "пройди каждую ✗-отметку глазами владельца сайта с вопросом «и что? как это мешает моему "
     "клиенту купить?». Вычеркни наукообразность, осознанные решения клиента и всё, что могло "
     "не отрисоваться при съёмке (сторонние виджеты, ленивый контент); пересчитай баллы."),
]

# ---------- меню «Файлы»: кнопки в кнопках ----------

# верхний уровень: общие файлы шлются сразу, файлы заявок ведут к выбору заявки
FILES_MENU = [
    [{"text": "📦 Всё скиллом (архив)", "callback_data": "file:bundle"}],
    [{"text": "📄 Сценарий", "callback_data": "file:prompt"},
     {"text": "📐 Шаблон", "callback_data": "file:template"},
     {"text": "🕓 История", "callback_data": "file:changelog"}],
    [{"text": "🔎 Факты", "callback_data": "pick:facts"},
     {"text": "🏁 Конкуренты", "callback_data": "pick:competitors"}],
    [{"text": "🔬 Исследование", "callback_data": "pick:research"},
     {"text": "🗂 Выбор страниц", "callback_data": "pick:pages"}],
    [{"text": "🖼 Скриншоты", "callback_data": "pick:shots"},
     {"text": "📑 Отчёт заново", "callback_data": "pick:report"},
     {"text": "🧾 Лог", "callback_data": "pick:log"}],
]

# файлы заявки: имя внутри папки задачи + подпись
ARTIFACT_KINDS = {
    "facts": ("facts.md", "🔎 Факты (наблюдения по слайсам) аудита №{}"),
    "competitors": ("competitors.md", "🏁 Матрица конкурентов по аудиту №{}"),
    "research": ("research.md", "🔬 Исследование ниши и ЦА по аудиту №{}"),
    "pages": ("pages.json", "🗂 Страницы, выбранные для аудита №{}"),
}


def send_skill_bundle():
    """Одним архивом — всё, чем живёт аудит: сценарий, шаблон, скрипты, правила, форма.
    Коллеги просят «скинь папку со скиллом» — по кнопке уходит готовый zip, а не файлы по одному.
    Мусор (бэкапы, кэш) не кладём: архив должен разворачиваться как рабочая копия скилла."""
    skill_dir = SKILL_MD_PATH.parent
    if not skill_dir.exists():
        return "Папка скилла не найдена на сервере — сообщите разработчику."
    stamp = time.strftime("%Y%m%d-%H%M")
    out = Path("/tmp") / f"usability-audit-skill-{stamp}.zip"
    skip = (".bak", ".pyc", ".log")
    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(skill_dir.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            if f.suffix in skip or ".bak-" in f.name:
                continue
            z.write(f, Path("usability-audit") / f.relative_to(skill_dir))
            added += 1
        form = Path(__file__).parent / "form.html"
        if form.exists():
            z.write(form, "форма-заявки/form.html")
            added += 1
    version = ""
    m = re.search(r"version:\s*([\d.]+)", SKILL_MD_PATH.read_text(encoding="utf-8")[:400])
    if m:
        version = f" v{m.group(1)}"
    rules = load_rules()
    caption = (f"📦 Скилл аудита{version} целиком — {added} файлов, "
               f"{out.stat().st_size // 1024} КБ.\n\n"
               "Внутри: SKILL.md (сценарий), references/ (шаблон отчёта, промпт правок, "
               "правила команды), scripts/ (съёмка, нарезка, конкуренты keys.so), "
               "CHANGELOG.md, форма заявок.\n"
               f"Правил команды поверх сценария: {len(rules)}.\n\n"
               "Чтобы поставить у себя: распакуйте папку usability-audit "
               "в ~/.claude/skills/ — и /usability-audit заработает.\n"
               "Доработанный файл можно прислать сюда же — установлю на сервер сам.")
    try:
        tg_send_file(out, caption)
    finally:
        out.unlink(missing_ok=True)  # архив живёт только на время отправки
    return None


def retry_job(job_id):
    """Возвращает упавшую задачу в очередь. Сессия на диске сохранена, поэтому воркер
    продолжит с места обрыва (--continue), а не начнёт аудит заново — почти готовая
    работа не пропадает (типовой случай: все попытки легли на волне ошибок API)."""
    with db() as c:
        row = c.execute("SELECT status, url FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return f"Заявки №{job_id} не существует."
    if row["status"] != "failed":
        return (f"Заявка №{job_id} в статусе «{row['status']}» — перезапускать можно только "
                f"упавшие (💥). Готовые правятся через «правь {job_id} …».")
    resume = any(session_dir(job_id).glob("*.jsonl"))
    with db() as c:
        c.execute("UPDATE jobs SET status='approved', note='' WHERE id=?", (job_id,))
    how = ("продолжу с места обрыва — наработки сохранились"
           if resume else "сессии не осталось, начну аудит заново")
    return f"🔁 Задача №{job_id} ({row['url']}) снова в очереди: {how}."


def job_pick_buttons(kind):
    """Кнопки последних заявок для выбора, по какой прислать материал."""
    with db() as c:
        rows = c.execute("SELECT id, url FROM jobs WHERE status IN ('done','failed','running') "
                         "ORDER BY id DESC LIMIT 8").fetchall()
    btns, row = [], []
    for r in rows:
        dom = (urlparse(r["url"]).netloc or r["url"]).replace("www.", "")
        row.append({"text": f"№{r['id']} {dom[:22]}", "callback_data": f"art:{kind}:{r['id']}"})
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    return btns


def send_artifact(kind, job_id):
    """Шлёт материал заявки; возвращает текст ответа, если слать нечего."""
    job_dir = JOBS_DIR / str(job_id)
    if kind in ARTIFACT_KINDS:
        fname, caption = ARTIFACT_KINDS[kind]
        files = sorted(job_dir.glob(f"**/{fname}"))
        if not files:
            return f"По заявке №{job_id} файла {fname} нет — не создан или заявка не выполнялась."
        tg_send_file(files[0], caption.format(job_id))
        return None
    if kind == "log":
        log = job_dir / "run.log"
        if not log.exists():
            return f"Лога по заявке №{job_id} нет."
        tg_send_file(log, f"🧾 Лог запуска аудита №{job_id} (последняя строка — итог и стоимость)")
        failed = sorted(job_dir.glob("run-failed*.log"))
        if failed:
            tg_send_file(failed[-1], f"🧾 Лог последней НЕудачной попытки №{job_id}")
        return None
    if kind == "report":
        with db() as c:
            row = c.execute("SELECT file_id, url FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["file_id"]:
            tg("sendDocument", chat_id=TG_CHAT, document=row["file_id"],
               caption=f"📑 Отчёт №{job_id}: {row['url']} (копия из облака)")
            return None
        local = sorted(job_dir.glob("**/audit-*.html"))
        if local:
            tg_send_file(local[0], f"📑 Отчёт №{job_id} (с сервера)")
            return None
        return f"Отчёта №{job_id} нет ни в облаке Telegram, ни на сервере."
    if kind == "shots":
        shots = sorted(job_dir.glob("**/shots/*.png"))
        if not shots:
            return (f"Скриншоты №{job_id} уже удалены автоочисткой (живут ~"
                    f"{int(CLEANUP_AFTER_HOURS)} ч после аудита). Если нужно свежее — "
                    f"«аудит <сайт>» снимет заново.")
        btns, row = [], []
        for p in shots[:24]:
            row.append({"text": p.stem[:32], "callback_data": f"shot:{job_id}:{p.stem[:40]}"})
            if len(row) == 2:
                btns.append(row)
                row = []
        if row:
            btns.append(row)
        tg_send(f"🖼 Скриншоты аудита №{job_id} — какой прислать?", buttons=btns)
        return None
    return "Неизвестный тип материала."


def handle_files_callback(data):
    """Кнопки меню «Файлы». Возвращает текст ответа или None, если файл уже отправлен."""
    if data.startswith("file:"):
        kind = data[5:]
        if kind == "prompt":
            return handle_text("промпт")
        if kind == "template":
            return handle_text("шаблон")
        if kind == "bundle":
            return send_skill_bundle()
        if kind == "changelog":
            log = SKILL_MD_PATH.parent / "CHANGELOG.md"
            if not log.exists():
                return "Файла истории правок нет."
            tg_send_file(log, "🕓 История правок сценария (CHANGELOG.md)")
            return None
    if data.startswith("pick:"):
        kind = data[5:]
        btns = job_pick_buttons(kind)
        if not btns:
            return "Выполненных заявок ещё нет."
        tg_send("По какой заявке прислать?", buttons=btns)
        return None
    if data.startswith("art:"):
        _, kind, jid = data.split(":", 2)
        return send_artifact(kind, int(jid))
    if data.startswith("shot:"):
        _, jid, name = data.split(":", 2)
        files = sorted((JOBS_DIR / jid).glob(f"**/shots/{name}*.png"))
        if not files:
            return f"Скриншот «{name}» по заявке №{jid} не найден (возможно, очищен)."
        tg_send_file(files[0], f"🖼 {name} — аудит №{jid}")
        return None
    return None


# имя файла (по подстроке, без учёта регистра и суффиксов «SKILL-17847...md») -> куда ставить
def config_file_target(filename):
    low = filename.lower()
    if low.endswith(".md") and "skill" in low:
        return SKILL_MD_PATH, "сценарий аудита (SKILL.md)"
    if low.endswith(".html") and "skeleton" in low:
        return RULES_PATH.parent / "report-skeleton.html", "HTML-шаблон отчёта"
    if low.endswith(".md") and "revise" in low:
        return REVISE_PROMPT_PATH, "промпт правок (revise-prompt.md)"
    if low.endswith(".md") and ("custom-rules" in low or "rules" in low):
        return RULES_PATH, "правила команды (custom-rules.md)"
    return None, None


def handle_document(doc):
    """Файл в чате: если это один из файлов конфигурации — бэкап, проверка, установка."""
    name = doc.get("file_name") or ""
    target, title = config_file_target(name)
    if not target:
        return ("Не узнаю файл «{}». Принимаю: SKILL.md (сценарий), report-skeleton.html "
                "(шаблон отчёта), revise-prompt.md (промпт правок), custom-rules.md "
                "(правила).".format(name or "без имени"))
    if (doc.get("file_size") or 0) > 19_000_000:
        return "Файл больше 19 МБ — Telegram не даст боту его скачать. Ужмите или передайте иначе."
    tmp = DATA_DIR / f"upload-{int(time.time())}-{name}"
    try:
        tg_download(doc["file_id"], tmp)
        text = tmp.read_text(encoding="utf-8")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return f"Не смог скачать/прочитать файл: {e}"
    # проверки, ломающие систему — отказ; недостающие защитные блоки — дописываем сами
    notes = []
    if title.startswith("сценарий"):
        if len(text) < 2000 or "тап" not in text:
            tmp.unlink(missing_ok=True)
            return "Это не похоже на сценарий аудита (слишком короткий или без этапов) — не ставлю."
        for marker, block in GUARD_BLOCKS:
            if marker.lower() not in text.lower():
                text += block
                notes.append(f"дописан служебный блок «{marker}»")
    elif title.startswith("HTML"):
        if "{{IMG_" not in text:
            tmp.unlink(missing_ok=True)
            return ("В шаблоне нет ни одного токена {{IMG_...}} — сборщик не сможет вставить "
                    "скриншоты. Верните токены на места картинок и пришлите снова.")
        if "corp-logo" not in text:
            notes.append("⚠️ в шаблоне не видно корпоративного логотипа (class=corp-logo)")
    elif "revise" in str(target):
        if "{instruction}" not in text:
            tmp.unlink(missing_ok=True)
            return ("В промпте правок нет плейсхолдера {instruction} — без него правки "
                    "сломаются. Добавьте {instruction} в то место, куда подставлять задачу.")
    backup = target.with_name(target.name + time.strftime(".bak-%Y%m%d-%H%M"))
    try:
        if target.exists():
            shutil.copy2(target, backup)
        target.write_text(text, encoding="utf-8")
    except Exception as e:
        return f"Не смог записать файл на сервер: {e}"
    finally:
        tmp.unlink(missing_ok=True)
    extra = ("\n(" + "; ".join(notes) + ")") if notes else ""
    return (f"✅ Установлен новый {title} — применится со следующего аудита.{extra}\n"
            f"Резервная копия: {backup.name} (откат — сообщите разработчику или пришлите её же).")


def session_dir(job_id):
    return Path.home() / ".claude" / "projects" / f"-opt-audit-service-data-jobs-{job_id}"


def job_readiness(job_id):
    """Оценка готовности аудита в % по артефактам на диске. Этапы оставляют
    предсказуемые файлы, поэтому диск честнее, чем последний вызов инструмента.
    Внутри самого долгого этапа (разбор слайсов) прогресс уточняется долей
    страниц из pages.json, уже записанных в facts.md."""
    job_dir = JOBS_DIR / str(job_id)
    work = next(iter(sorted(job_dir.glob("usability-audit-*"))), None)
    if not work:
        return 2
    pct = 5  # папка создана, идёт разведка структуры
    if (work / "pages.json").exists():
        pct = 8
    if (work / "research.md").exists():
        pct = 18
    if (work / "keyso.md").exists() or (work / "competitors.md").exists():
        pct = 25
    if (work / "competitors.md").exists():
        pct = 35
    if (work / "capture-results.json").exists():
        pct = 42
    if (work / "slices").is_dir():
        pct = 45
    facts = work / "facts.md"
    if facts.exists():
        # 45..75%: доля страниц, уже разобранных в facts.md
        try:
            pages = [pg["name"] for pg in
                     json.loads((work / "pages.json").read_text(encoding="utf-8"))["pages"]]
            body = facts.read_text(encoding="utf-8", errors="replace")
            done_pages = sum(1 for name in pages if name in body)
            pct = 45 + int(30 * done_pages / max(1, len(pages)))
        except Exception:
            pct = max(pct, 50)
    if (work / "report-template.html").exists():
        pct = 80
    if list(work.glob("audit-*.html")):
        pct = 90
    if (work / "render").is_dir():
        pct = 96
    return pct


def job_progress(job_id):
    """Живой прогресс выполняющегося аудита: этап + активность из журнала сессии.
    Журналы субагентов тоже считаются: модель выносит крупные этапы (конкуренты)
    в Agent, основной журнал при этом молчит десятки минут — это не зависание
    (случай №23: «светофор» показал красный при живом субагенте)."""
    files = sorted(session_dir(job_id).glob("**/*.jsonl"), key=os.path.getmtime)
    if not files:
        return "готовится к запуску"
    f = files[-1]
    idle = int(time.time() - f.stat().st_mtime)
    # если самый свежий журнал лежит в subagents/ — работает вынесенный субагент;
    # его задачу берём из соседнего .meta.json (кладётся при запуске Agent)
    agent_note = ""
    if f.parent.name == "subagents":
        agent_note = "субагент"
        try:
            meta = json.loads(f.with_suffix("").with_suffix(".meta.json").read_text(encoding="utf-8"))
            desc = (meta.get("description") or meta.get("label") or "").strip()
            if desc:
                agent_note = f"субагент «{desc[:60]}»"
        except Exception:
            pass
    last_tool = ""
    try:
        tail = f.read_bytes()[-60000:].decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            try:
                d = json.loads(line)
            except Exception:
                continue
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in reversed(content):
                    if b.get("type") == "tool_use":
                        last_tool = f"{b.get('name')} {str(b.get('input'))}"
                        break
            if last_tool:
                break
    except Exception:
        pass
    tl = last_tool.lower()
    if "capture.py" in tl:
        stage = "съёмка страниц"
    elif "slice.py" in tl or "extract_nav" in tl:
        stage = "нарезка скриншотов"
    elif "/slices/" in tl:
        m = re.search(r"/slices/([\w.-]+)", last_tool)
        stage = f"анализ скриншотов ({m.group(1)})" if m else "анализ скриншотов"
    elif "websearch" in tl or "webfetch" in tl or "research" in tl:
        stage = "исследование ниши"
    elif "build_report" in tl or "report-template" in tl or "skeleton" in tl:
        stage = "сборка отчёта"
    elif "render" in tl:
        stage = "самопроверка отчёта"
    elif "sitemap" in tl or "robots" in tl:
        stage = "разведка структуры сайта"
    else:
        stage = "работает"
    if idle < 180:
        alive = "🟢 активен"
    elif idle < 600:
        alive = f"🟡 тишина {idle // 60} мин"
    else:
        alive = f"🔴 тишина {idle // 60} мин — похоже, зависло"
    try:
        pct = job_readiness(job_id)
        stage = f"{stage} · готовность ≈{pct}%"
    except Exception:
        pass
    if agent_note:
        return f"{stage} · {alive} · 🤖 {agent_note}"
    return f"{stage} · {alive}"


def jobs_status():
    with db() as c:
        rows = c.execute("SELECT id, url, status, cost FROM jobs ORDER BY id DESC LIMIT 5").fetchall()
        month = c.execute("SELECT COALESCE(SUM(cost),0) s FROM jobs "
                          "WHERE strftime('%Y-%m', created) = strftime('%Y-%m', 'now')").fetchone()["s"]
    if not rows:
        return "Заявок ещё не было."
    ico = {"pending": "🕐 ждёт подтверждения", "approved": "▶️ в очереди", "running": "⏳ выполняется",
           "done": "✅ готов", "failed": "💥 ошибка", "rejected": "❌ отклонена", "revise": "🔧 правка в очереди"}
    lines = []
    for r in rows:
        line = (f"№{r['id']} · {r['url']} · {ico.get(r['status'], r['status'])}"
                + (f" · {r['cost'] * COST_RUB_FACTOR:.0f} ₽" if r["cost"] else ""))
        if r["status"] == "running":
            line += f"\n      └ {job_progress(r['id'])}"
        lines.append(line)
    return ("Последние заявки:\n" + "\n".join(lines)
            + f"\n\n💰 Расход за месяц: {fmt_cost(month)}")


def handle_text(text):
    """Кнопки, слэш-команды и текстовые команды в чате."""
    text = text.strip()
    parts = text.split(maxsplit=1)
    if parts and parts[0].startswith("/"):
        cmd, rest = parts[0].split("@")[0].lower(), (parts[1] if len(parts) > 1 else "")
        text = {"/rules": "правила", "/status": "статус", "/prompt": "промпт",
                "/template": "шаблон", "/files": "файлы", "/skill": "скилл",
                "/add": ("+ " + rest) if rest else "➕ Добавить правило",
                "/fix": ("правь " + rest) if rest else "✏️ Править отчёт",
                "/start": "помощь", "/help": "помощь"}.get(cmd, "помощь")
    low = text.lower()
    if low in ("помощь", "help", "ℹ️ помощь"):
        return HELP_TEXT
    if low in ("промпт", "📄 промпт", "prompt"):
        if not SKILL_MD_PATH.exists():
            return "Файл сценария не найден на сервере — сообщите разработчику."
        tg_send_file(SKILL_MD_PATH,
                     "📄 Текущий сценарий аудита (SKILL.md).\n"
                     "Дорабатывается сообщением «+ правило» прямо здесь — правки применяются "
                     "со следующего аудита.")
        rules = load_rules()
        if rules:
            return "Поверх сценария действуют правила команды:\n" + "\n".join(
                f"{i}. {r}" for i, r in enumerate(rules, 1))
        return "Правил команды поверх сценария пока нет («+ текст» — добавить)."
    if low in ("скилл", "архив", "📦 скилл", "bundle", "папка", "исходники"):
        return send_skill_bundle()
    if low in ("файлы", "📁 файлы", "files", "материалы"):
        tg_send("📁 Что прислать?", buttons=FILES_MENU)
        return None
    if low in ("шаблон", "📐 шаблон", "template"):
        skeleton = RULES_PATH.parent / "report-skeleton.html"
        if not skeleton.exists():
            return "Шаблон отчёта не найден на сервере — сообщите разработчику."
        tg_send_file(skeleton,
                     "📐 HTML-шаблон отчёта (report-skeleton.html).\n"
                     "Правила заполнения — в комментариях внутри. Токены {{IMG_...}}, классы "
                     "отметок и ширину колонки 760px не менять. Доработанный файл пришлите "
                     "сюда же — установлю сам.")
        return None
    if low in ("статус", "📊 статус"):
        return jobs_status()
    if low == "➕ добавить правило":
        return ("Отправьте правило одним сообщением, начиная с плюса:\n\n"
                "+ всегда проверяй наличие цен на странице услуг\n\n"
                "Пишите как задачу живому аудитору — сработает со следующего аудита.")
    if low == "✏️ править отчёт":
        return ("Отправьте одним сообщением: правь <номер заявки> <что изменить>\n\n"
                "Например: правь 1 убери из плана пункт про cookie\n\n"
                "Номер заявки видно в «📊 Статус» и в подписи к отчёту.")
    if low in ("правила", "список", "rules", "📋 правила"):
        rules = load_rules()
        if not rules:
            return "Правил пока нет. Добавить: «+ текст правила»."
        return "Правила для всех следующих аудитов:\n" + "\n".join(
            f"{i}. {r}" for i, r in enumerate(rules, 1))
    if text.startswith("+"):
        rule = " ".join(text[1:].split())
        if len(rule) < 5:
            return "Слишком коротко. Пример: «+ всегда проверяй наличие цен на странице услуг»"
        rules = load_rules()
        rules.append(rule)
        save_rules(rules)
        return f"✏️ Правило №{len(rules)} сохранено, применится со следующего аудита:\n«{rule}»"
    m = re.match(r"^(?:-|удали(?:ть)?)\s*(\d+)$", low)
    if m:
        i = int(m.group(1))
        rules = load_rules()
        if not 1 <= i <= len(rules):
            return f"Нет правила №{i}. Посмотреть список: «правила»"
        removed = rules.pop(i - 1)
        save_rules(rules)
        return f"🗑 Правило №{i} удалено: «{removed}»"
    m = re.match(r"^исследование\s+(\d+)$", low)
    if m:
        research = sorted((JOBS_DIR / m.group(1)).glob("**/research.md"))
        if not research:
            return f"Исследования по заявке №{m.group(1)} нет (заявка не выполнялась или файл не создан)."
        tg_send_file(research[0], f"🔬 Исследование ниши и ЦА по аудиту №{m.group(1)}")
        return None
    m = re.match(r"^(?:аудит|audit)\s+(\S+)\s*$", text, re.I)
    if m:
        if today_count() >= MAX_PER_DAY:
            return f"Дневной лимит ({MAX_PER_DAY}) исчерпан — попробуйте завтра или поднимите MAX_PER_DAY."
        try:
            url = validate_url(m.group(1))
        except ValueError as e:
            return f"Не могу взять в работу: {e}"
        job_id = enqueue(url, "внутренняя заявка из чата", auto=True)
        return f"▶️ Аудит №{job_id} ({url}) поставлен в очередь без подтверждения — заявка из чата команды."
    m = re.match(r"^(?:перезапуск|перезапусти|продолжи|retry)\s+(\d+)\s*$", low)
    if m:
        return retry_job(int(m.group(1)))
    m = re.match(r"^(?:правь|поправь|исправь)\s+(\d+)[\s,:—-]+(.+)$", text, re.I | re.S)
    if m:
        job_id, instruction = int(m.group(1)), " ".join(m.group(2).split())
        with db() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return f"Заявки №{job_id} не существует."
        if row["status"] not in ("done",):
            return f"Заявка №{job_id} в статусе «{row['status']}» — править можно только готовые отчёты."
        with db() as c:
            c.execute("UPDATE jobs SET status='revise', note=? WHERE id=?", (instruction, job_id))
        return f"🔧 Правка отчёта №{job_id} в очереди: «{instruction}». Обновлённый файл пришлю сюда."
    return HELP_TEXT


# ---------- подтверждение через telegram (long polling) ----------

def poll_telegram():
    with db() as c:
        row = c.execute("SELECT v FROM meta WHERE k='tg_offset'").fetchone()
    offset = int(row["v"]) if row else 0
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates",
                             params={"offset": offset + 1, "timeout": 50}, timeout=60).json()
            for upd in r.get("result", []):
                offset = upd["update_id"]
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO meta VALUES ('tg_offset', ?)", (str(offset),))
                msg = upd.get("message")
                if msg and str(msg.get("chat", {}).get("id")) == str(TG_CHAT) and msg.get("text"):
                    reply = handle_text(msg["text"])
                    if reply:
                        tg_send(reply, keyboard=True)
                    continue
                if msg and str(msg.get("chat", {}).get("id")) == str(TG_CHAT) and msg.get("document"):
                    reply = handle_document(msg["document"])
                    if reply:
                        tg_send(reply, keyboard=True)
                    continue
                cb = upd.get("callback_query")
                if not cb or str(cb["message"]["chat"]["id"]) != str(TG_CHAT):
                    continue  # реагируем только на кнопки из нашего чата
                data = cb["data"]
                if data.startswith("retry:"):
                    tg("answerCallbackQuery", callback_query_id=cb["id"])
                    tg_send(retry_job(int(data.split(":")[1])), keyboard=True)
                    continue
                if data.startswith(("approve:", "reject:")):
                    action, job_id = data.split(":")
                    new_status = "approved" if action == "approve" else "rejected"
                    with db() as c:
                        c.execute("UPDATE jobs SET status=? WHERE id=? AND status='pending'",
                                  (new_status, job_id))
                    tg("answerCallbackQuery", callback_query_id=cb["id"],
                       text="Запускаю" if action == "approve" else "Отклонено")
                    if action == "approve":
                        tg_send(f"▶️ Заявка №{job_id} в очереди на выполнение.")
                    else:
                        # отклонили — убираем сообщение заявки, чтобы не засорять чат
                        tg("deleteMessage", chat_id=TG_CHAT,
                           message_id=cb["message"]["message_id"])
                    continue
                # меню «Файлы» и его подуровни
                tg("answerCallbackQuery", callback_query_id=cb["id"])
                try:
                    reply = handle_files_callback(data)
                except Exception as e:
                    reply = f"Не получилось: {e}"
                if reply:
                    tg_send(reply, keyboard=True)
        except Exception as e:
            print(f"poll error: {e}")
            time.sleep(5)


# ---------- воркер: один аудит за раз ----------

def _cost_of(log_path):
    try:
        for line in reversed(Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return float(json.loads(line).get("total_cost_usd") or 0)
    except Exception as e:
        print(f"parse_cost: {e}")
    return 0.0


def parse_cost(log_path):
    """Полная стоимость задачи в $ (по прайсу Anthropic) — сумма ВСЕХ попыток.
    При обрывах API потраченное до обрыва лежит в run-failed-N.log, а run.log
    хранит только последнюю попытку: №23 показал «2 ₽» за многочасовой аудит.
    uuid результата защищает от двойного счёта (run.log дублирует последний фейл)."""
    log_path = Path(log_path)
    total, seen = 0.0, set()
    for f in sorted(log_path.parent.glob("run*.log")):
        try:
            for line in reversed(f.read_text(encoding="utf-8", errors="replace").splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    d = json.loads(line)
                    uid = d.get("uuid") or f.name
                    if uid not in seen:
                        seen.add(uid)
                        total += float(d.get("total_cost_usd") or 0)
                    break
        except Exception as e:
            print(f"parse_cost {f.name}: {e}")
    return total


RESUME_PROMPT = ("Предыдущий запуск оборвался из-за ошибки API. Продолжи выполнение задачи "
                 "с места обрыва: рабочая папка, файлы и заметки сохранились — не переделывай "
                 "уже сделанное, доведи аудит до готового отчёта по сценарию.")


def run_job(job, prompt, require_update_after=None, resume=False):
    job_dir = JOBS_DIR / str(job["id"])
    job_dir.mkdir(exist_ok=True)
    log_path = job_dir / "run.log"
    # промпт через stdin: многострочный текст в аргументе cmd.exe обрезается на переносе строки
    # json-вывод: в конце лога сводка с total_cost_usd и токенами
    cmd = [CLAUDE_BIN, "-p", "--dangerously-skip-permissions", "--output-format", "json"]
    if resume:
        cmd.append("--continue")  # продолжаем прерванную сессию вместо старта с нуля
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    started = time.time()
    # Прошлый заход мог погибнуть внезапно: OOM убивает процесс, ветка except не отрабатывает
    # и run-failed-N.log не создаётся. Сохраняем предыдущий лог ДО перезаписи — иначе вместе с
    # ним пропадает сводка total_cost_usd и стоимость занижается (№34 показал 16 ₽ вместо
    # нескольких сотен: три OOM-захода просто не попали в счёт).
    if log_path.exists() and log_path.stat().st_size:
        keep = job_dir / f"run-prev-{int(log_path.stat().st_mtime)}.log"
        if not keep.exists():
            shutil.copy(log_path, keep)
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=job_dir, stdout=log, stderr=subprocess.STDOUT,
                              input=prompt, encoding="utf-8", timeout=AUDIT_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude завершился с кодом {proc.returncode}, см. {log_path}")
    reports = sorted(job_dir.glob("**/audit-*.html"), key=os.path.getmtime)
    if not reports:
        # модель иногда создаёт рабочую папку в /tmp вопреки сценарию — забираем результат к себе
        stray = [p for p in Path("/tmp").glob("usability-audit-*/audit-*.html")
                 if p.stat().st_mtime > started]
        if stray:
            shutil.move(str(stray[-1].parent), str(job_dir / stray[-1].parent.name))
            reports = sorted(job_dir.glob("**/audit-*.html"), key=os.path.getmtime)
    if not reports:
        # вытащим финальный ответ модели — там обычно человеческое объяснение (капча и т.п.)
        hint = ""
        try:
            for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    hint = (json.loads(line).get("result") or "")[:400]
                    break
        except Exception:
            pass
        raise RuntimeError(f"отчёт не создан. {hint or 'см. ' + str(log_path)}")
    report = reports[-1]
    if require_update_after and os.path.getmtime(report) <= require_update_after:
        raise RuntimeError(f"файл отчёта не изменился — правка не применилась, см. {log_path}")
    return report


def claim_job():
    """Атомарный захват задачи из очереди: при нескольких воркерах выигрывает один."""
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE status IN ('approved','revise') "
                        "ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        cur = c.execute("UPDATE jobs SET status='running' WHERE id=? AND status=?",
                        (row["id"], row["status"]))
        if cur.rowcount == 0:
            return None  # задачу перехватил другой воркер
    return row


def worker():
    while True:
        job = claim_job()
        if not job:
            time.sleep(10)
            continue
        is_revise = job["status"] == "revise"  # статус на момент захвата
        if is_revise:
            job_dir = JOBS_DIR / str(job["id"])
            job_dir.mkdir(exist_ok=True)
            # отчёт мог быть удалён автоочисткой — восстанавливаем из облака Telegram
            if not list(job_dir.glob("**/audit-*.html")) and (job["file_id"] or ""):
                try:
                    tg_download(job["file_id"], job_dir / f"audit-restored-{job['id']}.html")
                except Exception as e:
                    print(f"restore from telegram failed: {e}")
            prompt = REVISE_PROMPT_PATH.read_text(encoding="utf-8").format(instruction=job["note"])
            tg_send(f"⏳ Вношу правку в отчёт №{job['id']}, обычно 5–15 минут…")
        else:
            prompt = AUDIT_COMMAND.format(url=job["url"])
            ctx = job["context"] if "context" in job.keys() else ""
            if ctx:
                prompt += ("\n\nКонтекст от клиента из брифа (используй в этапе исследования; "
                           "проверяй, но не выдумывай сверх него):\n" + ctx)
            tg_send(f"⏳ Аудит №{job['id']} ({job['url']}) запущен, обычно 30–60 минут…")
        try:
            old_mtime = None
            if is_revise:
                existing = sorted((JOBS_DIR / str(job["id"])).glob("**/audit-*.html"),
                                  key=os.path.getmtime)
                old_mtime = os.path.getmtime(existing[-1]) if existing else None
            # обрывы API-шлюза не должны убивать длинную задачу: до 3 продолжений
            # с места обрыва (--continue), а не рестарт с нуля. Если у задачи уже есть
            # прерванная сессия (например, после рестарта сервиса) — тоже продолжаем её.
            has_session = not is_revise and any(session_dir(job["id"]).glob("*.jsonl"))
            attempt = 0
            fresh_restart = False  # сессия «отравлена» шлюзом — резюм невозможен, только с нуля
            while True:
                try:
                    use_resume = (attempt > 0 or has_session) and not fresh_restart
                    report = run_job(job, RESUME_PROMPT if use_resume else prompt,
                                     require_update_after=old_mtime, resume=use_resume)
                    break
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    attempt += 1
                    failed_log = JOBS_DIR / str(job["id"]) / "run.log"
                    err_tail = ""
                    if failed_log.exists():
                        shutil.copy(failed_log, failed_log.with_name(f"run-failed-{attempt}.log"))
                        err_tail = failed_log.read_text(encoding="utf-8", errors="replace")[-2500:]
                    if attempt > 3:
                        raise
                    # «each thinking block must contain thinking» — шлюз повредил сессию при
                    # --continue (№25): повторы в ту же сессию бесполезны, начинаем с чистой
                    if not is_revise and ("thinking block" in err_tail or attempt == 3):
                        fresh_restart = True
                        shutil.rmtree(session_dir(job["id"]), ignore_errors=True)
                        how = "сессия повреждена шлюзом — начинаю аудит заново"
                    else:
                        how = "продолжаю с места обрыва"
                    # перегрузка шлюза приходит сериями (№24: 4 попытки за 5 минут впустую) —
                    # ждём с нарастанием, а не долбим сразу
                    delay = 180 * attempt
                    tg_send(f"⚠️ Задача №{job['id']}: {str(e)[:160]} — {how} "
                            f"через {delay // 60} мин (попытка {attempt + 1} из 4)…")
                    time.sleep(delay)
            with db() as c:
                c.execute("UPDATE jobs SET status='done', note=? WHERE id=?",
                          (str(report), job["id"]))
            cost = parse_cost(JOBS_DIR / str(job["id"]) / "run.log")
            cost_line = f"\n💰 {fmt_cost(cost)}" if cost else ""
            caption = (f"🔧 Правка отчёта №{job['id']} готова: {job['url']}{cost_line}" if is_revise
                       else f"✅ Аудит №{job['id']} готов: {job['url']}\n"
                            f"Контакт клиента: {job['contact'] or '—'}{cost_line}")
            with db() as c:
                c.execute("UPDATE jobs SET cost = cost + ? WHERE id=?", (cost, job["id"]))
            fid = tg_send_file(report, caption)
            if fid:  # отчёт в облаке TG — локальную копию можно чистить
                with db() as c:
                    c.execute("UPDATE jobs SET file_id=? WHERE id=?", (fid, job["id"]))
            # копия на офисную шару дизайнеров; правка перезаписывает файл той же заявки
            share_err = save_to_share(report, f"{job['id']:03d}-{Path(report).name}")
            if share_err:
                tg_send(f"⚠️ Отчёт №{job['id']} в Telegram отправлен, но на сервак "
                        f"(Designers/Аудиты Claude) не сохранился: {share_err}")
            if not is_revise:
                research = sorted((JOBS_DIR / str(job["id"])).glob("**/research.md"))
                if research:
                    tg_send_file(research[0], f"🔬 Исследование ниши и ЦА по аудиту №{job['id']} "
                                              f"— на этих критериях построены выводы отчёта")
        except Exception as e:
            with db() as c:
                c.execute("UPDATE jobs SET status='failed', note=? WHERE id=?",
                          (str(e)[:500], job["id"]))
            tg_send(f"💥 Задача №{job['id']} упала: {e}",
                    buttons=[[{"text": "🔁 Продолжить с места обрыва",
                               "callback_data": f"retry:{job['id']}"}]])


# ---------- автоочистка диска ----------

HEAVY = {"shots", "slices", "html", "render", "compare"}  # сырьё аудита; отчёт и factsмd остаются


def cleanup_job_dir(job_dir, drop_report=False):
    """Удаляет тяжёлое сырьё завершённого аудита. drop_report=True (отчёт уже в облаке TG) —
    удаляет и сам отчёт; остаются только run.log, facts.md и research.md."""
    freed = 0
    for p in job_dir.rglob("*"):
        if p.is_dir() and p.name in HEAVY:
            freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            shutil.rmtree(p, ignore_errors=True)
    extra = list(job_dir.rglob("report-template.html")) + list(job_dir.rglob("*.png"))
    if drop_report:
        extra += list(job_dir.rglob("audit-*.html"))
    for p in extra:
        if p.is_file():
            freed += p.stat().st_size
            p.unlink(missing_ok=True)
    return freed


def prune_sessions():
    """Удаляет стенограммы сессий завершённых задач старше SESSION_KEEP_DAYS.
    Сессии живых задач не трогаем: по ним job_progress показывает прогресс,
    а ретрай продолжает работу через --continue."""
    if SESSION_KEEP_DAYS <= 0:
        return
    cutoff = time.time() - SESSION_KEEP_DAYS * 86400
    root = Path.home() / ".claude" / "projects"
    for sess in root.glob("-opt-audit-service-data-jobs-*"):
        if not sess.is_dir():
            continue
        job_id = sess.name.rsplit("-", 1)[-1]
        newest = max((f.stat().st_mtime for f in sess.rglob("*.jsonl")), default=0)
        if not newest or newest > cutoff:
            continue
        with db() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] in ("running", "approved", "revise"):
            continue
        freed = sum(f.stat().st_size for f in sess.rglob("*") if f.is_file())
        shutil.rmtree(sess, ignore_errors=True)
        print(f"cleanup: сессия job {job_id}, освобождено {freed // 1024 // 1024} MB")


def cleanup_worker():
    while True:
        try:
            cutoff = time.time() - CLEANUP_AFTER_HOURS * 3600
            for job_dir in JOBS_DIR.iterdir():
                if not job_dir.is_dir():
                    continue
                log = job_dir / "run.log"
                # run.log дописывается в момент завершения; свежий или отсутствующий — не трогаем
                if not log.exists() or log.stat().st_mtime > cutoff:
                    continue
                with db() as c:
                    row = c.execute("SELECT status, file_id FROM jobs WHERE id=?",
                                    (job_dir.name,)).fetchone()
                if row and row["status"] == "running":
                    continue
                # отчёт удаляем только если он подтверждённо лежит в облаке Telegram
                drop_report = bool(row and (row["file_id"] or ""))
                if any(job_dir.rglob("shots")) or (drop_report and any(job_dir.rglob("audit-*.html"))):
                    freed = cleanup_job_dir(job_dir, drop_report=drop_report)
                    if freed:
                        print(f"cleanup: job {job_dir.name}, освобождено {freed // 1024 // 1024} MB")
            prune_sessions()
        except Exception as e:
            print(f"cleanup error: {e}")
        time.sleep(1800)


# меню слэш-команд рядом с полем ввода (идемпотентно, ставится при каждом старте)
tg("setMyCommands", commands=[
    {"command": "help", "description": "Справка: как пользоваться ботом"},
    {"command": "rules", "description": "Правила для будущих аудитов"},
    {"command": "add", "description": "Добавить правило: /add текст"},
    {"command": "fix", "description": "Править отчёт: /fix номер и что изменить"},
    {"command": "status", "description": "Статус последних заявок"},
    {"command": "prompt", "description": "Прислать файл текущего сценария аудита"},
    {"command": "template", "description": "Прислать HTML-шаблон отчёта"},
    {"command": "files", "description": "Меню материалов: факты, конкуренты, скрины, логи"},
])

threading.Thread(target=poll_telegram, daemon=True).start()
for _ in range(MAX_PARALLEL):
    threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=cleanup_worker, daemon=True).start()
