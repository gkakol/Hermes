import csv
from datetime import date, datetime, timedelta
import json
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

# =====================================================================
#                        KONFIGURACJA
# =====================================================================

MAX_LOOKAHEAD_DAYS = 120
PARALLEL_WORKERS = 4
MAX_CAPACITY = 70
TARGET_PROMO_PRICE = 50.00
SUPER_PROMO_PRICE = 35.00  # Próg natychmiastowego alertu cenowego

CSV_SANOK_WROCLAW = "ceny_sanok_wroclaw.csv"
CSV_WROCLAW_SANOK = "ceny_wroclaw_sanok.csv"
ARCHIVE_DIR = "archive"
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
        retries = Retry(total=3, backoff_factor=0.4, status_forcelist=[500, 502, 503, 504])
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


def query_api(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int = 1):
    session = get_session()
    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": "normal",
        "initial_stop": str(from_id),
        "final_stop": str(to_id),
        "passengers": str(passengers),
        "date_there": date_str,
        "date_return": "",
        "initial_stop_name": from_name,
        "final_stop_name": to_name,
    }
    for attempt in range(2):
        try:
            r = session.post(GATEWAY_ENDPOINT, data=payload, headers=HEADERS, timeout=7)
            if r.status_code != 200:
                time.sleep(0.15)
                continue

            raw = r.json() if hasattr(r, "json") else json.loads(r.text)
            content = raw.get("neotickets", raw) if isinstance(raw, dict) else raw
            data = json.loads(content) if isinstance(content, str) else content

            courses = {}
            if isinstance(data, dict) and "ga4_data" in data and len(data["ga4_data"]) > 0:
                for it in data["ga4_data"][0].get("items", []):
                    name = it.get("item_name", "")
                    price = float(it.get("price") or it.get("discount", 0.0) or 0.0)
                    m = re.search(r"(\d{2}[-:]\d{2})\s*-\s*(\d{2}[-:]\d{2})", name)
                    if m and price > 0:
                        courses[normalize_time(m.group(1))] = {
                            "hours": f"{normalize_time(m.group(1))} -> {normalize_time(m.group(2))}",
                            "price": price
                        }
            return courses
        except Exception:
            time.sleep(0.15)
    return None


def query_day_seats_map(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, passengers: int):
    data = query_api(from_id, from_name, to_id, to_name, date_str, passengers=passengers)
    if data is None:
        return None
    return set(data.keys())


def detect_sales_horizon(prev_furthest: str) -> tuple:
    """Dynamiczny horyzont: sprawdza kiedy faktycznie kończy się sprzedaż + bada sondy nowej puli."""
    today = date.today()
    if prev_furthest:
        try:
            anchor = datetime.strptime(prev_furthest, "%d.%m.%Y").date()
        except ValueError:
            anchor = today + timedelta(days=60)
    else:
        anchor = today + timedelta(days=60)

    # Sondy sprawdzające otwarcie nowej puli poza granicą (+1, +7, +14, +28 dni)
    detected_frontier = anchor
    for jump in [1, 7, 14, 28]:
        probe_d = anchor + timedelta(days=jump)
        if (probe_d - today).days > MAX_LOOKAHEAD_DAYS:
            break
        probe_str = probe_d.strftime("%d.%m.%Y")
        res = query_api(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], probe_str, 1)
        if res and len(res) > 0:
            detected_frontier = probe_d

    # Uściślenie brzegu sprzedaży (binary search)
    low = max(1, (anchor - today).days - 10)
    high = min(MAX_LOOKAHEAD_DAYS, (detected_frontier - today).days + 10)
    last_valid = anchor

    while low <= high:
        mid = (low + high) // 2
        d_mid = today + timedelta(days=mid)
        d_str = d_mid.strftime("%d.%m.%Y")
        res = query_api(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], d_str, 1)
        if res and len(res) > 0:
            last_valid = d_mid
            low = mid + 1
        else:
            high = mid - 1
        time.sleep(0.01)

    total_days = max(1, (last_valid - today).days + 1)
    return last_valid.strftime("%d.%m.%Y"), total_days


