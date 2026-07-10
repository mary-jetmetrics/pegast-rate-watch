"""Ежедневный трекер курса EUR туроператора Pegas Touristik.

Источник: внутренний API страницы agency.pegast.ru/ExchangeRates.
Отдаёт курс на любую дату, поэтому пропущенные дни всегда можно добрать.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

API = "https://agency.pegast.ru/ExchangeRates/GetExchangeRates"
TZ = zoneinfo.ZoneInfo("Asia/Yekaterinburg")  # Пермь, UTC+5
HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.csv")

BOOKING_DATE = dt.date(2026, 7, 10)
BOOKING_RATE = 92.44
# Историю тянем на день раньше брони, чтобы в первый же день было с чем сравнивать.
HISTORY_START = dt.date(2026, 7, 9)

TOUR_TOTAL_EUR = 3423.60

# Внесённые платежи: дата, сумма в рублях, курс зачёта, зачтено евро.
# Евро берём из учёта туроператора, а не считаем сами: он округляет вниз
# (65000 / 92.44 = 703.159, зачли 703.15), и наш остаток должен сходиться с его.
PAYMENTS = [
    (dt.date(2026, 7, 10), 65_000, 92.44, 703.15),
]

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье"]

# Пока наблюдений мало, "минимум за всё время" — пустой сигнал: показываем коридор.
MIN_DAYS_FOR_EXTREMUM = 7


def fetch_rate(day: dt.date) -> float | None:
    """Курс EUR→RUB на дату. None, если Пегас его ещё не опубликовал."""
    payload = json.dumps({"date": day.isoformat()}).encode()
    headers = {"Content-Type": "application/json",
               "X-Requested-With": "XMLHttpRequest"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(API, data=payload, headers=headers)
            raw = urllib.request.urlopen(req, timeout=20).read().decode()
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))

    # Ответ — не строгий JSON: даты приходят литералом `new Date(1783641600000)`.
    data = json.loads(re.sub(r"new Date\((\d+)\)", r"\1", raw))
    if data.get("RateNotFound") or not data.get("IsSucceeded"):
        return None
    for r in data.get("Rates", []):
        if r["SourceCurrency"] == "EUR":
            return round(float(r["Rate"]), 4)
    return None


def load_history() -> dict[dt.date, float]:
    if not os.path.exists(HISTORY):
        return {}
    with open(HISTORY, newline="", encoding="utf-8") as f:
        return {dt.date.fromisoformat(row["date"]): float(row["eur_rub"])
                for row in csv.DictReader(f)}


def save_history(history: dict[dt.date, float]) -> None:
    with open(HISTORY, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "eur_rub"])
        for day in sorted(history):
            w.writerow([day.isoformat(), f"{history[day]:.4f}".rstrip("0").rstrip(".")])


def backfill(history: dict[dt.date, float], today: dt.date) -> list[dt.date]:
    """Добирает все даты от начала наблюдения до сегодня, которых нет в истории."""
    added = []
    day = HISTORY_START
    while day <= today:
        if day not in history:
            rate = fetch_rate(day)
            if rate is not None:
                history[day] = rate
                added.append(day)
        day += dt.timedelta(days=1)
    return added


def fmt(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def fmt_money(value: float) -> str:
    """Рубли без копеек, с неразрывным пробелом между разрядами."""
    return f"{round(value):,}".replace(",", " ")


def fmt_eur(value: float) -> str:
    whole, _, cents = f"{value:.2f}".partition(".")
    return f"{int(whole):,}".replace(",", " ") + "," + cents


def fmt_delta(value: float) -> str:
    return ("+" if value > 0 else "−" if value < 0 else "") + fmt(abs(value))


def compare_line(label: str, rate: float, base: float) -> str:
    """Строка сравнения с кружком: евро дешевеет — зелёный, дорожает — красный."""
    diff = rate - base
    if abs(diff) < 0.005:
        return f"⚪️ {label}: без изменений"
    dot = "🟢" if diff < 0 else "🔴"
    pct = diff / base * 100
    return f"{dot} {label}: {fmt_delta(diff)} ₽ ({fmt_delta(pct)}%)"


def build_message(history: dict[dt.date, float], today: dt.date, repo_url: str) -> str:
    days = sorted(history)
    latest = days[-1]
    rate = history[latest]

    stale = latest < today
    lines = ["<b>Курс евро</b>" if stale else "<b>Курс евро сегодня</b>", ""]
    if stale:
        lines.append("⚠️ Курс на сегодня Пегас ещё не опубликовал, показываем последний известный.")
        lines.append("")
    lines.append(f"{latest.day} {MONTHS[latest.month - 1]}, "
                 f"{WEEKDAYS[latest.weekday()]} = {fmt(rate)} ₽")
    lines.append("")

    if len(days) > 1:
        prev = days[-2]
        # Обычно это вчера, но если Пегас пропустил день — говорим, с чем сравниваем.
        if prev == latest - dt.timedelta(days=1):
            label = "По сравнению со вчера"
        else:
            label = f"По сравнению с {prev.strftime('%d.%m')}"
        line = compare_line(label, rate, history[prev])
        if "без изменений" in line:
            held_since = latest
            for d in reversed(days[:-1]):
                if abs(history[d] - rate) >= 0.005:
                    break
                held_since = d
            line += f" (курс держится с {held_since.strftime('%d.%m')})"
        lines.append(line)

    lines.append(compare_line("По сравнению с бронью", rate, BOOKING_RATE))

    low = min(history.values())
    high = max(history.values())
    if len(days) < MIN_DAYS_FOR_EXTREMUM:
        lines.append(f"\nКоридор наблюдения: {fmt(low)} — {fmt(high)} ₽ ({len(days)} дн.)")
    elif rate <= low + 0.0001:
        lines.append("\n🔥 <b>Минимум за всё время наблюдения</b>")
    elif rate >= high - 0.0001:
        lines.append("\n🔴 <b>Максимум за всё время наблюдения</b>")
    else:
        lines.append(f"\nКоридор наблюдения: {fmt(low)} — {fmt(high)} ₽")

    if repo_url:
        lines.append(f'\n<a href="{repo_url}/blob/main/history.csv">Таблица всех наблюдений</a>')

    lines.append("")
    lines.extend(balance_lines(rate))
    return "\n".join(lines)


def balance_lines(rate: float) -> list[str]:
    paid_rub = sum(p[1] for p in PAYMENTS)
    paid_eur = sum(p[3] for p in PAYMENTS)
    left_eur = TOUR_TOTAL_EUR - paid_eur
    left_rub = left_eur * rate
    return [
        "<b>Остаток по туру</b>",
        "",
        f"Оплачено: {fmt_eur(paid_eur)} € = {fmt_money(paid_rub)} ₽",
        f"Осталось: {fmt_eur(left_eur)} € = {fmt_money(left_rub)} ₽ по текущему курсу",
        "",
        f"Стоимость тура целиком, если закрыть остаток сегодня: {fmt_money(paid_rub + left_rub)} ₽",
    ]


def send(text: str, token: str, chat_id: str) -> None:
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        result = json.loads(r.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram отклонил сообщение: {result}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="показать сообщение и не отправлять его")
    args = p.parse_args()

    today = dt.datetime.now(TZ).date()
    history = load_history()
    added = backfill(history, today)
    if added:
        save_history(history)
        print(f"Добавлено дней: {len(added)} ({', '.join(d.isoformat() for d in added)})")
    else:
        print("Новых дат нет")

    if not history:
        raise SystemExit("История пуста — Пегас не отдал ни одной даты")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    repo_url = f"https://github.com/{repo}" if repo else ""
    text = build_message(history, today, repo_url)

    if args.dry_run:
        print("\n--- сообщение ---")
        print(text)
        return

    send(text, os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"])
    print("Отправлено")


if __name__ == "__main__":
    main()
