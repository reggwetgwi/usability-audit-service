# Usability Audit Service

> **EN:** Self-hosted service that turns a website URL into a ready-to-send UX audit report.
> A lead submits a form → the manager approves the request in Telegram → a worker runs an
> LLM-driven audit over Playwright screenshots → an HTML report is delivered back to Telegram.
> FastAPI + SQLite queue + Telegram bot + headless LLM worker.

Сервис автоматического аудита юзабилити сайтов. Заявка приходит с веб-формы,
менеджер подтверждает её кнопкой в Telegram, воркер прогоняет аудит и присылает
готовый HTML-отчёт обратно в чат.

## Зачем

Ручной UX-аудит сайта занимает у специалиста несколько часов и делается под каждого
клиента заново. Здесь этот процесс сведён к одной кнопке: менеджер не открывает
редактор, не запускает скрипты и не собирает отчёт руками — он получает готовый
документ, который можно отправить клиенту.

## Как устроено

```
веб-форма (form.html)
      │  POST /audit  (+ honeypot, X-Auth-Token)
      ▼
FastAPI (app.py) ──► очередь в SQLite
      │
      ├─► карточка заявки в Telegram: «Запустить / Отклонить»
      │
      ▼ (после подтверждения)
   воркер
      ├─ capture_vm.py  →  Playwright: desktop 1440 + mobile 390, нарезка на слайсы
      ├─ LLM в headless-режиме  →  анализ слайсов + исследование ниши и ЦА
      └─ сборка HTML-отчёта  →  отправка в Telegram
```

Ключевые решения:

- **Промптов в `app.py` нет вообще.** Транспорт и сценарий аудита разделены: логика
  анализа, критерии и шаблон отчёта лежат в текстовых файлах и правятся без
  перезапуска сервиса. Перезапуск нужен только при смене лимитов и токенов в `.env`.
- **Съёмка по слайсам.** Страница режется на фрагменты по 1800 px и анализируется
  фрагмент за фрагментом. При анализе целого скриншота модель начинает додумывать
  детали, которых на странице нет; нарезка это заметно снижает.
- **Вежливый режим обхода.** Настраиваемая задержка между запросами и разброс
  интервалов — чтобы не выглядеть для хостинга клиента как атака.
- **Очередь на SQLite.** Аудит идёт минутами, поэтому заявка не обрабатывается
  в HTTP-запросе: она кладётся в очередь и подхватывается воркером.

## Стек

Python 3.11 · FastAPI · Uvicorn · SQLite · Playwright · Telegram Bot API · systemd

## Запуск

```bash
python -m venv venv && venv\Scripts\activate     # Windows
pip install -r requirements.txt
copy config.example.env .env                     # заполнить значения
uvicorn app:app --port 8080
```

Тестовая заявка:

```bash
curl -X POST http://localhost:8080/audit \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: your-secret" \
  -d '{"url": "https://example.com", "contact": "Иван, +7..."}'
```

В Telegram придёт карточка с кнопками подтверждения.

## Деплой

В каталоге `deploy/` — юнит systemd и инструкция для Ubuntu VPS (2–4 ГБ RAM).
Переменные окружения подхватываются через `EnvironmentFile`, поэтому наследуются
и дочерними процессами воркера.

## Структура

| Файл | Назначение |
|---|---|
| `app.py` | FastAPI: приём заявок, очередь, Telegram-бот, вебхук `/hook/<секрет>` |
| `capture_vm.py` | съёмка страницы через Playwright и нарезка на слайсы |
| `form.html` | публичная форма с брифом (ниша, ЦА, цели) и honeypot |
| `config.example.env` | шаблон конфигурации |
| `deploy/` | systemd-юниты, конфиг nginx, скрипты бэкапа и watchdog |

## Лицензия

MIT
