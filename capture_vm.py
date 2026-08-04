# -*- coding: utf-8 -*-
"""Полностраничные скриншоты (desktop+mobile) + отрендеренный HTML.

Запуск: python capture.py <workdir>
Ожидает <workdir>/pages.json: {"pages": [{"name": "home", "url": "https://..."}]}
Пишет: shots/{name}-desktop.png, shots/{name}-mobile.png, html/{name}.html,
capture-results.json

Чтобы нас не банили по IP («вежливый режим»), настраивается через окружение:
  CAPTURE_DELAY_SEC (6)   пауза с разбросом перед каждой загрузкой, кроме первой; 0 — выкл.
                          Если robots.txt сайта просит Crawl-delay больше — берём его.
  CAPTURE_MAX_PAGES (10)  потолок страниц за прогон (каждая = 2 загрузки, desktop+mobile).
  CAPTURE_FORCE=1         снимать заново даже то, что уже снято (по умолчанию не трогаем).
Съёмка сама останавливается, если сайт начал отшивать: HTTP 429/503 — сразу, две
блокировки (403/антибот) подряд или три любых неудачи подряд — тоже. Недоснятые
страницы получают в capture-results.json поле skipped с причиной.

Переменная окружения CAPTURE_PROXY (необязательная): если сайт не открывается
напрямую (обрыв/таймаут — часть сайтов режет датацентровые диапазоны), страница
переснимается через этот прокси. Формат http://user:pass@host:port или socks5://…
Нужен резидентный/мобильный адрес — датацентровый попадёт под ту же блокировку.

Chromium — самый прожорливый этап аудита (память/CPU). При параллельных аудитах
съёмки выполняются ПО ОЧЕРЕДИ: межпроцессный замок /tmp/audit-chromium.lock
(fcntl, POSIX) — второй процесс ждёт, пока первый доснимет. Замок исчезает
вместе с упавшим процессом, вечных блокировок не бывает. На Windows (локальная
отладка) замок просто пропускается.
"""
import json
import os
import random
import sys
import time

from PIL import Image
from playwright.sync_api import sync_playwright

try:
    import fcntl  # POSIX only
except ImportError:
    fcntl = None

LOCK_PATH = "/tmp/audit-chromium.lock"


def chromium_lock():
    """Блокирующий захват общесистемного замка на съёмку. Возвращает файл-держатель."""
    if fcntl is None:
        return None
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Chromium занят другим аудитом — жду своей очереди на съёмку…", flush=True)
        t0 = time.time()
        fcntl.flock(lock_file, fcntl.LOCK_EX)  # блокирующее ожидание
        print(f"очередь подошла (ждал {int(time.time() - t0)}с)", flush=True)
    return lock_file

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.abspath(sys.argv[1])
PAGES = json.load(open(os.path.join(BASE, "pages.json"), encoding="utf-8"))["pages"]
os.makedirs(os.path.join(BASE, "shots"), exist_ok=True)
os.makedirs(os.path.join(BASE, "html"), exist_ok=True)

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ширины съёмки (SKILL.md v3): типовой полноэкранный монитор и типовой смартфон
DESKTOP_WIDTH = 1920
MOBILE_WIDTH = 375

