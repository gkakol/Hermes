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

DAYS_AHEAD = 7
PARALLEL_WORKERS = 4
MAX_CAPACITY = 70

CSV_CORRIDOR = "corridor_segments_7d.csv"
REPORT_MD = "CORRIDOR.md"

# Kluczowe węzły magistrali N3 zgodnie z podanymi ID:
STOPS = {
    "SANOK": {"id": "17", "name": "SANOK D.A. Lipińskiego"},
    "NIEBYLEC": {"id": "19", "name": "NIEBYLEC"},
    "RZESZOW": {"id": "76", "name": "RZESZÓW D.A. PKS"},
    "KRAKOW_MDA": {"id": "71", "name": "KRAKÓW MDA"},
    "KRAKOW_AIRPORT": {"id": "72", "name": "KRAKÓW Lotnisko Balice"},
    "KATOWICE": {"id": "73", "name": "KATOWICE D.A. ul. Sądowa 5"},
    "GLIWICE": {"id": "123", "name": "GLIWICE Centrum Przesiadkowe ul. Składowa 8a"},
    "WROCLAW": {"id": "77", "name": "WROCŁAW PKS Polbus"}
}

# Kolejne segmenty w kierunku: Sanok -> Wrocław
SEGMENTS_EAST_WEST = [
    ("SANOK", "NIEBYLEC", "Sanok ➔ Niebylec"),
    ("NIEBYLEC", "RZESZOW", "Niebylec ➔ Rzeszów"),
    ("RZESZOW", "KRAKOW_MDA", "Rzeszów ➔ Kraków"),
    ("KRAKOW_MDA", "KRAKOW_AIRPORT", "Kraków ➔ Balice"),
    ("KRAKOW_AIRPORT", "KATOWICE", "Balice ➔ Katowice"),
    ("KATOWICE", "GLIWICE", "Katowice ➔ Gliwice"),
    ("GLIWICE", "WROCLAW", "Gliwice ➔ Wrocław"),
]

# Kolejne segmenty w kierunku powrotnym: Wrocław -> Sanok
SEGMENTS_WEST_EAST = [
    ("WROCLAW", "GLIWICE", "Wrocław ➔ Gliwice"),
    ("GLIWICE", "KATOWICE", "Gliwice ➔ Katowice"),
    ("KATOWICE", "KRAKOW_AIRPORT", "Katowice ➔ Balice"),
    ("KRAKOW_AIRPORT", "KRAKOW_MDA", "Balice ➔ Kraków"),
    ("KRAKOW_MDA", "RZESZOW", "Kraków ➔ Rzeszów"),
    ("RZESZOW", "NIEBYLEC", "Rzeszów ➔ Niebylec"),
    ("NIEBYLEC", "SANOK", "Niebylec ➔ Sanok"),
]

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
    for _ in range(2):
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


def probe_seats_fast(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, dep: str) -> int:
    """Szybkie badanie wolnych miejsc na konkretnym pododcinku."""
    # Szybki check standardowego sufitu (50 / 70)
    res_50 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=50)
    if res_50 and dep in res_50:
        res_70 = query_api(from_id, from_name, to_id, to_name, date_str, passengers=MAX_CAPACITY)
        return MAX_CAPACITY if (res_70 and dep in res_70) else 50

    # Binary search dla wąskiego zakresu 1..49
    low, high = 1, 49
    exact = 1
    while low <= high:
        mid = (low + high) // 2
        res = query_api(from_id, from_name, to_id, to_name, date_str, passengers=mid)
        if res is None:
            break
        if dep in res:
            exact = mid
            low = mid + 1
        else:
            high = mid - 1
        time.sleep(0.01)

    return exact


def analyze_course_segments(direction_name: str, segments: list, date_str: str, main_dep: str, main_hours: str) -> dict:
    """Bada wszystkie węzły na trasie pojedynczego kursu."""
    seg_results = {}
    min_seats = 999
    bottleneck_seg = ""

    for from_key, to_key, seg_label in segments:
        f_info = STOPS[from_key]
        t_info = STOPS[to_key]
        
        # Pobieramy ofertę dla tego odcinka
        seg_courses = query_api(f_info["id"], f_info["name"], t_info["id"], t_info["name"], date_str, passengers=1)
        if not seg_courses:
            seats = 0
        else:
            # Dopasowujemy kurs po przybliżonym oknie czasowym lub bierzemy pierwszy skojarzony
            # Na odcinku początkowym dopasowujemy po main_dep
            matched_dep = None
            if main_dep in seg_courses:
                matched_dep = main_dep
            else:
                # Jeśli to odcinek w środku trasy (np. Katowice -> Gliwice), bierzemy odpowiednik czasowy
                matched_dep = list(seg_courses.keys())[0] if seg_courses else None

            if matched_dep:
                seats = probe_seats_fast(f_info["id"], f_info["name"], t_info["id"], t_info["name"], date_str, matched_dep)
            else:
                seats = 0

        seg_results[seg_label] = seats
        if seats < min_seats:
            min_seats = seats
            bottleneck_seg = seg_label

    return {
        "direction": direction_name,
        "date": date_str,
        "main_dep": main_dep,
        "main_hours": main_hours,
        "segments": seg_results,
        "bottleneck_seg": bottleneck_seg,
        "bottleneck_seats": min_seats
    }


