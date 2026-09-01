import csv
from datetime import date, datetime, timedelta
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

# =====================================================================
#                     HERMES CONFIGURATION & MAPPING
# =====================================================================

PROJECT_NAME = "Project Aether: N3 Transit Observatory"
DAYS_FORWARD_SEARCH = 90
MAX_CONCURRENT_WORKERS = 8
TARGET_PROMO_THRESHOLD = 50.00
ANALYZE_FLOW_DAYS_COUNT = 7

DATA_ARCHIVE_CSV = "oracle_pulse.csv"
HORIZON_STATE_FILE = "chronos_boundary.txt"
README_REPORT_FILE = "README.md"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 8 węzłów magistrali N3
NODES_CATALOG = {
    "sanok": {"id": "17", "name": "SANOK D.A. Lipińskiego", "short": "Sanok"},
    "niebylec": {"id": "19", "name": "NIEBYLEC", "short": "Niebylec"},
    "rzeszow": {"id": "76", "name": "RZESZÓW D.A. PKS", "short": "Rzeszów"},
    "krakow_mda": {"id": "71", "name": "KRAKÓW MDA", "short": "Kraków MDA"},
    "balice": {"id": "72", "name": "KRAKÓW Lotnisko Balice", "short": "Balice"},
    "katowice": {"id": "73", "name": "KATOWICE D.A. ul. Sądowa 5", "short": "Katowice"},
    "gliwice": {"id": "123", "name": "GLIWICE Centrum Przesiadkowe ul. Składowa 8a", "short": "Gliwice"},
    "wroclaw": {"id": "77", "name": "WROCŁAW PKS Polbus", "short": "Wrocław"}
}

LINE_N3_WESTBOUND = [
    {
        "id": "N3_W1", "name": "Nocny (23:50)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["sanok", "niebylec", "rzeszow", "krakow_mda", "balice", "katowice", "gliwice", "wroclaw"],
        "schedule": {"sanok": "23:50", "niebylec": "00:50", "rzeszow": "01:30", "krakow_mda": "03:35", "balice": "04:00", "katowice": "04:55", "gliwice": "05:22", "wroclaw": "07:25"}
    },
    {
        "id": "N3_W2", "name": "Poranny I (03:00)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["sanok", "niebylec", "rzeszow", "krakow_mda", "balice", "katowice", "gliwice", "wroclaw"],
        "schedule": {"sanok": "03:00", "niebylec": "04:00", "rzeszow": "04:40", "krakow_mda": "06:55", "balice": "07:20", "katowice": "08:30", "gliwice": "09:00", "wroclaw": "10:55"}
    },
    {
        "id": "N3_W3", "name": "Poranny II (06:35)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["sanok", "niebylec", "rzeszow", "krakow_mda", "balice", "katowice", "gliwice", "wroclaw"],
        "schedule": {"sanok": "06:35", "niebylec": "07:40", "rzeszow": "08:40", "krakow_mda": "10:55", "balice": "11:30", "katowice": "12:30", "gliwice": "13:00", "wroclaw": "15:05"}
    },
    {
        "id": "N3_W4", "name": "Południowy (10:10)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["sanok", "niebylec", "rzeszow", "krakow_mda", "balice", "katowice", "gliwice", "wroclaw"],
        "schedule": {"sanok": "10:10", "niebylec": "11:25", "rzeszow": "12:20", "krakow_mda": "14:40", "balice": "15:15", "katowice": "16:25", "gliwice": "16:55", "wroclaw": "18:50"}
    },
    {
        "id": "N3_W5_SUN", "name": "Popołudniowy Niedzielny (16:20)", "days": [7],
        "nodes": ["sanok", "niebylec", "rzeszow", "krakow_mda", "balice", "katowice", "gliwice", "wroclaw"],
        "schedule": {"sanok": "16:20", "niebylec": "17:30", "rzeszow": "18:20", "krakow_mda": "20:35", "balice": "21:00", "katowice": "22:00", "gliwice": "22:27", "wroclaw": "00:20"}
    }
]