def resolve_course_adaptive(from_id, from_name, to_id, to_name, date_str, dep, prev_val: int) -> int:
    if prev_val and isinstance(prev_val, int) and 1 < prev_val <= MAX_CAPACITY:
        map_prev = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, prev_val)
        if map_prev is not None and dep in map_prev:
            if prev_val < MAX_CAPACITY:
                map_plus = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, min(MAX_CAPACITY, prev_val + 2))
                if map_plus is not None and dep in map_plus:
                    return min(MAX_CAPACITY, prev_val + 2)
            return prev_val
        else:
            low, high = 1, prev_val - 1
    else:
        map_50 = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, 50)
        if map_50 is not None and dep in map_50:
            map_max = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, MAX_CAPACITY)
            if map_max is not None and dep in map_max:
                return MAX_CAPACITY
            return 50
        low, high = 1, 49

    exact = 1
    while low <= high:
        mid = (low + high) // 2
        day_map = query_day_seats_map(from_id, from_name, to_id, to_name, date_str, mid)
        if day_map is None:
            return prev_val if (prev_val and isinstance(prev_val, int)) else 1
        if dep in day_map:
            exact = mid
            low = mid + 1
        else:
            high = mid - 1
        time.sleep(0.01)

    return exact


def process_day_unified(date_str: str, prev_sw: dict, prev_ws: dict) -> tuple:
    time.sleep(0.02)
    sw_raw = query_api(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, 1) or {}
    ws_raw = query_api(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, 1) or {}

    res_sw = []
    for dep, data in sw_raw.items():
        prev_s = prev_sw.get((date_str, dep), {}).get("seats")
        seats = resolve_course_adaptive(STOPS["sanok"]["id"], STOPS["sanok"]["name"], STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], date_str, dep, prev_s)
        res_sw.append({
            "route": "Sanok ➔ Wrocław", "date": date_str, "departure": dep, "hours": data["hours"],
            "price": data["price"], "seats": seats
        })

    res_ws = []
    for dep, data in ws_raw.items():
        prev_s = prev_ws.get((date_str, dep), {}).get("seats")
        seats = resolve_course_adaptive(STOPS["wroclaw"]["id"], STOPS["wroclaw"]["name"], STOPS["sanok"]["id"], STOPS["sanok"]["name"], date_str, dep, prev_s)
        res_ws.append({
            "route": "Wrocław ➔ Sanok", "date": date_str, "departure": dep, "hours": data["hours"],
            "price": data["price"], "seats": seats
        })

    return date_str, res_sw, res_ws


def load_previous_snapshot(csv_file: str) -> dict:
    prev = {}
    if not os.path.isfile(csv_file):
        return prev
    try:
        with open(csv_file, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row.get("Data kursu"), row.get("Godzina"))
                seats = row.get("Wolne miejsca", "B/D")
                ts_str = row.get("Data pomiaru", "")
                prev[key] = {
                    "price": float(row.get("Cena (PLN)", 0)),
                    "seats": int(seats) if str(seats).isdigit() else None,
                    "timestamp": ts_str
                }
    except Exception:
        pass
    return prev