def scan_day_corridor(date_str: str) -> list:
    results = []
    # Pobieramy kursy główne dla Sanok -> Wrocław
    sw_main = query_api(STOPS["SANOK"]["id"], STOPS["SANOK"]["name"], STOPS["WROCLAW"]["id"], STOPS["WROCLAW"]["name"], date_str, 1) or {}
    for dep, data in sw_main.items():
        res = analyze_course_segments("Sanok ➔ Wrocław", SEGMENTS_EAST_WEST, date_str, dep, data["hours"])
        results.append(res)

    # Pobieramy kursy główne dla Wrocław -> Sanok
    ws_main = query_api(STOPS["WROCLAW"]["id"], STOPS["WROCLAW"]["name"], STOPS["SANOK"]["id"], STOPS["SANOK"]["name"], date_str, 1) or {}
    for dep, data in ws_main.items():
        res = analyze_course_segments("Wrocław ➔ Sanok", SEGMENTS_WEST_EAST, date_str, dep, data["hours"])
        results.append(res)

    return results


def save_corridor_csv(records: list):
    if not records:
        return
    file_exists = os.path.isfile(CSV_CORRIDOR)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for r in records:
        row = {
            "Data pomiaru": ts,
            "Kierunek": r["direction"],
            "Data kursu": r["date"],
            "Godzina": r["main_dep"],
            "Wąskie gardło (odcinek)": r["bottleneck_seg"],
            "Wolne miejsca (min)": r["bottleneck_seats"]
        }
        # Dodajemy kolumny poszczególnych segmentów
        for seg_name, s_val in r["segments"].items():
            row[seg_name] = s_val
        rows.append(row)

    if not rows:
        return

    fieldnames = list(rows[0].keys())
    with open(CSV_CORRIDOR, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def build_corridor_report(records: list):
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = [
        "# 🛣️ Sentinel N3: Radar Węzłów i Odcinków (Horyzont 7 Dni)\n\n",
        f"> 🕒 **Ostatnia aktualizacja:** `{now_ts}` | 🎯 **Zakres:** Kolejne 7 dni\n\n",
        "> Raport identyfikuje tzw. **wąskie gardła** na trasie N3. Kurs może być zablokowany z Sanoka do Wrocławia, ",
        "mimo że na większości trasy są wolne fotele, z powodu wyprzedania jednego krytycznego odcinka.\n\n",
        "## 🔍 Analiza Wąskich Gardeł Kursów\n\n",
        "| Data | Kierunek | Odjazd | Wąskie gardło (Odcinek krytyczny) | Dostępne miejsca | Status |\n",
        "| :--- | :--- | :---: | :--- | :---: | :---: |\n"
    ]

    for r in sorted(records, key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["main_dep"])):
        b_seats = r["bottleneck_seats"]
        b_seg = r["bottleneck_seg"]
        
        if b_seats <= 5:
            badge = f"🔴 **{b_seats} szt.** (KOREK)"
            status = "⚠️ Krytyczny brak miejsc"
        elif b_seats <= 15:
            badge = f"🟠 **{b_seats} szt.**"
            status = "Końcówka biletów"
        else:
            badge = f"🟢 `{b_seats} szt.`"
            status = "Dostępny"

        md.append(f"| 📅 **{r['date']}** | {r['direction']} | ⏰ **{r['main_dep']}** | **{b_seg}** | {badge} | {status} |\n")

    md.extend([
        "\n---\n\n",
        "## 📊 Szczegółowe obłożenie segmentów (Ostatni pomiar)\n\n"
    ])

    for r in records:
        md.append(f"### 🚌 {r['direction']} | 📅 {r['date']} {r['main_hours']}\n")
        md.append(f"> Najwęższy odcinek: **{r['bottleneck_seg']}** ({r['bottleneck_seats']} wolnych miejsc)\n\n")
        md.append("| Segment trasy | Wolne miejsca |\n| :--- | :---: |\n")
        for seg_name, s_val in r["segments"].items():
            bar_color = "🔴" if s_val <= 6 else ("🟠" if s_val <= 17 else "🟢")
            md.append(f"| {seg_name} | {bar_color} **{s_val}** / {MAX_CAPACITY} |\n")
        md.append("\n")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.writelines(md)


def main():
    start_t = time.time()
    print("==========================================================", flush=True)
    print("🛣️ SENTINEL CORRIDOR RADAR (7-DAYS SEGMENT ENGINE)", flush=True)
    print("==========================================================", flush=True)

    today = date.today()
    dates = [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(DAYS_AHEAD)]
    print(f"📡 Skanowanie łańcucha węzłów dla {len(dates)} dni ({dates[0]} - {dates[-1]})...", flush=True)

    all_records = []
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(scan_day_corridor, d) for d in dates]
        for fut in as_completed(futures):
            day_res = fut.result()
            all_records.extend(day_res)
            print(f"  ⚡ Zbadano węzły dla jednego dnia ({len(day_res)} kursów)...", flush=True)

    save_corridor_csv(all_records)
    build_corridor_report(all_records)

    print("==========================================================", flush=True)
    print(f"⏱️ ZAKOŃCZONO W CZASIE: {time.time() - start_t:.2f} s", flush=True)
    print(f"📁 Wyniki zapisano w: {CSV_CORRIDOR} oraz {REPORT_MD}", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