# Фолбэк через прокси: часть сайтов режет датацентровые диапазоны целиком, и с нашего
# egress страница не открывается вовсе (обрыв/таймаут), хотя с обычного домашнего или
# мобильного адреса работает. Тогда переснимаем тот же URL через прокси из окружения.
# CAPTURE_PROXY=http://user:pass@host:port (или socks5://…), пусто — фолбэк выключен.
# Нужен резидентный/мобильный адрес: датацентровый прокси попадает под ту же блокировку.
def _parse_proxy(raw):
    """'http://user:pass@host:port' -> dict для playwright (или None)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    from urllib.parse import urlparse
    u = urlparse(raw if "://" in raw else "http://" + raw)
    if not u.hostname:
        return None
    proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}" if u.port
                       else f"{u.scheme}://{u.hostname}"}
    if u.username:
        proxy["username"] = u.username
        proxy["password"] = u.password or ""
    return proxy


PROXY = _parse_proxy(os.environ.get("CAPTURE_PROXY"))

# Вежливый режим. Одна страница = два полных прогона (desktop+mobile) с прокруткой и
# дозагрузкой всех картинок; подряд без паузы это выглядит для антифрода хостинга как
# краулер — 29.07.2026 нас за это забанили по IP на хостинге клиента. Пауза со случайным
# разбросом ставится перед каждой загрузкой, кроме первой. CAPTURE_DELAY_SEC=0 — выключить.
DELAY_SEC = float(os.environ.get("CAPTURE_DELAY_SEC") or 6)
_hits = [0]


def throttle():
    """Пауза перед очередным обращением к сайту (первое — без ожидания)."""
    if DELAY_SEC > 0 and _hits[0]:
        wait = random.uniform(DELAY_SEC * 0.6, DELAY_SEC * 1.4)
        print(f"  пауза {wait:.1f}с перед следующей загрузкой (вежливый режим)", flush=True)
        time.sleep(wait)
    _hits[0] += 1


# Потолок на число страниц: защита от раздутого pages.json (каждая страница — ДВЕ полных
# загрузки, desktop+mobile). Отброшенное печатаем — молча урезать нельзя.
MAX_PAGES = int(os.environ.get("CAPTURE_MAX_PAGES") or 10)
if len(PAGES) > MAX_PAGES:
    print(f"ВНИМАНИЕ: страниц {len(PAGES)}, снимаю первые {MAX_PAGES} "
          f"(лимит CAPTURE_MAX_PAGES). Отброшены: "
          f"{', '.join(p['name'] for p in PAGES[MAX_PAGES:])}", flush=True)
    PAGES = PAGES[:MAX_PAGES]

# Повторный прогон не должен снимать заново то, что уже снято: при ретрае аудита
# (app.py даёт до 4 попыток, а при сбросе сессии агент запускает съёмку с нуля) сайт
# иначе получает те же 12–16 загрузок повторно. CAPTURE_FORCE=1 — снять всё принудительно.
FORCE = os.environ.get("CAPTURE_FORCE") == "1"
RESULTS_PATH = os.path.join(BASE, "capture-results.json")
_prev = {}
if not FORCE and os.path.exists(RESULTS_PATH):
    try:
        _prev = json.load(open(RESULTS_PATH, encoding="utf-8"))
    except Exception:
        _prev = {}


def already_captured(name):
    """Снимки этой страницы уже лежат и прошлый прогон её не провалил?"""
    old = _prev.get(name)
    if not isinstance(old, dict) or old.get("error") or old.get("skipped"):
        return False
    need = [os.path.join(BASE, "shots", f"{name}-desktop.png"),
            os.path.join(BASE, "shots", f"{name}-mobile.png"),
            os.path.join(BASE, "html", f"{name}.html")]
    return all(os.path.exists(p) and os.path.getsize(p) > 1024 for p in need)


def robots_rules(url):
    """(crawl_delay, парсер) из robots.txt сайта. Одна короткая попытка, сбой не критичен."""
    from urllib import robotparser
    from urllib.parse import urlparse
    import urllib.request
    try:
        u = urlparse(url)
        req = urllib.request.Request(f"{u.scheme}://{u.netloc}/robots.txt",
                                     headers={"User-Agent": DESKTOP_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            lines = r.read(300000).decode("utf-8", "replace").splitlines()
        rp = robotparser.RobotFileParser()
        rp.parse(lines)
        return rp.crawl_delay("*"), rp
    except Exception as e:
        print(f"robots.txt не прочитан ({type(e).__name__}) — работаю со своими паузами",
              flush=True)
        return None, None

# сетевые обрывы — повод перезайти через прокси. Антибот и капча в список НЕ входят:
# там страница отдаётся, просто с защитой, и смена адреса не помогает.
NET_ERRORS = ("ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_TIMED_OUT",
              "ERR_CONNECTION_REFUSED", "ERR_EMPTY_RESPONSE", "ERR_TIMED_OUT",
              "ERR_SSL_PROTOCOL_ERROR", "ERR_ADDRESS_UNREACHABLE", "ERR_NETWORK_CHANGED",
              "Timeout", "net::ERR")

# признаки страниц антибот-защиты (вместо сайта — заглушка/капча)
BLOCK_STRONG = ["smartcaptcha", "ddos-guard", "checking your browser", "проверка браузера",
                "qrator", "подтвердите, что вы не робот", "are you a robot", "cf-chl",
                "attention required", "доступ ограничен", "access denied",
                "проведите вправо", "slide to verify", "потяните ползунок",
                "передвиньте ползунок", "соберите пазл"]


def frame_covers_page(page, frame):
    """Виден ли фрейм и занимает ли он экран. Botfaqtor и подобные держат служебный
    iframe размером 0×0 на КАЖДОЙ странице с их счётчиком — сам факт его наличия
    блокировкой не является (случай magazin-kaplya: сайт снимался нормально,
    а съёмка падала). Капча закрывает контент, поэтому меряем видимую площадь."""
    try:
        el = frame.frame_element()
        box = el.bounding_box()
        if not box:
            return False  # display:none / вне потока — служебный
        vw = page.evaluate("innerWidth")
        vh = page.evaluate("innerHeight")
        visible = el.evaluate("""e => {
            const s = getComputedStyle(e);
            return s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.1;
        }""")
        # капча — минимум треть экрана по обеим сторонам; счётчики дают 0×0 или 1×1
        return bool(visible and box["width"] > vw * 0.3 and box["height"] > vh * 0.3)
    except Exception:
        return False


def looks_blocked(page):
    # капча-виджеты живут в iframe — текст страницы их не выдаёт, проверяем адреса фреймов
    try:
        for fr in page.frames:
            u = (fr.url or "").lower()
            # reCAPTCHA-чекбокс у формы обратной связи — норма, а не блокировка сайта
            if "google.com/recaptcha" in u or "gstatic.com/recaptcha" in u:
                continue
            if any(x in u for x in ("captcha", "ddos-guard", "qrator", "challenge",
                                    "botfaqtor", "blocked.")):
                # тревога только если фрейм реально закрывает страницу собой
                if frame_covers_page(page, fr):
                    return f"антибот-виджет ({u[:70]})"
    except Exception:
        pass
    try:
        txt = (page.title() + " " + page.evaluate(
            "document.body ? document.body.innerText.slice(0, 8000) : ''")).lower()
    except Exception:
        return None
    for m in BLOCK_STRONG:
        if m in txt:
            return m
    # антиботы часто размывают контент CSS-фильтром, пока капча не решена.
    # blur бывает и дизайн-приёмом, поэтому тревога — только если размыт КРУПНЫЙ блок
    try:
        blurred = page.evaluate("""() => {
            const vw = innerWidth, vh = innerHeight;
            for (const el of document.querySelectorAll('*')) {
                const s = getComputedStyle(el);
                if (!(s.filter || '').includes('blur')) continue;
                const r = el.getBoundingClientRect();
                if (r.width > vw * 0.7 && r.height > vh * 0.7) return true;
            }
            return false;
        }""")
        if blurred:
            return "страница размыта защитой (blur до решения капчи)"
    except Exception:
        pass
    # слово «captcha» само по себе бывает и в подвале обычного сайта (reCAPTCHA у формы) —
    # считаем блокировкой только на «пустых» страницах-заглушках
    if "captcha" in txt and len(txt) < 1500:
        return "captcha-заглушка"
    return None


def capture(pw, name, url, mobile, proxy=None):
    args = {
        "viewport": {"width": MOBILE_WIDTH if mobile else DESKTOP_WIDTH,
                     "height": 812 if mobile else 1080},
        "user_agent": MOBILE_UA if mobile else DESKTOP_UA,
        "locale": "ru-RU",
    }
    if mobile:
        args.update(device_scale_factor=2, is_mobile=True, has_touch=True)
    throttle()  # не долбим сервер загрузками подряд — иначе бан по IP на хостинге
    # маскировка от антибот-защит: убираем главный маркер автоматизации
    launch_args = {"args": ["--disable-blink-features=AutomationControlled"]}
    if proxy:
        launch_args["proxy"] = proxy
    browser = pw.chromium.launch(**launch_args)
    ctx = browser.new_context(**args)
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en']});")
    page = ctx.new_page()
    t0 = time.time()
    # domcontentloaded вместо networkidle: антиботы и вечные пинги не дают сети «затихнуть»
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        browser.close()  # иначе при обрыве Chromium остаётся висеть процессом-сиротой
        raise
    # сайт прямым текстом говорит, что мы лишние: 429/503 — «сбавь темп», 403 — «не пущу».
    # Это команда остановиться, а не повод идти по остальным страницам и добивать лимит.
    if resp is not None and resp.status in (403, 429, 503):
        code = resp.status
        browser.close()
        raise RuntimeError(f"{'RATELIMIT' if code in (429, 503) else 'FORBIDDEN'}: "
                           f"сайт ответил HTTP {code}")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass  # сеть не затихла — работаем с тем, что загрузилось
    # антибот-заглушка? JS-проверки часто пропускают после паузы и перезагрузки
    if looks_blocked(page):
        page.wait_for_timeout(8000)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        blocker = looks_blocked(page)
        if blocker:
            browser.close()
            raise RuntimeError(f"ANTIBOT: сайт закрыт защитой ({blocker}) — вместо страниц "
                               f"снимается капча, аудит невозможен без белого списка")
    load_sec = round(time.time() - t0, 1)  # до полной сетевой тишины
    perf = page.evaluate("""() => {
        const t = performance.timing;
        return { dcl_ms: t.domContentLoadedEventEnd - t.navigationStart,
                 load_ms: (t.loadEventEnd || Date.now()) - t.navigationStart };
    }""")
    page.wait_for_timeout(2500)
    # ленивые картинки переводим в жадные — часть подгрузится сама, без прокрутки
    page.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(i => { i.loading = 'eager'; })")
    # медленная прокрутка до низа: даём IntersectionObserver'ам сработать,
    # а картинкам — успеть скачаться (это главная защита от серых заглушек)
    page.evaluate("""async () => {
        await new Promise(resolve => {
            let total = 0;
            const step = () => {
                window.scrollBy(0, 600); total += 600;
                if (total >= document.body.scrollHeight) resolve();
                else setTimeout(step, 300);
            };
            step();
        });
    }""")
    # явное ожидание дозагрузки всех запущенных картинок (максимум 15 с)
    page.evaluate("""async () => {
        const pending = Array.from(document.images).filter(i => !i.complete);
        await Promise.race([
            Promise.all(pending.map(i => new Promise(r => { i.onload = i.onerror = r; }))),
            new Promise(r => setTimeout(r, 15000)),
        ]);
    }""")
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass  # сайты с вечными пингами — не повод падать
    # второй быстрый проход снизу вверх: ловит блоки, съехавшие после дозагрузки
    page.evaluate("""async () => {
        for (let y = document.body.scrollHeight; y > 0; y -= 1200) {
            window.scrollTo(0, y);
            await new Promise(r => setTimeout(r, 120));
        }
        window.scrollTo(0, 0);
    }""")
    page.wait_for_timeout(2000)
    # полностраничный снимок растягивает viewport и пересчитывает srcset (sizes=auto -> 100vw),
    # запуская НОВЫЕ загрузки вместо уже показанных картинок — снимок ловит их серыми.
    # Замораживаем текущий выбор браузера: src = currentSrc, адаптивность отключаем.
    page.evaluate("""document.querySelectorAll('img').forEach(i => {
        if (i.currentSrc) i.src = i.currentSrc;
        i.removeAttribute('srcset'); i.removeAttribute('sizes');
    })""")
    page.wait_for_timeout(500)
    # финальная проверка: капча-виджеты часто подгружаются уже после старта (во время прокрутки)
    blocker = looks_blocked(page)
    if blocker:
        browser.close()
        raise RuntimeError(f"ANTIBOT: сайт закрыт защитой ({blocker}) — вместо страниц "
                           f"снимается капча, аудит невозможен без белого списка")
    shot = os.path.join(BASE, "shots", f"{name}-{'mobile' if mobile else 'desktop'}.png")
    page.screenshot(path=shot, full_page=True)
    if not mobile:
        with open(os.path.join(BASE, "html", f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    size = Image.open(shot).size
    browser.close()
    return size, {"networkidle_sec": load_sec, **perf}


results = {}
_lock_holder = chromium_lock()  # очередь на Chromium: съёмка — по одному аудиту за раз

# robots.txt хозяина сайта: если он просит паузу больше нашей — слушаемся его
CRAWL_DELAY, ROBOTS = robots_rules(PAGES[0]["url"]) if PAGES else (None, None)
if CRAWL_DELAY and CRAWL_DELAY > DELAY_SEC:
    print(f"robots.txt просит Crawl-delay {CRAWL_DELAY}с — поднимаю паузу "
          f"с {DELAY_SEC}с", flush=True)
    DELAY_SEC = float(CRAWL_DELAY)

aborted = None      # причина досрочной остановки: сайт начал нас отшивать
blocks = 0          # блокирующих ответов подряд
fails = 0           # любых неудач подряд
with sync_playwright() as pw:
    for item in PAGES:
        name, url = item["name"], item["url"]
        if aborted:
            results[name] = {"url": url, "skipped": aborted}
            continue
        if already_captured(name):
            print(f"{name}: уже снято в прошлый прогон — не трогаю сайт повторно", flush=True)
            results[name] = _prev[name]
            continue
        if ROBOTS is not None and not ROBOTS.can_fetch("*", url):
            print(f"{name}: ВНИМАНИЕ, robots.txt закрывает этот URL от роботов "
                  f"({url}) — снимаю, т.к. аудит согласован с владельцем", flush=True)
        used_proxy = False
        try:
            try:
                d, speed = capture(pw, name, url, mobile=False)
            except Exception as e:
                # сеть не пустила напрямую — второй заход через прокси (если он задан)
                if not (PROXY and any(s in str(e) for s in NET_ERRORS)):
                    raise
                print(f"{name}: прямое соединение оборвалось ({str(e).splitlines()[0][:70]}), "
                      f"пробую через прокси…", flush=True)
                d, speed = capture(pw, name, url, mobile=False, proxy=PROXY)
                used_proxy = True
            m, m_speed = capture(pw, name, url, mobile=True,
                                 proxy=PROXY if used_proxy else None)
            print(f"{name}: desktop {d} за {speed['networkidle_sec']}s "
                  f"(DCL {speed['dcl_ms']}ms), mobile {m}"
                  f"{' [через прокси]' if used_proxy else ''}")
            results[name] = {"url": url, "desktop": d, "mobile": m,
                             "speed": speed, "speed_mobile": m_speed,
                             "via_proxy": used_proxy}
            blocks = fails = 0  # страница снята — счётчики отбоя сбрасываем
        except Exception as e:
            msg = str(e)
            print(f"{name} ERROR: {msg}")
            results[name] = {"url": url, "error": msg,
                             "proxy_tried": bool(PROXY) and not used_proxy}
            fails += 1
            # сайт попросил сбавить темп — прекращаем немедленно, это прямой запрет
            if "RATELIMIT:" in msg:
                aborted = ("сайт ответил 429/503 — просит сбавить темп; съёмка остановлена, "
                           "чтобы не получить бан по IP")
            elif "FORBIDDEN:" in msg or "ANTIBOT:" in msg:
                blocks += 1
                if blocks >= 2:
                    aborted = ("сайт блокирует запросы (две страницы подряд) — съёмка "
                               "остановлена, нужен белый список")
            if not aborted and fails >= 3:
                aborted = "три страницы подряд не открылись — дальше не долблю сайт"
            if aborted:
                print(f"СТОП: {aborted}", flush=True)

with open(os.path.join(BASE, "capture-results.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("DONE")
