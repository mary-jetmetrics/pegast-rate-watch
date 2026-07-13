"""Ежедневный трекер курса EUR туроператора Pegas Touristik.

Источник: внутренний API страницы agency.pegast.ru/ExchangeRates.

Как Пегас ставит курс: по будням после 17:30 МСК, на следующий день. Пятничный
курс держится всю субботу, воскресенье и понедельник. То есть к утреннему запуску
курс на сегодня уже установлен и финален. Но в волатильные дни оператор оставляет
за собой право переставить курс несколько раз за день, поэтому последние дни мы
перечитываем, а не считаем записанное один раз навсегда верным.

Два режима:
  * обычный — утреннее сообщение с курсом на сегодня и остатком по туру;
  * --preview — курс на завтра, руками после 17:30 МСК. В историю его не пишем,
    см. fetch_rate.

Утреннее сообщение должно быть ровно одно в день и прийти гарантированно, а
расписания GitHub Actions гарантий не дают: 13.07 запуск опоздал на 3 часа 18 минут,
а другой в тот же день не случился вовсе. Поэтому запусков несколько (см. daily.yml),
а отправленное отмечается в last_sent.txt: доехавший первым шлёт, остальные молчат.
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

PAGE = "https://agency.pegast.ru/ExchangeRates"
API = f"{PAGE}/GetExchangeRates"
TZ = zoneinfo.ZoneInfo("Asia/Yekaterinburg")  # Пермь, UTC+5
HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(HERE, "history.csv")
# Дата последнего отправленного утреннего сообщения — защита от повторов, когда
# до Пегаса доехало сразу несколько запусков подряд.
SENT = os.path.join(HERE, "last_sent.txt")

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

# Сколько последних дней перечитывать поверх записанного, чтобы поймать
# внутридневной пересмотр курса. Прошедший день API отдаёт уже окончательным.
RECHECK_DAYS = 5


def fetch_rate(day: dt.date) -> float | None:
    """Курс EUR→RUB, действующий на указанную дату.

    Спрашивать будущие даты бессмысленно: на дату, курс на которую ещё не
    установлен, API не отвечает ошибкой, а молча отдаёт последний известный курс
    (проверено — на год вперёд возвращает сегодняшний). Так что None здесь
    означает сбой API, а не "курса на этот день ещё нет": сказать второе он не умеет.
    """
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


def load_sent() -> dt.date | None:
    if not os.path.exists(SENT):
        return None
    raw = open(SENT, encoding="utf-8").read().strip()
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def save_sent(day: dt.date) -> None:
    with open(SENT, "w", encoding="utf-8") as f:
        f.write(day.isoformat() + "\n")


Revision = tuple[dt.date, float, float]  # дата, было, стало


def sync_history(history: dict[dt.date, float],
                 today: dt.date) -> tuple[list[dt.date], list[Revision]]:
    """Добирает пропущенные даты и перечитывает последние RECHECK_DAYS.

    Перечитывание нужно на случай, когда Пегас переставил курс уже после того,
    как мы его записали. Дальше сегодняшнего дня не ходим: на будущие даты API
    отдаёт протянутое значение, и записать его значило бы придумать курс.
    """
    added: list[dt.date] = []
    revised: list[Revision] = []
    recheck_from = today - dt.timedelta(days=RECHECK_DAYS - 1)

    day = HISTORY_START
    while day <= today:
        known = history.get(day)
        if known is None or day >= recheck_from:
            rate = fetch_rate(day)
            if rate is None:
                pass
            elif known is None:
                history[day] = rate
                added.append(day)
            elif abs(rate - known) >= 0.0001:
                history[day] = rate
                revised.append((day, known, rate))
        day += dt.timedelta(days=1)
    return added, revised


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


def build_message(history: dict[dt.date, float], today: dt.date, repo_url: str,
                  revised: list[Revision]) -> str:
    days = sorted(history)
    latest = days[-1]
    rate = history[latest]

    # Курс на сегодня к утру всегда есть, так что сюда попадаем только если API сбоил.
    stale = latest < today
    lines = ["<b>Курс евро</b>" if stale else "<b>Курс евро сегодня</b>", ""]
    if stale:
        lines.append("⚠️ Курс на сегодня получить не удалось, показываем последний известный.")
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

    # Если курс за уже показанный день переставили, честно говорим об этом:
    # иначе цифры в истории молча разойдутся с теми, что были в прошлом сообщении.
    for day, was, now in revised:
        if day != latest:
            lines.append(f"\nℹ️ Курс за {day.strftime('%d.%m')} пересмотрен задним "
                         f"числом: {fmt(was)} → {fmt(now)} ₽")

    lines.append("")
    if repo_url:
        lines.append(f'<a href="{repo_url}/blob/main/history.csv">Таблица всех наблюдений</a>')
    lines.append(f'<a href="{PAGE}">Курс на сайте Пегаса</a>')

    lines.append("")
    lines.extend(balance_lines(rate))
    return "\n".join(lines)


def build_preview_message(tomorrow: dt.date, rate: float, today_rate: float,
                          repo_url: str) -> str:
    """Вечернее сообщение: курс, который Пегас выставил на завтра."""
    lines = ["<b>Предварительный курс на завтра</b>", ""]
    lines.append(f"{tomorrow.day} {MONTHS[tomorrow.month - 1]}, "
                 f"{WEEKDAYS[tomorrow.weekday()]} = {fmt(rate)} ₽")
    lines.append("")
    lines.append(compare_line("По сравнению с сегодня", rate, today_rate))
    lines.append(compare_line("По сравнению с бронью", rate, BOOKING_RATE))

    # API не умеет отвечать "курса на эту дату ещё нет" — на неустановленную дату он
    # отдаёт последний известный. Поэтому совпадение с сегодняшним курсом неотличимо
    # от "Пегас ещё не выставил новый", и молчать об этом нельзя: иначе протянутое
    # значение прочитается как решение оператора.
    if abs(rate - today_rate) < 0.005:
        lines.append("")
        lines.append("ℹ️ Совпадение с сегодняшним курсом значит одно из двух: Пегас "
                     "оставил курс прежним или ещё не выставил новый. По API это "
                     "не различить, окончательный курс покажет утреннее сообщение.")

    lines.append("")
    lines.append(f'<a href="{PAGE}">Курс на сайте Пегаса</a>')
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
    p.add_argument("--preview", action="store_true",
                   help="курс на завтра вместо курса на сегодня")
    p.add_argument("--force", action="store_true",
                   help="отправить, даже если сообщение за сегодня уже уходило")
    args = p.parse_args()

    today = dt.datetime.now(TZ).date()

    # Запусков за ночь несколько, сообщение нужно одно. Дошедший первым уже всё
    # отправил и записал историю — остальным делать нечего.
    if not args.preview and not args.force and load_sent() == today:
        print(f"Сообщение за {today.isoformat()} уже отправлено")
        return

    history = load_history()
    added, revised = sync_history(history, today)
    if added or revised:
        save_history(history)
    if added:
        print(f"Добавлено дней: {len(added)} ({', '.join(d.isoformat() for d in added)})")
    for day, was, now in revised:
        print(f"Пересмотрен курс за {day.isoformat()}: {fmt(was)} → {fmt(now)}")
    if not added and not revised:
        print("Изменений нет")

    if not history:
        raise SystemExit("История пуста — Пегас не отдал ни одной даты")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    repo_url = f"https://github.com/{repo}" if repo else ""

    if args.preview:
        tomorrow = today + dt.timedelta(days=1)
        rate = fetch_rate(tomorrow)
        if rate is None:
            raise SystemExit("Пегас не отдал курс на завтра")
        today_rate = history.get(today, history[max(history)])
        text = build_preview_message(tomorrow, rate, today_rate, repo_url)
    else:
        text = build_message(history, today, repo_url, revised)

    if args.dry_run:
        print("\n--- сообщение ---")
        print(text)
        return

    send(text, os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"])
    print("Отправлено")

    # Отметку ставим только после успешной отправки: упади мы на Telegram, следующий
    # запуск должен попробовать снова, а не решить, что за сегодня уже всё сделано.
    if not args.preview:
        save_sent(today)


if __name__ == "__main__":
    main()