LINE_N3_EASTBOUND = [
    {
        "id": "N3_E1_MON", "name": "Poranny Poniedziałkowy (03:40)", "days": [1],
        "nodes": ["wroclaw", "gliwice", "katowice", "balice", "krakow_mda", "rzeszow", "niebylec", "sanok"],
        "schedule": {"wroclaw": "03:40", "gliwice": "05:40", "katowice": "06:10", "balice": "07:00", "krakow_mda": "07:40", "rzeszow": "09:55", "niebylec": "10:34", "sanok": "11:37"}
    },
    {
        "id": "N3_E2", "name": "Poranny (07:45)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["wroclaw", "gliwice", "katowice", "balice", "krakow_mda", "rzeszow", "niebylec", "sanok"],
        "schedule": {"wroclaw": "07:45", "gliwice": "09:45", "katowice": "10:15", "balice": "11:05", "krakow_mda": "11:45", "rzeszow": "13:50", "niebylec": "14:35", "sanok": "15:27"}
    },
    {
        "id": "N3_E3", "name": "Południowy (12:00)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["wroclaw", "gliwice", "katowice", "balice", "krakow_mda", "rzeszow", "niebylec", "sanok"],
        "schedule": {"wroclaw": "12:00", "gliwice": "14:00", "katowice": "14:30", "balice": "15:20", "krakow_mda": "16:15", "rzeszow": "18:40", "niebylec": "19:20", "sanok": "20:14"}
    },
    {
        "id": "N3_E4", "name": "Popołudniowy (15:35)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["wroclaw", "gliwice", "katowice", "balice", "krakow_mda", "rzeszow", "niebylec", "sanok"],
        "schedule": {"wroclaw": "15:35", "gliwice": "17:40", "katowice": "18:15", "balice": "19:05", "krakow_mda": "19:45", "rzeszow": "21:55", "niebylec": "22:30", "sanok": "23:36"}
    },
    {
        "id": "N3_E5", "name": "Nocny (22:25)", "days": [1, 2, 3, 4, 5, 6, 7],
        "nodes": ["wroclaw", "gliwice", "katowice", "balice", "krakow_mda", "rzeszow", "niebylec", "sanok"],
        "schedule": {"wroclaw": "22:25", "gliwice": "00:20", "katowice": "00:50", "balice": "01:50", "krakow_mda": "02:20", "rzeszow": "04:30", "niebylec": "05:00", "sanok": "06:11"}
    }
]

GATEWAY_ENDPOINT = "https://neobus.pl/"
GATEWAY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pl,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://neobus.pl",
    "Referer": "https://neobus.pl/",
}


# =====================================================================
#                     NETWORK & TELEMETRY PROTOCOL
# =====================================================================

def init_protocol_session() -> requests.Session:
    sess = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=20)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    try:
        sess.get(GATEWAY_ENDPOINT, headers=GATEWAY_HEADERS, timeout=10)
    except Exception:
        pass
    return sess


def normalize_timestamp(t: str) -> str:
    return re.sub(r'[-]', ':', t.strip())


def is_timestamp_matched(t1: str, t2: str) -> bool:
    n1, n2 = normalize_timestamp(t1), normalize_timestamp(t2)
    return n1 == n2 or n1 in n2 or n2 in n1


def fetch_node_pair(session: requests.Session, from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, seats_probe: int = 1):
    payload = {
        "ajax": "true",
        "dataType": "json",
        "module": "neotickets",
        "step": "1",
        "ticket_type": "normal",
        "initial_stop": str(from_id),
        "final_stop": str(to_id),
        "passengers": str(seats_probe),
        "date_there": date_str,
        "date_return": "",
        "initial_stop_name": from_name,
        "final_stop_name": to_name,
    }
    try:
        r = session.post(GATEWAY_ENDPOINT, data=payload, headers=GATEWAY_HEADERS, timeout=8)
        if r.status_code != 200:
            return []

        try:
            raw = r.json()
        except Exception:
            raw = json.loads(r.text)

        content = raw.get("neotickets", raw) if isinstance(raw, dict) else raw
        data = json.loads(content) if isinstance(content, str) else content

        records = []
        if isinstance(data, dict) and "ga4_data" in data and len(data["ga4_data"]) > 0:
            items = data["ga4_data"][0].get("items", [])
            for it in items:
                name = it.get("item_name", "")
                p_val = it.get("price")
                if p_val is None:
                    p_val = it.get("discount", 0.0)
                try:
                    price = float(p_val)
                except (ValueError, TypeError):
                    price = 0.0

                m = re.search(r"(\d{2}[-:]\d{2})\s*-\s*(\d{2}[-:]\d{2})", name)
                if m:
                    dep = normalize_timestamp(m.group(1))
                    arr = normalize_timestamp(m.group(2))
                    h_label = f"{dep} -> {arr}"
                else:
                    dep = name
                    h_label = name

                if price > 0:
                    records.append({"hours": h_label, "departure": dep, "price": price})
        return records
    except Exception:
        return []


