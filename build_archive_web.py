import csv
import glob
import json
import os
from datetime import datetime

DOCS_DIR = "docs"
ARCHIVE_DIR = "archive"
OUTPUT_JSON = os.path.join(DOCS_DIR, "data.json")

DNI_TYGODNIA = ["Pn", "Wt", "Śr", "Cz", "Pt", "Sb", "Nd"]


def parse_date_safe(d_str):
    try:
        return datetime.strptime(d_str.strip(), "%d.%m.%Y").date()
    except Exception:
        return None


def parse_datetime_safe(dt_str):
    try:
        return datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def process_archive():
    os.makedirs(DOCS_DIR, exist_ok=True)
    
    # 1. Zbieramy wszystkie pliki historyczne
    csv_files = glob.glob(os.path.join(ARCHIVE_DIR, "arch_*.csv"))
    
    courses_map = {}

    for path in csv_files:
        route_name = "Sanok ➔ Wrocław" if "sanok_wroclaw" in path else "Wrocław ➔ Sanok"
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    d_kurs = row.get("Data kursu")
                    godz = row.get("Godzina")
                    if not d_kurs or not godz:
                        continue

                    # Unikalny klucz kursu
                    key = (route_name, d_kurs, godz)
                    meas_dt = parse_datetime_safe(row.get("Data pomiaru", ""))
                    
                    price = float(row.get("Cena (PLN)", 0.0) or 0.0)
                    seats_raw = row.get("Wolne miejsca", "")
                    seats = int(seats_raw) if str(seats_raw).isdigit() else None

                    # Zachowujemy najświeższy pomiar dla danego kursu (ostatni stan przed odjazdem)
                    if key not in courses_map:
                        courses_map[key] = {
                            "meas_dt": meas_dt,
                            "price": price,
                            "seats": seats
                        }
                    else:
                        prev_meas = courses_map[key]["meas_dt"]
                        if meas_dt and (prev_meas is None or meas_dt > prev_meas):
                            courses_map[key] = {
                                "meas_dt": meas_dt,
                                "price": price,
                                "seats": seats
                            }
        except Exception as e:
            print(f"[!] Błąd odczytu {path}: {e}")

    # 2. Formatowanie rekordów i statystyki
    records = []
    weekday_stats = {d: {"total": 0, "sold_out": 0, "sum_price": 0.0} for d in DNI_TYGODNIA}

    for (route, d_kurs, godz), data in courses_map.items():
        dt_obj = parse_date_safe(d_kurs)
        if not dt_obj:
            continue
        
        day_name = DNI_TYGODNIA[dt_obj.weekday()]
        seats = data["seats"]
        price = data["price"]
        is_sold_out = (seats is not None and seats <= 3)

        weekday_stats[day_name]["total"] += 1
        weekday_stats[day_name]["sum_price"] += price
        if is_sold_out:
            weekday_stats[day_name]["sold_out"] += 1

        records.append({
            "route": route,
            "date": d_kurs,
            "day": day_name,
            "hour": godz,
            "price": price,
            "seats": seats if seats is not None else -1,
            "sold_out": is_sold_out
        })

    # Sortowanie chronologiczne malejąco (najświeższe na górze)
    records.sort(
        key=lambda r: (datetime.strptime(r["date"], "%d.%m.%Y"), r["hour"]),
        reverse=True
    )

    # 3. Metryki zbiorcze
    total_courses = len(records)
    total_sold_out = sum(1 for r in records if r["sold_out"])
    avg_price = (sum(r["price"] for r in records) / total_courses) if total_courses > 0 else 0.0

    analytics = {
        "days": DNI_TYGODNIA,
        "sold_out_rates": [
            round((weekday_stats[d]["sold_out"] / weekday_stats[d]["total"] * 100), 1)
            if weekday_stats[d]["total"] > 0 else 0
            for d in DNI_TYGODNIA
        ],
        "avg_prices": [
            round((weekday_stats[d]["sum_price"] / weekday_stats[d]["total"]), 2)
            if weekday_stats[d]["total"] > 0 else 0
            for d in DNI_TYGODNIA
        ]
    }

    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_courses": total_courses,
        "total_sold_out": total_sold_out,
        "sold_out_rate_pct": round((total_sold_out / total_courses * 100), 1) if total_courses > 0 else 0,
        "avg_price": round(avg_price, 2),
        "analytics": analytics,
        "records": records
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"✅ Pomyślnie wygenerowano {OUTPUT_JSON} (Zarchiwizowano {total_courses} kursów).")


if __name__ == "__main__":
    process_archive()