def rotate_and_clean_database(csv_file: str, current_courses: list):
    """Rotacja bazy: przeszłe daty trafiają do folderu archive/, główny plik trzyma tylko przyszłe dane."""
    if not current_courses:
        return
    today = date.today()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    # Wczytujemy istniejące wiersze, jeśli plik istnieje
    all_rows = []
    if os.path.isfile(csv_file):
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                all_rows = list(csv.DictReader(f))
        except Exception:
            all_rows = []

    # Nowe rekordy z bieżącego pomiaru
    new_rows = [{
        "Data pomiaru": ts,
        "Data kursu": c["date"],
        "Godzina": c["departure"],
        "Cena (PLN)": f"{c['price']:.2f}",
        "Wolne miejsca": str(c.get("seats", "B/D"))
    } for c in current_courses]

    combined = all_rows + new_rows

    active_rows = []
    archive_rows = []

    for r in combined:
        try:
            d_kurs = datetime.strptime(r["Data kursu"], "%d.%m.%Y").date()
            if d_kurs >= today:
                active_rows.append(r)
            else:
                archive_rows.append(r)
        except Exception:
            active_rows.append(r)

    # Zapis do archiwum, jeśli są przeszłe rekordy
    if archive_rows:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        archive_file = os.path.join(ARCHIVE_DIR, f"arch_{os.path.basename(csv_file)}")
        file_exists = os.path.isfile(archive_file)
        with open(archive_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Data pomiaru", "Data kursu", "Godzina", "Cena (PLN)", "Wolne miejsca"])
            if not file_exists:
                writer.writeheader()
            writer.writerows(archive_rows)

    # Nadpisanie głównego pliku czystymi, aktualnymi danymi
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Data pomiaru", "Data kursu", "Godzina", "Cena (PLN)", "Wolne miejsca"])
        writer.writeheader()
        writer.writerows(active_rows)


def compute_deltas_and_velocity(current_courses: list, prev_dict: dict, check_ts: str) -> tuple:
    """Wylicza zmiany (deltas) oraz tempo sprzedaży (velocity)."""
    deltas = []
    alerts = []
    parts = check_ts.split(" ")
    date_part, time_part = parts[0], parts[1] if len(parts) > 1 else ""

    now_dt = datetime.strptime(check_ts, "%Y-%m-%d %H:%M:%S")

    for c in current_courses:
        prev = prev_dict.get((c["date"], c["departure"]))
        curr_seats = c.get("seats") if isinstance(c.get("seats"), int) else None
        curr_price = c["price"]

        # Wyliczanie tempa (velocity)
        velocity_str = "—"
        if prev and curr_seats is not None and prev.get("seats") is not None:
            prev_seats = prev.get("seats")
            prev_ts_str = prev.get("timestamp")
            if prev_ts_str:
                try:
                    prev_dt = datetime.strptime(prev_ts_str, "%Y-%m-%d %H:%M:%S")
                    hours_diff = max(0.1, (now_dt - prev_dt).total_seconds() / 3600.0)
                    seats_drop = prev_seats - curr_seats
                    if seats_drop > 0:
                        rate_24h = round((seats_drop / hours_diff) * 24)
                        velocity_str = f"🔥 `{rate_24h} os./24h`" if rate_24h >= 5 else f"⚡ `{rate_24h} os./24h`"
                    elif seats_drop < 0:
                        velocity_str = "🔄 `Zwrot`"
                    else:
                        velocity_str = "🟢 `Stabilnie`"
                except Exception:
                    pass

        c["velocity"] = velocity_str

        if not prev:
            continue

        prev_seats = prev.get("seats")
        price_diff = round(curr_price - prev.get("price", 0.0), 2)
        seats_diff = (curr_seats - prev_seats) if (curr_seats is not None and prev_seats is not None) else 0

        # Weryfikacja zdarzeń do alertu Discord
        # 1. Super Promo
        if curr_price <= SUPER_PROMO_PRICE and (prev.get("price", 999) > SUPER_PROMO_PRICE):
            alerts.append(f"🔥 **SUPER PROMO!** {c['route']} | {c['date']} {c['hours']} -> **{curr_price:.2f} zł**!")
        # 2. Spadek liczby miejsc na weekend poniżej 5
        try:
            d_weekday = datetime.strptime(c["date"], "%d.%m.%Y").weekday()
            if d_weekday in [4, 6] and curr_seats is not None and curr_seats <= 5 and (prev_seats or 99) > 5:
                alerts.append(f"⚠️ **KOŃCÓWKA NA WEEKEND!** {c['route']} | {c['date']} ({c['departure']}) -> Zostało tylko **{curr_seats} miejsc**!")
        except Exception:
            pass
        # 3. Zwrot biletów w pełnym kursie (Seat Drop)
        if prev_seats is not None and prev_seats <= 3 and curr_seats is not None and curr_seats > prev_seats:
            alerts.append(f"🎟️ **ZWROT BILETÓW!** Ktoś oddał bilet na {c['route']} | {c['date']} {c['departure']} (+{seats_diff} szt.)")

        if abs(price_diff) >= 0.01 or seats_diff != 0:
            deltas.append({
                "check_date": date_part, "check_time": time_part,
                "route": c["route"].replace("➔", "→"), "date": c["date"], "hours": c["hours"],
                "curr_price": curr_price, "price_diff": price_diff,
                "prev_seats": prev_seats, "curr_seats": curr_seats, "seats_diff": seats_diff
            })

    return deltas, alerts


def send_discord_alerts(alerts: list):
    if not DISCORD_WEBHOOK_URL or not alerts:
        return
    for chunk in [alerts[i:i + 5] for i in range(0, len(alerts), 5)]:
        body = "\n".join(chunk)
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"username": "Sentinel Radar", "content": body}, timeout=6)
        except Exception:
            pass