def resolve_segment_capacity(from_id: str, from_name: str, to_id: str, to_name: str, date_str: str, dep_time: str) -> tuple:
    sess = init_protocol_session()
    base = fetch_node_pair(sess, from_id, from_name, to_id, to_name, date_str, seats_probe=1)
    matched = [c for c in base if is_timestamp_matched(c["departure"], dep_time)]
    if not matched:
        return 0, 0.0
    price_unit = matched[0]["price"]

    # Szybki check 65 miejsc
    c65 = fetch_node_pair(sess, from_id, from_name, to_id, to_name, date_str, seats_probe=65)
    if any(is_timestamp_matched(c["departure"], dep_time) for c in c65):
        c90 = fetch_node_pair(sess, from_id, from_name, to_id, to_name, date_str, seats_probe=90)
        if any(is_timestamp_matched(c["departure"], dep_time) for c in c90):
            return 90, price_unit
        low, high = 66, 90
    else:
        low, high = 1, 64

    exact_seats = 1
    while low <= high:
        mid = (low + high) // 2
        res = fetch_node_pair(sess, from_id, from_name, to_id, to_name, date_str, seats_probe=mid)
        if any(is_timestamp_matched(c["departure"], dep_time) for c in res):
            exact_seats = mid
            low = mid + 1
        else:
            high = mid - 1

    return exact_seats, price_unit


def evaluate_segment_task(s_from: dict, s_to: dict, dep_time: str, date_str: str) -> dict:
    free_seats, unit_price = resolve_segment_capacity(
        s_from["id"], s_from["name"], s_to["id"], s_to["name"], date_str, dep_time
    )
    cap = 90 if free_seats > 65 else 65
    pax = max(0, cap - free_seats) if free_seats > 0 else 0
    return {
        "segment": f"{s_from['short']} ➔ {s_to['short']}",
        "node_origin": s_from["short"],
        "node_target": s_to["short"],
        "dep_time": dep_time,
        "price": unit_price,
        "free_seats": free_seats if free_seats > 0 else "B/D",
        "capacity": cap,
        "passengers": pax if free_seats > 0 else None,
        "revenue": (pax * unit_price) if free_seats > 0 else 0.0
    }


# =====================================================================
#                     ANALYTICS & REPORTING
# =====================================================================

def generate_horizon_dates(count: int) -> list:
    today = date.today()
    return [(today + timedelta(days=i)).strftime("%d.%m.%Y") for i in range(count)]


def save_pulse_archive(flow_dataset: list, filename: str):
    if not flow_dataset:
        return
    file_exists = os.path.isfile(filename)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    records = []
    for item in flow_dataset:
        for s in item.get("segments", []):
            rev_val = f"{s['revenue']:.2f}" if s.get('revenue') is not None else "0.00"
            p_val = f"{s['price']:.2f}" if s.get('price') is not None else "0.00"
            records.append([
                ts,
                item["direction"],
                item["course_name"],
                item["date"],
                item["start_time"],
                s["segment"],
                s["node_origin"],
                s["node_target"],
                s["dep_time"],
                p_val,
                s["free_seats"],
                s["passengers"] if s["passengers"] is not None else "B/D",
                s["capacity"],
                s["delta_str"],
                rev_val
            ])

    with open(filename, mode="a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "Timestamp", "Direction", "Course Name", "Date", "Origin Departure",
                "Segment", "Node Origin", "Node Target", "Departure Time", "Price (PLN)",
                "Free Seats", "Onboard Passengers", "Vehicle Capacity", "Node Exchange Delta", "Est Revenue (PLN)"
            ])
        w.writerows(records)
    print(f"💾 [{filename}] Zapisano {len(records)} wierszy telemetrycznych.", flush=True)


def format_occupancy_bar(seats, total: int = 65) -> str:
    if not isinstance(seats, int) or seats < 0:
        return "B/D"
    filled = max(0, min(10, int(round((seats / total) * 10))))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {seats}/{total}"


