import csv
from datetime import date, datetime, timedelta
import html
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

# =====================================================================
#                        KONFIGURACJA
# =====================================================================

DAYS_FORWARD_SEARCH = 120
PARALLEL_WORKERS = 5  # Bezpieczna liczba wątków chroniąca przed banem WAF
TARGET_PROMO_PRICE = 50.00

CSV_SANOK_WROCLAW = "ceny_sanok_wroclaw.csv"
CSV_WROCLAW_SANOK = "ceny_wroclaw_sanok.csv"
HORIZON_FILE = "chronos_boundary.txt"
README_FILE = "README.md"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

STOPS = {
    "sanok": {"id": "17", "name": "SANOK D.A. Lipińskiego"},
    "wroclaw": {"id": "77", "name": "WROCŁAW PKS Polbus"}
}

GATEWAY_ENDPOINT = "https://neobus.pl/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pl,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://neobus.pl",
    "Referer": "https://neobus.pl/",
}

_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=5, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        try:
            s.get(GATEWAY_ENDPOINT, headers=HEADERS, timeout=8)
        except Exception:
            pass
        _thread_local.session = s
    return _thread_local.session


def normalize_time(t: str) -> str:
    return re.sub(r'[-]', ':', t.strip())


def query_roundtrip_neobus(date_str: str, passengers: int = 1) -> tuple:
    """Wysyła jedno zapytanie Tam+Powrót dla danej liczby pasażerów."""
    session = get_session()
    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": "normal",
        "initial_stop": STOPS["sanok"]["id"],
        "final_stop": STOPS["wroclaw"]["id"],
        "passengers": str(passengers),
        "date_there": date_str,
        "date_return": date_str,
        "initial_stop_name": STOPS["sanok"]["name"],
        "final_stop_name": STOPS["wroclaw"]["name"],
    }
    try:
        r = session.post(GATEWAY_ENDPOINT, data=payload, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return {}, {}

        try:
            raw = r.json()
        except Exception:
            raw = json.loads(r.text)

        content = raw.get("neotickets", raw) if isinstance(raw, dict) else raw
        data = json.loads(content) if isinstance(content, str) else content

        there_dict = {}
        back_dict = {}

        # 1. Parsowanie ga4_data (Tam = indeks 0, Powrót = indeks 1)
        if isinstance(data, dict) and "ga4_data" in data and len(data["ga4_data"]) > 0:
            for idx, section in enumerate(data["ga4_data"]):
                target = there_dict if idx == 0 else back_dict
                for it in section.get("items", []):
                    name = it.get("item_name", "")
                    p_val = it.get("price") or it.get("discount", 0.0)
                    try:
                        price = float(p_val)
                    except (ValueError, TypeError):
                        price = 0.0

                    m = re.search(r"(\d{2}[-:]\d{2})\s*-\s*(\d{2}[-:]\d{2})", name)
                    if m:
                        dep = normalize_time(m.group(1))
                        arr = normalize_time(m.group(2))
                        h_str = f"{dep} -> {arr}"
                    else:
                        dep = name
                        h_str = name

                    if price > 0:
                        target[dep] = {"hours": h_str, "price": price}

        # 2. Awaryjny fallback na HTML
        if not there_dict and isinstance(data, dict) and "html" in data:
            raw_html = html.unescape(data["html"])
            blocks = re.findall(r'(\d{2}:\d{2})\s*(?:-|–|&ndash;)\s*(\d{2}:\d{2}).*?([\d\s]+[.,]\d{2})\s*zł', raw_html, re.DOTALL)
            for dep, arr, p_str in blocks:
                clean_p = float(p_str.replace(" ", "").replace(",", "."))
                there_dict[dep] = {"hours": f"{dep} -> {arr}", "price": clean_p}

        return there_dict, back_dict
    except Exception as e:
        return {}, {}


def scan_single_day_schedule(date_str: str) -> tuple:
    """Etap 1: Pobiera siatkę połączeń dla 1 pasażera."""
    time.sleep(0.05)
    there, back = query_roundtrip_neobus(date_str, passengers=1)
    
    res_there = []
    for dep, data in there.items():
        res_there.append({
            "route": "Sanok ➔ Wrocław", "date": date_str, "hours": data["hours"],
            "departure": dep, "price": data["price"], "seats": 1
        })
        
    res_back = []
    for dep, data in back.items():
        res_back.append({
            "route": "Wrocław ➔ Sanok", "date": date_str, "hours": data["hours"],
            "departure": dep, "price": data["price"], "seats": 1
        })
        
    return date_str, res_there, res_back


def resolve_seats_for_active_day(day_data: tuple) -> tuple:
    """Etap 2: Bada binarnie wolne miejsca dla dnia, w którym faktycznie są kursy."""
    date_str, courses_there, courses_back = day_data
    if not courses_there and not courses_back:
        return date_str, [], []

    seats_there = {c["departure"]: 1 for c in courses_there}
    seats_back = {c["departure"]: 1 for c in courses_back}

    time.sleep(0.05)
    # Check 65 miejsc
    t65, b65 = query_roundtrip_neobus(date_str, passengers=65)
    
    # Check 90 miejsc (piętrowe)
    t90, b90 = query_roundtrip_neobus(date_str, passengers=90)
    for dep in list(seats_there.keys()):
        if dep in t90:
            seats_there[dep] = 90
        elif dep in t65:
            seats_there[dep] = 65

    for dep in list(seats_back.keys()):
        if dep in b90:
            seats_back[dep] = 90
        elif dep in b65:
            seats_back[dep] = 65

    # Wyszukiwanie binarne dla kursów poniżej 65 miejsc
    unresolved_there = [dep for dep, val in seats_there.items() if val < 65]
    unresolved_back = [dep for dep, val in seats_back.items() if val < 65]

    if unresolved_there or unresolved_back:
        low, high = 2, 64
        while low <= high:
            mid = (low + high) // 2
            t_mid, b_mid = query_roundtrip_neobus(date_str, passengers=mid)

            for dep in unresolved_there:
                if dep in t_mid:
                    seats_there[dep] = max(seats_there[dep], mid)

            for dep in unresolved_back:
                if dep in b_mid:
                    seats_back[dep] = max(seats_back[dep], mid)

            any_available = any(dep in t_mid for dep in unresolved_there) or any(dep in b_mid for dep in unresolved_back)
            if any_available:
                low = mid + 1
            else:
                high = mid - 1
            time.sleep(0.05)

    for c in courses_there:
        c["seats"] = seats_there.get(c["departure"], 1)

    for c in courses_back:
        c["seats"] = seats_back.get(c["departure"], 1)

    return date_str, courses_there, courses_back


def generate_dates(days_count: int) -> list:
    today = date.today()
    return [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(days_count)]


def load_previous_snapshot(csv_file: str) -> dict:
    prev = {}
    if not os.path.isfile(csv_file):
        return prev
    try:
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("Data kursu"), row.get("Godzina kursu"))
                try:
                    price = float(row.get("Cena (PLN)", 0))
                    seats = row.get("Wolne miejsca", "B/D")
                    prev[key] = {
                        "price": price,
                        "seats": int(seats) if str(seats).isdigit() else None
                    }
                except Exception:
                    pass
    except Exception:
        pass
    return prev