def render_bar(seats, total: int = MAX_CAPACITY) -> str:
    if not isinstance(seats, int):
        return "B/D"
    filled = max(0, min(10, int(round((seats / total) * 10))))
    return f"[{'█' * filled}{'░' * (10 - filled)}] {seats}/{total}"


def build_readme(all_courses: list, deltas: list, now_ts: str, horizon_str: str, active_days: int):
    today = date.today()
    future_courses = []
    for c in all_courses:
        try:
            d_dt = datetime.strptime(c["date"], "%d.%m.%Y").date()
            if d_dt >= today and isinstance(c.get("seats"), int):
                future_courses.append(c)
        except Exception:
            pass

    top_50 = sorted(future_courses, key=lambda x: (x["seats"], x["price"]))[:50]

    md = [
        "# 🚌 Sentinel N3: Sanok ⇄ Wrocław\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 📡 **Aktywny horyzont sprzedaży:** **`{horizon_str}`** ({active_days} dni)\n\n",
        "## 🚨 1. Dziennik Zmian (Względem poprzedniego pomiaru)\n\n",
        "> Poniżej prezentowane są różnice względem poprzedniego sprawdzenia (ubytek foteli lub obniżka ceny).\n\n"
    ]

    if deltas:
        md.append("| Data sprawdzenia | Trasa | Kurs | Zmiana ceny | Zmiana miejsc |\n")
        md.append("| :--- | :--- | :--- | :---: | :---: |\n")
        for d in deltas:
            col_ts = f"`{d['check_date']}`<br>`{d['check_time']}`"
            from_to = d['route'].split("→")
            col_route = f"{from_to[0].strip()} →<br>{from_to[1].strip()}" if len(from_to) == 2 else d['route']
            col_course = f"📅 {d['date']} ({d['hours']})"

            if abs(d['price_diff']) >= 0.01:
                p_delta = f"🟢 `{d['price_diff']:+.2f} zł`" if d['price_diff'] < 0 else f"🔴 `{d['price_diff']:+.2f} zł`"
                col_price = f"**{d['curr_price']:.2f} zł**<br>({p_delta})"
            else:
                col_price = f"{d['curr_price']:.2f} zł"

            p_s = d['prev_seats'] if d['prev_seats'] is not None else "?"
            c_s = d['curr_seats'] if d['curr_seats'] is not None else "?"
            diff_s = f"({d['seats_diff']:+d})" if d['seats_diff'] != 0 else "(0)"
            col_seats = f"{p_s} → **{c_s} szt.**<br>`{diff_s}`"

            md.append(f"| {col_ts} | {col_route} | {col_course} | {col_price} | {col_seats} |\n")
    else:
        md.append("> ℹ️ Brak zmian cen i dostępności miejsc od ostatniego cyklu pomiarowego.\n")

    md.extend([
        "\n---\n\n",
        "## 🔥 2. TOP 50 Najbardziej Obleganych Kursów (Końcówki biletów)\n\n",
        "> Zestawienie aktualnych kursów o najmniejszej liczbie wolnych miejsc wraz ze wskaźnikiem tempa wyprzedaży.\n\n",
        "| Poz. | Trasa | Data | Godzina | Wolne miejsca | Tempo wyprzedaży | Cena | Status |\n",
        "| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :---: |\n"
    ])

    for rank, c in enumerate(top_50, 1):
        bar = render_bar(c["seats"])
        r_tag = c["route"].replace("➔", "→")
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_PRICE else f"{c['price']:.2f} zł"
        alert_seats = f"🔴 **{c['seats']} szt.**" if c['seats'] <= 6 else f"**{c['seats']} szt.**"
        velo = c.get("velocity", "—")
        md.append(f"| {rank:02d} | {r_tag} | 📅 **{c['date']}** | ⏰ {c['hours']} | `{bar}` ({alert_seats}) | {velo} | {p_tag} | [Kup bilet](https://neobus.pl/) |\n")

    md.extend([
        "\n---\n\n",
        "## 🗺️ 3. Mapy Obłożenia (Heatmapy)\n\n",
        "### 🚌 Trasa: Sanok ➔ Wrocław\n\n",
        '<p align="center"><img src="heatmapa_sanok_wroclaw.png" alt="Heatmapa Sanok -> Wrocław" width="520"></p>\n\n',
        "### 🚌 Trasa: Wrocław ➔ Sanok\n\n",
        '<p align="center"><img src="heatmapa_wroclaw_sanok.png" alt="Heatmapa Wrocław -> Sanok" width="520"></p>\n\n'
    ])

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)


