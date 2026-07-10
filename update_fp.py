import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import os
import time
import glob

# ---------- Конфигурация ----------
PAIRS = {
    "6E": "EURUSD=X",
    "6B": "GBPUSD=X",
    "6A": "AUDUSD=X",
    "6N": "NZDUSD=X",
    "6C": "USDCAD=X",
    "6J": "USDJPY=X"
}
OVERLAP_HOURS = 72          # запас для перекрытия выходных и задержек
DELTA_ROWS = 72             # количество строк в дельте
MAX_RETRIES = 3

# ---------- Вспомогательные функции ----------
def load_existing_year_data(ticker, year):
    """Загружает существующий годовой файл, если он есть."""
    filename = f"{ticker}_{year}_hourly.csv"
    try:
        df = pd.read_csv(filename, parse_dates=['time'])
        return df
    except FileNotFoundError:
        return pd.DataFrame()
    except Exception as e:
        print(f"Ошибка чтения {filename}: {e}")
        return pd.DataFrame()

def save_year_data(ticker, year, df):
    """Сохраняет годовой файл."""
    filename = f"{ticker}_{year}_hourly.csv"
    df.to_csv(filename, index=False)
    print(f"  Сохранён {filename}, записей: {len(df)}")

def get_last_time_from_df(df):
    """Возвращает время последней записи из DataFrame."""
    if df.empty:
        return None
    return df['time'].iloc[-1]

def fetch_new_data(ticker, start_date, end_date):
    """Скачивает данные с Yahoo за указанный период."""
    for attempt in range(1, MAX_RETRIES+1):
        print(f"    Попытка {attempt} скачать {ticker} с {start_date} по {end_date} ...", end="")
        try:
            fut = yf.download(ticker + "=F", start=start_date, end=end_date, interval="1h", progress=False)
            fx  = yf.download(PAIRS[ticker], start=start_date, end=end_date, interval="1h", progress=False)
            if fut is None or fx is None:
                print("  запрос вернул None")
                continue
            if fut.empty and fx.empty:
                print("  оба DataFrame пустые")
                continue
            if fut.empty:
                print(f"  фьючерс пустой ({len(fut)})")
                continue
            if fx.empty:
                print(f"  спот пустой ({len(fx)})")
                continue
            print(f"  OK: фьючерс {len(fut)}, спот {len(fx)}")
            return fut, fx
        except Exception as e:
            print(f"  ошибка: {e}")
        time.sleep(5)
    print(f"  Не удалось загрузить данные для {ticker} после {MAX_RETRIES} попыток.")
    return None, None

def merge_and_deduplicate(old_df, new_df):
    """Объединяет старые и новые данные, удаляет дубли по времени."""
    if old_df.empty:
        return new_df
    combined = pd.concat([old_df, new_df], ignore_index=True)
    combined.drop_duplicates(subset=['time'], keep='last', inplace=True)
    combined.sort_values('time', inplace=True)
    return combined

def build_dataframe(ticker, fut, fx):
    """Из сырых данных Yahoo строит итоговый DataFrame с колонками."""
    fut = fut[['Open','High','Low','Close','Volume']].copy()
    fut.columns = ['futOpen','futHigh','futLow','futClose','futVolume']
    fx = fx[['Open','High','Low','Close','Volume']].copy()
    fx.columns = ['spotOpen','spotHigh','spotLow','spotClose','spotVolume']

    merged = pd.merge(fut, fx, left_index=True, right_index=True, how='inner')
    if merged.empty:
        return pd.DataFrame()

    merged.sort_index(inplace=True)

    if ticker == "6J":
        merged['fp']   = (1.0 / merged['futClose']) - merged['spotClose']
        merged['hiFP'] = (1.0 / merged['futLow'])   - merged['spotHigh']
        merged['loFP'] = (1.0 / merged['futHigh'])  - merged['spotLow']
    else:
        merged['fp']   = merged['futClose'] - merged['spotClose']
        merged['hiFP'] = merged['futHigh']  - merged['spotLow']
        merged['loFP'] = merged['futLow']   - merged['spotHigh']

    out = pd.DataFrame({
        'time':       merged.index.tz_localize(None),  # убираем UTC
        'futOpen':    merged['futOpen'].round(6),
        'futHigh':    merged['futHigh'].round(6),
        'futLow':     merged['futLow'].round(6),
        'futClose':   merged['futClose'].round(6),
        'futVolume':  merged['futVolume'].round(0).astype(int),
        'spotOpen':   merged['spotOpen'].round(6),
        'spotHigh':   merged['spotHigh'].round(6),
        'spotLow':    merged['spotLow'].round(6),
        'spotClose':  merged['spotClose'].round(6),
        'spotVolume': merged['spotVolume'].round(0).astype(int),
        'hiFP':       merged['hiFP'].round(6),
        'loFP':       merged['loFP'].round(6),
        'fp':         merged['fp'].round(6)
    })
    return out