def update_database(courses: list, csv_file: str):
    if not courses:
        return
    file_exists = os.path.isfile(csv_file)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for c in courses:
        rows.append([
            ts,
            c["date"],
            c["hours"],
            f"{c['price']:.2f}",
            str(c.get("seats", "B/D"))
        ])

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["Data pomiaru", "Data kursu", "Godzina kursu", "Cena (PLN)", "Wolne miejsca"])
        w.writerows(rows)
    print(f"💾 [{csv_file}] Zapisano {len(rows)} rekordów.", flush=True)


def compute_deltas(current_courses: list, prev_dict: dict) -> list:
    deltas = []
    for c in current_courses:
        key = (c["date"], c["hours"])
        prev = prev_dict.get(key)
        if not prev:
            continue

        curr_seats = c.get("seats") if isinstance(c.get("seats"), int) else None
        prev_seats = prev.get("seats")
        curr_price = c["price"]
        prev_price = prev.get("price", 0.0)

        price_diff = round(curr_price - prev_price, 2)
        seats_diff = (curr_seats - prev_seats) if (curr_seats is not None and prev_seats is not None) else 0

        if abs(price_diff) >= 0.01 or seats_diff != 0:
            deltas.append({
                "route": c["route"],
                "date": c["date"],
                "hours": c["hours"],
                "curr_price": curr_price,
                "price_diff": price_diff,
                "curr_seats": curr_seats,
                "seats_diff": seats_diff
            })
    return deltas