def generate_observatory_readme(flow_dataset: list, terminus_direct: list):
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    md = [
        f"# 🏛️ {PROJECT_NAME}\n\n",
        f"> 🕒 **Ostatnia synchronizacja:** `{now_ts}`  \n",
        "> 📊 **Status magistrali N3 (Sanok ⇄ Wrocław)** | 8 Węzłów | 7 Odcinków Pomiarowych\n\n",
        "## 🧭 Bezpośrednie kursy magistralne (Terminus ➔ Terminus)\n\n",
        "| Kierunek | Data | Odjazd | Wolne miejsca | Cena min. |\n",
        "| :--- | :--- | :---: | :---: | :---: |\n"
    ]

    for c in terminus_direct[:20]:
        seats_v = c.get("seats", "B/D")
        cap = 90 if isinstance(seats_v, int) and seats_v > 65 else 65
        bar = format_occupancy_bar(seats_v, cap)
        p_tag = f"🔥 **{c['price']:.2f} zł**" if c['price'] <= TARGET_PROMO_THRESHOLD else f"{c['price']:.2f} zł"
        md.append(f"| {c['route']} | 📅 **{c['date']}** | ⏰ {c['departure']} | `{bar}` | {p_tag} |\n")

    if flow_dataset:
        md.extend([
            "\n---\n\n",
            "## 📈 Analityka Węzłowa: Potoki, Wymiana Pasażerska (Δ) i Estymacja Obrotów\n\n"
        ])

        for item in flow_dataset:
            rev = item.get("total_revenue", 0.0)
            lf = item.get("avg_load_factor", 0.0)

            md.append(f"### 🚌 {item['direction']}: `{item['course_name']}` | 📅 Data: **{item['date']}**\n")
            md.append(f"> 💵 **Szacowany obrót kursu:** `{rev:.2f} PLN` | 📊 **Średni Load Factor:** `{lf:.1f}%`\n\n")
            md.append("| Odcinek | Odjazd | Cena | Wolne | Na pokładzie | Bilans w węźle (Δ) | Obrót odcinka |\n")
            md.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |\n")

            for s in item["segments"]:
                cap = s["capacity"]
                pax = s["passengers"]
                pax_str = f"**{pax}/{cap}**" if pax is not None else "B/D"
                pr_str = f"{s['price']:.2f} zł" if s.get('price') is not None else "B/D"
                rev_str = f"**{s['revenue']:.2f} zł**" if s.get('revenue') is not None else "B/D"
                md.append(f"| {s['segment']} | ⏰ {s['dep_time']} | {pr_str} | `{s['free_seats']}` | {pax_str} | {s['delta_str']} | {rev_str} |\n")
            md.append("\n")

    md.extend([
        "\n---\n\n",
        "## 🗺️ Macierz Obłożenia Magistrali N3\n\n",
        "### 🚌 Trasa Zachodnia (Sanok ➔ Wrocław)\n\n",
        "![Heatmap Westbound](chronos_westbound.png)\n\n",
        "### 🚌 Trasa Wschodnia (Wrocław ➔ Sanok)\n\n",
        "![Heatmap Eastbound](chronos_eastbound.png)\n"
    ])

    with open(README_REPORT_FILE, "w", encoding="utf-8") as f:
        f.writelines(md)
    print("📄 Zaktualizowano README.md.", flush=True)


def check_and_signal_horizon_expansion(active_dates: list):
    if not active_dates:
        return
    dt_dates = sorted([time.strptime(d, "%d.%m.%Y") for d in active_dates])
    furthest = time.strftime("%d.%m.%Y", dt_dates[-1])
    prev = ""
    if os.path.isfile(HORIZON_STATE_FILE):
        with open(HORIZON_STATE_FILE, "r", encoding="utf-8") as f:
            prev = f.read().strip()

    if not prev:
        with open(HORIZON_STATE_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)
        return

    if time.strptime(furthest, "%d.%m.%Y") > time.strptime(prev, "%d.%m.%Y"):
        msg = (
            f"📢 **HERMES RADAR: NOWY HORYZONT CZASOWY OTWARTY!** @everyone\n\n"
            f"📅 Zakres sprzedaży rozszerzony do: **{furthest}** (wcześniej: {prev})\n"
            f"🚀 Bilety promocyjne w puli!"
        )
        if DISCORD_WEBHOOK_URL:
            try:
                requests.post(DISCORD_WEBHOOK_URL, json={"username": "Hermes Sentinel", "content": msg}, timeout=8)
            except Exception:
                pass
        with open(HORIZON_STATE_FILE, "w", encoding="utf-8") as f:
            f.write(furthest)


# =====================================================================
#                     EXECUTION CONTROL
# =====================================================================