def check_and_notify_horizon(new_furthest: str):
    if not new_furthest:
        return
    prev = ""
    if os.path.isfile(HORIZON_FILE):
        with open(HORIZON_FILE, "r", encoding="utf-8") as f:
            prev = f.read().strip()

    if not prev:
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(new_furthest)
        return

    if datetime.strptime(new_furthest, "%d.%m.%Y") > datetime.strptime(prev, "%d.%m.%Y"):
        msg = f"📢 **OTWARTO NOWĄ PULĘ BILETÓW!** @everyone\n\n📅 Sprzedaż wydłużona do: **{new_furthest}** (wcześniej: {prev})\n🔗 Sprawdź promocje na: https://neobus.pl/"
        send_discord_alerts([msg])
        with open(HORIZON_FILE, "w", encoding="utf-8") as f:
            f.write(new_furthest)


def main():
    start_t = time.time()
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("==========================================================", flush=True)
    print(f"🚀 SENTINEL N3: VELOCITY ENGINE & SMART RADAR (CAP: {MAX_CAPACITY})", flush=True)
    print("==========================================================", flush=True)

    prev_horizon = ""
    if os.path.isfile(HORIZON_FILE):
        with open(HORIZON_FILE, "r", encoding="utf-8") as f:
            prev_horizon = f.read().strip()

    # Dynamiczne ustalenie horyzontu (bada sondami nową pulę)
    print("🔍 [1/3] Ustalanie aktywnego horyzontu sprzedaży...", flush=True)
    horizon_str, active_days = detect_sales_horizon(prev_horizon)
    print(f"✅ Horyzont: {horizon_str} ({active_days} dni do sprawdzenia)", flush=True)
    check_and_notify_horizon(horizon_str)

    prev_sw = load_previous_snapshot(CSV_SANOK_WROCLAW)
    prev_ws = load_previous_snapshot(CSV_WROCLAW_SANOK)

    dates = [(date.today() + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(active_days)]
    courses_sw, courses_ws = [], []

    print(f"\n📡 [2/3] Skanowanie i adaptacyjny pomiar foteli ({active_days} dni)...", flush=True)
    done_days = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(process_day_unified, d, prev_sw, prev_ws) for d in dates]
        for fut in as_completed(futures):
            day_str, res_sw, res_ws = fut.result()
            courses_sw.extend(res_sw)
            courses_ws.extend(res_ws)
            done_days += 1
            if done_days % 15 == 0 or done_days == active_days:
                print(f"  [⚡ {done_days:03d}/{active_days} | {(done_days/active_days)*100:5.1f}%] Przetworzono {day_str}...", flush=True)

    courses_sw.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))
    courses_ws.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["departure"]))
    all_active = courses_sw + courses_ws

    print("\n📊 [3/3] Obliczanie dynamiki (Velocity), rotacja CSV i alerty...", flush=True)
    deltas_sw, alerts_sw = compute_deltas_and_velocity(courses_sw, prev_sw, now_ts)
    deltas_ws, alerts_ws = compute_deltas_and_velocity(courses_ws, prev_ws, now_ts)
    all_deltas = deltas_sw + deltas_ws
    all_alerts = alerts_sw + alerts_ws

    # Wysłanie powiadomień Discord
    if all_alerts:
        send_discord_alerts(all_alerts)
        print(f"📢 Wysłano {len(all_alerts)} alertów na Discorda.", flush=True)

    # Rotacja baz CSV
    rotate_and_clean_database(CSV_SANOK_WROCLAW, courses_sw)
    rotate_and_clean_database(CSV_WROCLAW_SANOK, courses_ws)

    # Budowa odchudzonego README
    build_readme(all_active, all_deltas, now_ts, horizon_str, active_days)

    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO POMYŚLNIE W CZASIE: {time.time() - start_t:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