def render_bar(seats, total: int = 65) -> str:
    if not isinstance(seats, int) or seats < 0:
        return "B/D"
    filled = max(0, min(10, int(round((seats / total) * 10))))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {seats}/{total}"


def build_readme(courses_san_wro: list, courses_wro_san: list, deltas: list):
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 📡 **Zakres:** 120+ dni rozkładowych\n\n",
        "## 🚨 1. Dziennik Zmian (Względem poprzedniego pomiaru)\n\n"
    ]

    if deltas:
        md.append("| Trasa | Data | Kurs | Aktualna Cena | Zmiana Ceny (Δ) | Wolne Miejsca | Zmiana Miejsc (Δ) |\n")
        md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: |\n")
        for d in deltas:
            p_delta = f"🟢 `{d['price_diff']:+.2f} zł`" if d['price_diff'] < 0 else (f"🔴 `{d['price_diff']:+.2f} zł`" if d['price_diff'] > 0 else "0.00 zł")
            s_delta = f"📉 `{d['seats_diff']:+d} miejsc` (Wykupiono)" if d['seats_diff'] < 0 else (f"📈 `{d['seats_diff']:+d} miejsc` (Zwrot)" if d['seats_diff'] > 0 else "Bez zmian")
            s_str = f"{d['curr_seats']}" if d['curr_seats'] is not None else "B/D"
            md.append(f"| {d['route']} | 📅 **{d['date']}** | ⏰ {d['hours']} | **{d['curr_price']:.2f} zł** | {p_delta} | `{s_str}` | {s_delta} |\n")
    else:
        md.append("> ℹ️ Brak zmian cen i zajętości miejsc od ostatniego cyklu pomiarowego.\n")

    md.extend([
        "\n---\n\n",
        "## 🗺️ 2. Mapy Obłożenia i Dostępności Miejsc (Heatmapy)\n\n",
        "### 🚌 Trasa: Sanok ➔ Wrocław\n\n",
        "![Heatmapa Sanok -> Wrocław](heatmapa_sanok_wroclaw.png)\n\n",
        "### 🚌 Trasa: Wrocław ➔ Sanok\n\n",
        "![Heatmapa Wrocław -> Sanok](heatmapa_wroclaw_sanok.png)\n\n",
        "---\n\n",
        "## 📋 3. Pełny Rozkład i Dostępność Kursów\n\n",
        "### 📍 Sanok ➔ Wrocław\n\n",
        "| Data | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_san_wro:
        s_val = c.get("seats", "B/D")
        cap = 90 if isinstance(s_val, int) and s_val > 65 else 65
        bar = render_bar(s_val, cap)
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n### 📍 Wrocław ➔ Sanok\n\n",
        "| Data | Kurs | Wolne miejsca | Cena | Status |\n",
        "| :--- | :---: | :---: | :---: | :---: |\n"
    ])

    for c in courses_wro_san:
        s_val = c.get("seats", "B/D")
        cap = 90 if isinstance(s_val, int) and s_val > 65 else 65
        bar = render_bar(s_val, cap)
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        md.append(f"| 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)
    print("📄 Zaktualizowano README.md.", flush=True)


def check_and_notify_horizon(active_dates: list):
    if not active_dates:
        return
    dt_dates = sorted([time.strptime(d, "%d.%m.%Y") for d in active_dates])
    furthest = time.strftime("%d.%m.%Y", dt_dates[-1])
    prev = ""
    if os.path.isfile(HORIZON_FILE):
        with open(HORIZON_FILE, "r", encoding="utf-8") as f:
            prev = f.read().strip()

    if not prev:
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)
        return

    if time.strptime(furthest, "%d.%m.%Y") > time.strptime(prev, "%d.%m.%Y"):
        msg = (
            f"📢 **OTWARTO NOWĄ PULĘ BILETÓW!** @everyone\n\n"
            f"📅 Sprzedaż wydłużona do: **{furthest}** (wcześniej: {prev})\n"
            f"🚀 Sprawdź promocyjne bilety na https://neobus.pl/"
        )
        if DISCORD_WEBHOOK_URL:
            try:
                requests.post(DISCORD_WEBHOOK_URL, json={"username": "Sentinel Radar", "content": msg}, timeout=8)
            except Exception:
                pass
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)