def update_manifest(ticker):
    """Обновляет manifest.txt: собирает все годовые файлы для тикера."""
    pattern = f"{ticker}_*_hourly.csv"
    files = glob.glob(pattern)
    years = []
    for f in files:
        parts = f.split('_')
        if len(parts) >= 2:
            year_str = parts[1]
            if year_str.isdigit():
                years.append(int(year_str))
    if years:
        years.sort()
        manifest = {}
        if os.path.exists("manifest.txt"):
            with open("manifest.txt", "r") as mf:
                for line in mf:
                    line = line.strip()
                    if line:
                        parts_line = line.split(',')
                        if len(parts_line) >= 2:
                            tick = parts_line[0]
                            yrs = [int(y) for y in parts_line[1:] if y.isdigit()]
                            manifest[tick] = yrs
        manifest[ticker] = years
        with open("manifest.txt", "w") as mf:
            for tick, yrs in manifest.items():
                mf.write(f"{tick},{','.join(map(str, sorted(yrs)))}\n")
        print(f"  Манифест обновлён для {ticker}: {years}")

def process_ticker(ticker, forex):
    """Обновляет данные для одного тикера."""
    print(f"\n=== {ticker} ({forex}) ===")

    now = datetime.now()
    current_year = now.year

    df_current = load_existing_year_data(ticker, current_year)
    last_time_raw = get_last_time_from_df(df_current)

    if last_time_raw is None:
        print(f"  Файл за {current_year} отсутствует. Загружаем последние 730 дней...")
        start_date = now - timedelta(days=730)
        end_date = now
        fut, fx = fetch_new_data(ticker, start_date, end_date)
        if fut is None:
            print(f"  Не удалось загрузить данные для {ticker}")
            return
        new_df = build_dataframe(ticker, fut, fx)
        if new_df.empty:
            print(f"  Нет данных для {ticker}")
            return
        new_df['year'] = new_df['time'].dt.year
        df_current_year = new_df[new_df['year'] == current_year].copy().drop(columns=['year'])
        if not df_current_year.empty:
            save_year_data(ticker, current_year, df_current_year)
            df_current = df_current_year
            last_time = get_last_time_from_df(df_current)
        else:
            print(f"  Данные за {current_year} не получены.")
            return
    else:
        last_time = last_time_raw
        print(f"  Последняя запись: {last_time} (тип: {type(last_time)})")
        df_current = df_current

    start_fetch = last_time - timedelta(hours=OVERLAP_HOURS)
    end_fetch = now - timedelta(hours=1)
    if start_fetch >= end_fetch:
        print("  Данные актуальны, обновление не требуется.")
    else:
        print(f"  Скачиваем новые данные с {start_fetch} по {end_fetch} ...")
        fut, fx = fetch_new_data(ticker, start_fetch, end_fetch)
        if fut is None:
            print("  Не удалось загрузить новые данные.")
        else:
            new_df = build_dataframe(ticker, fut, fx)
            print(f"  build_dataframe вернул {len(new_df)} строк")
            if new_df.empty:
                print("  Получен пустой DataFrame.")
            else:
                print(f"  Диапазон времени в new_df: с {new_df['time'].min()} по {new_df['time'].max()}")
                new_rows = new_df[new_df['time'] > last_time]
                print(f"  Найдено {len(new_rows)} новых строк (time > {last_time})")
                if new_rows.empty:
                    print("  Новых данных нет.")
                else:
                    print(f"  Первая новая строка: {new_rows.iloc[0]['time']}, последняя: {new_rows.iloc[-1]['time']}")
                    updated_df = merge_and_deduplicate(df_current, new_rows)
                    save_year_data(ticker, current_year, updated_df)
                    df_current = updated_df
                    last_time = get_last_time_from_df(df_current)

    if not df_current.empty:
        delta = df_current.tail(DELTA_ROWS)
        delta_filename = f"delta_{ticker}.csv"
        delta.to_csv(delta_filename, index=False)
        print(f"  Дельта сохранена ({len(delta)} строк)")

        with open(f"last_update_{ticker}.txt", "w") as f:
            f.write(last_time.strftime('%Y.%m.%d %H:%M'))
        print(f"  last_update обновлён: {last_time}")

    update_manifest(ticker)

def main():
    print("=== Инкрементальное обновление данных ===")
    for ticker, forex in PAIRS.items():
        process_ticker(ticker, forex)
        time.sleep(5)
    print("\nГотово!")

if __name__ == "__main__":
    main()
