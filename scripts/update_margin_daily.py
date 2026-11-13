#!/usr/bin/env python3
"""
update_margin_daily.py

Ежедневная лёгкая проверка: тянет http://www.cmegroup.com/CmeWS/mvc/Margins/OUTRIGHT.csv,
берёт текущую (ближайший контракт) maintenance margin по 6 валютам, сравнивает
с последней сохранённой строкой в margin_<ticker>.csv и, если значение изменилось,
дописывает новую строку (Non-HRP + HRP, синтезированные по формуле CME:
Non-HRP = maintenance, HRP = round(maintenance * 1.10)).

Рассчитан на запуск в GitHub Actions на ветке data-storage, где лежат сами CSV.

Запуск:
    python update_margin_daily.py --datadir .
"""

import argparse
import csv
import os
import sys
from datetime import date, datetime, timezone

import requests

OUTRIGHT_URL = "http://www.cmegroup.com/CmeWS/mvc/Margins/OUTRIGHT.csv?sortField=exchange&sortAsc=true"

# Код продукта -> тикер, ИМЕННО для ежедневного OUTRIGHT.csv
# (отличается от кодов в архивных Advisory Notices для CAD/JPY!)
DAILY_CODE_TO_TICKER = {
    "AD": "6A",   # AUSTRALIAN DOLLAR FUTURES
    "BP": "6B",   # BRITISH POUND FUTURES
    "C1": "6C",   # CANADIAN DOLLAR FUTURES
    "EC": "6E",   # EURO FUTURE
    "J1": "6J",   # JAPANESE YEN FUTURES
    "NE": "6N",   # NEW ZEALAND FUTURES
    # Металлы (GC, SI) намеренно НЕ подключены: маржа в % и в долларах плавает по месяцам.
    # CL (WTI) и BZ (Brent) НЕ публикуются через этот конкретный фид —
    # проверено дважды на свежих выгрузках. Нужен другой источник для
    # ежедневного отслеживания нефти; историю по ним даёт cme_margin_history.py.
}

FIELDNAMES = ["effective_date", "rate_type", "change", "iso",
              "current_initial", "current_maintenance",
              "new_initial", "new_maintenance", "source_url"]


def fetch_daily_maintenance(session):
    """Возвращает {ticker: (maintenance:int, iso:str)} для ближайшего контракта каждой валюты."""
    resp = session.get(OUTRIGHT_URL, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig")

    reader = csv.DictReader(text.splitlines())
    best_by_ticker = {}  # ticker -> (start_period_sort_key, maintenance, iso)

    for row in reader:
        code = row.get("Product Code", "").strip()
        ticker = DAILY_CODE_TO_TICKER.get(code)
        if not ticker:
            continue
        try:
            maint = int(float(row["Maintenance"].replace(",", "")))
        except (ValueError, KeyError):
            continue
        iso = row.get("Currency", "USD").strip()

        # "Start Period" вида "09/2026" -> ключ сортировки для поиска ближайшего контракта
        start = row.get("Start Period", "").strip()
        try:
            mm, yyyy = start.split("/")
            sort_key = (int(yyyy), int(mm))
        except ValueError:
            sort_key = (9999, 12)

        if ticker not in best_by_ticker or sort_key < best_by_ticker[ticker][0]:
            best_by_ticker[ticker] = (sort_key, maint, iso)

    return {t: (v[1], v[2]) for t, v in best_by_ticker.items()}


def read_last_maintenance(path):
    """Последнее значение maintenance margin (по строке Non-HRP) из существующего CSV."""
    if not os.path.exists(path):
        return None
    last_non_hrp = None
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["rate_type"] == "Non-HRP":
                last_non_hrp = row
    if last_non_hrp is None:
        return None
    return int(last_non_hrp["new_maintenance"])


def append_rows(path, effective_date, iso, old_maint, new_maint, source_url):
    change = "Increase" if new_maint > old_maint else "Decrease"
    non_hrp_init_old, non_hrp_init_new = old_maint, new_maint
    hrp_init_old = round(old_maint * 1.10)
    hrp_init_new = round(new_maint * 1.10)

    rows = [
        {
            "effective_date": effective_date, "rate_type": "HRP", "change": change, "iso": iso,
            "current_initial": hrp_init_old, "current_maintenance": old_maint,
            "new_initial": hrp_init_new, "new_maintenance": new_maint, "source_url": source_url,
        },
        {
            "effective_date": effective_date, "rate_type": "Non-HRP", "change": change, "iso": iso,
            "current_initial": non_hrp_init_old, "current_maintenance": old_maint,
            "new_initial": non_hrp_init_new, "new_maintenance": new_maint, "source_url": source_url,
        },
    ]

    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--datadir", default=".", help="Папка с margin_<ticker>.csv и last_update_margin_<ticker>.txt")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 margin-daily-sync/1.0"})

    today = date.today().isoformat()
    changed_any = False

    try:
        daily = fetch_daily_maintenance(session)
    except Exception as e:
        print(f"ОШИБКА: не удалось скачать/распарсить OUTRIGHT.csv: {e}", file=sys.stderr)
        sys.exit(1)

    if not daily:
        print("ОШИБКА: в OUTRIGHT.csv не нашлось ни одного из наших 6 тикеров — "
              "похоже, CME опять поменяла коды продуктов.", file=sys.stderr)
        sys.exit(1)

    for ticker in sorted(set(DAILY_CODE_TO_TICKER.values())):
        csv_path = os.path.join(args.datadir, f"margin_{ticker}.csv")
        last_update_path = os.path.join(args.datadir, f"last_update_margin_{ticker}.txt")

        if ticker not in daily:
            print(f"[{ticker}] нет в сегодняшнем OUTRIGHT.csv — пропускаю")
            continue

        new_maint, iso = daily[ticker]
        old_maint = read_last_maintenance(csv_path)

        if old_maint is None:
            print(f"[{ticker}] в {csv_path} ещё нет истории (Non-HRP) — пропускаю, "
                  f"нужен первый исторический прогон")
            continue

        if new_maint == old_maint:
            print(f"[{ticker}] без изменений ({old_maint})")
        else:
            append_rows(csv_path, today, iso, old_maint, new_maint, OUTRIGHT_URL)
            print(f"[{ticker}] ИЗМЕНЕНИЕ: {old_maint} -> {new_maint}, записано в {csv_path}")
            changed_any = True

        # last_update = момент последнего ИЗМЕНЕНИЯ маржи по этому тикеру.
        # Пишем его только при изменении, либо если файла ещё нет (первый запуск) —
        # иначе эксперт видел бы "обновление" каждый день без реальных изменений.
        created_now = not os.path.exists(last_update_path)
        if new_maint != old_maint or created_now:
            with open(last_update_path, "w", encoding="utf-8") as f:
                f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
            if created_now:
                print(f"[{ticker}] создан {os.path.basename(last_update_path)}")
                changed_any = True   # чтобы workflow закоммитил новый файл

    # Для workflow: сигнализируем через файл, коммитить что-то или нет
    marker_path = os.path.join(args.datadir, ".margin_changed")
    if changed_any:
        with open(marker_path, "w") as f:
            f.write("1")
    elif os.path.exists(marker_path):
        os.remove(marker_path)


if __name__ == "__main__":
    main()