def main():
    start_t = time.time()
    print("==========================================================", flush=True)
    print("🚀 SENTINEL N3: SANOK ⇄ WROCŁAW (120+ DNI ROUNDTRIP)", flush=True)
    print("==========================================================", flush=True)

    prev_san_wro = load_previous_snapshot(CSV_SANOK_WROCLAW)
    prev_wro_san = load_previous_snapshot(CSV_WROCLAW_SANOK)

    dates = generate_dates(DAYS_FORWARD_SEARCH)
    total_days = len(dates)

    # ETAP 1: Szybki skan kalendarza (1 zapytanie = cały dzień w obie strony)
    print(f"\n📡 [ETAP 1/2] Skanowanie kalendarza połączeń ({total_days} dni)...", flush=True)
    active_days_data = []
    done_scan = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(scan_single_day_schedule, d) for d in dates]
        for fut in as_completed(futures):
            day_str, res_sw, res_ws = fut.result()
            done_scan += 1
            if res_sw or res_ws:
                active_days_data.append((day_str, res_sw, res_ws))
            if done_scan % 15 == 0 or done_scan == total_days:
                pct = (done_scan / total_days) * 100
                print(f"  [📅 {done_scan:03d}/{total_days} | {pct:5.1f}%] Przeskanowano horyzont (Aktywnych dni ze sprzedażą: {len(active_days_data)})...", flush=True)

    total_active = len(active_days_data)
    print(f"\n✅ Wykryto {total_active} dni z aktywną sprzedażą biletów.", flush=True)

    # ETAP 2: Dokładne badanie wolnych miejsc tylko dla dni z aktywnymi kursami
    courses_san_wro = []
    courses_wro_san = []

    print(f"\n🔬 [ETAP 2/2] Binarne badanie miejsc dla {total_active} aktywnych dni...", flush=True)
    done_eval = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(resolve_seats_for_active_day, item) for item in active_days_data]
        for fut in as_completed(futures):
            day_str, res_sw, res_ws = fut.result()
            courses_san_wro.extend(res_sw)
            courses_wro_san.extend(res_ws)
            done_eval += 1
            pct = (done_eval / total_active) * 100
            print(f"  [💺 {done_eval:02d}/{total_active} | {pct:5.1f}%] Data: {day_str} ➔ Zbadano {len(res_sw) + len(res_ws)} kursów w obie strony", flush=True)

    all_courses = courses_san_wro + courses_wro_san
    total_courses = len(all_courses)
    print(f"\n🚀 Zakończono pomiary. Zbadano łącznie {total_courses} kursów.", flush=True)

    all_dates = sorted(list({c["date"] for c in all_courses}), key=lambda x: datetime.strptime(x, "%d.%m.%Y"))
    check_and_notify_horizon(all_dates)

    # Sortowanie chronologiczne
    courses_san_wro.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))
    courses_wro_san.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))

    # Wyliczanie zmian względem poprzedniego pomiaru
    deltas = compute_deltas(courses_san_wro, prev_san_wro) + compute_deltas(courses_wro_san, prev_wro_san)

    # Zapis danych
    print("\n💾 Zapisywanie baz CSV i generowanie README.md...", flush=True)
    update_database(courses_san_wro, CSV_SANOK_WROCLAW)
    update_database(courses_wro_san, CSV_WROCLAW_SANOK)
    build_readme(courses_san_wro, courses_wro_san, deltas)

    elapsed = time.time() - start_t
    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO POMYŚLNIE W CZASIE: {elapsed:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
