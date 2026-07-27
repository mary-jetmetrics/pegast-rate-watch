"""Телеграм-бот по запросу: команда /завтра → самый актуальный курс Пегаса.

Отдельный always-on процесс (хостится на PythonAnywhere, см. README). GitHub Actions
шлёт утреннее сообщение по расписанию — этот бот отвечает на команды в любой момент.

Слушает входящие через long-polling на голом urllib: зависимостей нет, как и у
rates.py. Логику курса не дублирует — зовёт fetch_rate и build_preview_message
из rates.py, чтобы ответ на /завтра совпадал с ручным `python3 rates.py --preview`.

Запуск: TG_BOT_TOKEN=... python3 bot.py
Отвечает только в чате TG_CHAT_ID (если задан) — чтобы бот не работал на чужих.
"""

import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import rates

TOKEN = os.environ["TG_BOT_TOKEN"]
# Необязательный фильтр: если задан, бот отвечает только в этом чате.
ALLOWED_CHAT = os.environ.get("TG_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

# В режиме --once (запуск из GitHub Actions по крону) процесс не живёт между
# запусками, поэтому offset — до какого сообщения мы уже ответили — храним в файле
# рядом и коммитим в репо, иначе на каждый прогон бот отвечал бы на всё заново.
OFFSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_offset.txt")

HELP = (
    "Команды:\n"
    "/завтра — самый актуальный курс (на завтра, а если Пегас ещё не выложил — на сегодня)\n"
    "/help — эта справка"
)


def api(method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def send(chat_id, text: str) -> None:
    api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


def preview_text() -> str:
    """Тот же ответ, что даёт `python3 rates.py --preview`, но без истории.

    /завтра истории не касается: fetch_rate ходит прямо в API, а сравнение идёт
    с сегодняшним курсом, который тоже берётся из API. Repo-ссылку не передаём —
    у хостинга нет GITHUB_REPOSITORY.
    """
    today = dt.datetime.now(rates.TZ).date()
    tomorrow = today + dt.timedelta(days=1)
    tomorrow_rate = rates.fetch_rate(tomorrow)
    today_rate = rates.fetch_rate(today)
    if tomorrow_rate is None or today_rate is None:
        return "⚠️ Пегас сейчас не отвечает, попробуйте через минуту."
    return rates.build_preview_message(today, tomorrow, tomorrow_rate, today_rate, "")


def handle(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat_id = msg["chat"]["id"]
    if ALLOWED_CHAT and str(chat_id) != ALLOWED_CHAT:
        return

    # /завтра или /завтра@ИмяБота — берём первое слово без упоминания бота.
    cmd = msg["text"].strip().split()[0].split("@")[0].lower()
    if cmd in ("/завтра", "/tomorrow", "/start"):
        send(chat_id, preview_text())
    elif cmd in ("/help", "/помощь"):
        send(chat_id, HELP)
    # На прочее молчим: чат может быть общий, не спамим на каждое сообщение.


def load_offset() -> int | None:
    if not os.path.exists(OFFSET_FILE):
        return None
    try:
        return int(open(OFFSET_FILE, encoding="utf-8").read().strip())
    except ValueError:
        return None


def save_offset(offset: int) -> None:
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        f.write(str(offset) + "\n")


def drain(offset: int | None) -> int | None:
    """Забрать накопившиеся апдейты, ответить на команды, вернуть новый offset."""
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = api("getUpdates", params)
    for update in resp.get("result", []):
        offset = update["update_id"] + 1
        try:
            handle(update)
        except Exception as e:  # один битый апдейт не должен ронять проход
            print(f"Ошибка обработки апдейта: {e}")
    return offset


def run_once() -> None:
    """Один проход для GitHub Actions: разгрести очередь и выйти.

    Actions чат не «слушает» — он просыпается по крону. Поэтому offset между
    запусками живёт в файле, который workflow коммитит в репо (см. tg-bot.yml).
    """
    offset = load_offset()
    new_offset = drain(offset)
    if new_offset is not None and new_offset != offset:
        save_offset(new_offset)
        print(f"Обработано, offset → {new_offset}")
    else:
        print("Новых сообщений нет")


def run_loop() -> None:
    """Бесконечный long-polling — для запуска на всегда-живом хосте."""
    offset = load_offset()
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            resp = api("getUpdates", params)
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle(update)
                except Exception as e:
                    print(f"Ошибка обработки апдейта: {e}")
            if offset is not None:
                save_offset(offset)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"Сеть/Telegram недоступны, повтор через 5 с: {e}")
            time.sleep(5)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true",
                   help="один проход и выход (для GitHub Actions); иначе крутится в цикле")
    if p.parse_args().once:
        run_once()
    else:
        run_loop()


if __name__ == "__main__":
    main()