def main():
    start_time = time.time()
    print("==========================================================", flush=True)
    print(f"🚀 {PROJECT_NAME} - ROZPOCZĘCIE POMIARU", flush=True)
    print("==========================================================", flush=True)

    session = init_protocol_session()
    horizon = generate_horizon_dates(DAYS_FORWARD_SEARCH)
    total_days = len(horizon)

    terminus_direct = []

    # ETAP 1: Szybki skan bezpośredni Sanok <-> Wrocław
    print(f"\n📡 [ETAP 1/2] Skanowanie krańcowe relacji bezpośrednich ({total_days} dni)...", flush=True)
    for idx, d in enumerate(horizon, 1):
        west = fetch_node_pair(session, NODES_CATALOG["sanok"]["id"], NODES_CATALOG["sanok"]["name"], NODES_CATALOG["wroclaw"]["id"], NODES_CATALOG["wroclaw"]["name"], d, 1)
        east = fetch_node_pair(session, NODES_CATALOG["wroclaw"]["id"], NODES_CATALOG["wroclaw"]["name"], NODES_CATALOG["sanok"]["id"], NODES_CATALOG["sanok"]["name"], d, 1)

        for c in west:
            terminus_direct.append({"route": "Sanok ➔ Wrocław", "date": d, "departure": c["departure"], "price": c["price"], "seats": "B/D"})
        for c in east:
            terminus_direct.append({"route": "Wrocław ➔ Sanok", "date": d, "departure": c["departure"], "price": c["price"], "seats": "B/D"})

        if idx % 15 == 0 or idx == total_days:
            pct = (idx / total_days) * 100
            print(f"  [📅 {idx:02d}/{total_days} | {pct:4.1f}%] Skan horyzontu: {d}...", flush=True)

    all_dates = sorted(list({c["date"] for c in terminus_direct}), key=lambda x: datetime.strptime(x, "%d.%m.%Y"))
    check_and_signal_horizon_expansion(all_dates)

    # ETAP 2: Dokładna analityka 7 odcinków N3 dla najbliższych dni rozkładowych
    flow_dates = all_dates[:ANALYZE_FLOW_DAYS_COUNT] if all_dates else horizon[:ANALYZE_FLOW_DAYS_COUNT]
    print(f"\n🔬 [ETAP 2/2] Rekonstrukcja potoków pasażerskich (7 segmentów na kurs, {len(flow_dates)} dni)...", flush=True)

    flow_dataset = []
    for d in flow_dates:
        d_obj = datetime.strptime(d, "%d.%m.%Y")
        dow = d_obj.isoweekday()

        active_schedules = [c for c in LINE_N3_WESTBOUND if dow in c["days"]] + \
                           [c for c in LINE_N3_EASTBOUND if dow in c["days"]]

        for c_def in active_schedules:
            direction = "Sanok ➔ Wrocław" if "W" in c_def["id"] else "Wrocław ➔ Sanok"
            nodes_keys = c_def["nodes"]
            schedule = c_def["schedule"]

            futures_pool = []
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as seg_exec:
                for i in range(len(nodes_keys) - 1):
                    s_orig = NODES_CATALOG[nodes_keys[i]]
                    s_dest = NODES_CATALOG[nodes_keys[i + 1]]
                    futures_pool.append(seg_exec.submit(evaluate_segment_task, s_orig, s_dest, schedule[nodes_keys[i]], d))

                segments_eval = [f.result() for f in futures_pool]

            if not any(s["price"] > 0 for s in segments_eval):
                continue

            for idx, s in enumerate(segments_eval):
                pax = s["passengers"]
                delta_str = "Start trasy"
                if idx > 0 and pax is not None and segments_eval[idx - 1]["passengers"] is not None:
                    diff = pax - segments_eval[idx - 1]["passengers"]
                    delta_str = f"📈 +{diff}" if diff > 0 else (f"📉 {diff}" if diff < 0 else "➡️ 0")
                s["delta_str"] = delta_str

            total_rev = sum(s["revenue"] for s in segments_eval)
            max_c = max(s["capacity"] for s in segments_eval)
            valid_p = [s["passengers"] for s in segments_eval if s["passengers"] is not None]
            avg_p = sum(valid_p) / len(valid_p) if valid_p else 0

            flow_dataset.append({
                "direction": direction,
                "course_name": c_def["name"],
                "date": d,
                "start_time": schedule[nodes_keys[0]],
                "capacity": max_c,
                "total_revenue": total_rev,
                "avg_load_factor": (avg_p / max_c) * 100,
                "segments": segments_eval
            })

    flow_dataset.sort(key=lambda x: (datetime.strptime(x["date"], "%d.%m.%Y"), x["start_time"]))

    # Zapis i publikacja
    print("\n💾 Utrwalanie telemetrii w archiwum...", flush=True)
    save_pulse_archive(flow_dataset, DATA_ARCHIVE_CSV)
    generate_observatory_readme(flow_dataset, terminus_direct)

    elapsed = time.time() - start_time
    print("==========================================================", flush=True)
    print(f"⏱️ CYKL ZAKOŃCZONY POMYŚLNIE W CZASIE: {elapsed:.2f} s", flush=True)
    print("==========================================================", flush=True)


if __name__ == "__main__":
    main()
