import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

ARCHIVE_FILE = "oracle_pulse.csv"
OUT_WEST = "chronos_westbound.png"
OUT_EAST = "chronos_eastbound.png"

# Ścisła kolejność geograficzna segmentów
ORDER_WESTBOUND = [
    "Sanok ➔ Niebylec",
    "Niebylec ➔ Rzeszów",
    "Rzeszów ➔ Kraków MDA",
    "Kraków MDA ➔ Balice",
    "Balice ➔ Katowice",
    "Katowice ➔ Gliwice",
    "Gliwice ➔ Wrocław"
]

ORDER_EASTBOUND = [
    "Wrocław ➔ Gliwice",
    "Gliwice ➔ Katowice",
    "Katowice ➔ Balice",
    "Balice ➔ Kraków MDA",
    "Kraków MDA ➔ Rzeszów",
    "Rzeszów ➔ Niebylec",
    "Niebylec ➔ Sanok"
]


def create_fallback_chart(out_img: str, title: str):
    plt.figure(figsize=(10, 4))
    plt.text(0.5, 0.5, "Oczekiwanie na pełne dane telemetryczne...", horizontalalignment='center', verticalalignment='center', fontsize=12, color='gray')
    plt.title(title, fontsize=12, pad=10)
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(out_img, dpi=150)
    plt.close()


def build_corridor_heatmap(df: pd.DataFrame, direction_name: str, segment_order: list, out_img: str):
    if df.empty:
        create_fallback_chart(out_img, f"Magistrala N3: {direction_name}")
        return

    subset = df[df["Direction"] == direction_name].copy()
    if subset.empty:
        create_fallback_chart(out_img, f"Magistrala N3: {direction_name}")
        return

    # Normalizacja pasażerów i etykiety czasu
    subset["Onboard Passengers"] = pd.to_numeric(subset["Onboard Passengers"], errors="coerce").fillna(0).astype(int)
    subset["Time_Label"] = subset["Date"] + " " + subset["Origin Departure"]

    # Pivot z zachowaniem unikalnych ostatnich odczytów
    pivot = subset.pivot_table(
        index="Segment",
        columns="Time_Label",
        values="Onboard Passengers",
        aggfunc="last"
    )

    # 1. Narzucenie kolejności geograficznej stacja po stacji (oś Y)
    pivot = pivot.reindex(segment_order).fillna(0).astype(int)

    # 2. Chronologiczne posortowanie kursów (oś X)
    sorted_columns = sorted(
        pivot.columns, 
        key=lambda x: pd.to_datetime(x, format="%d.%m.%Y %H:%M", errors="coerce")
    )
    pivot = pivot[sorted_columns]

    if pivot.empty or pivot.shape[1] == 0:
        create_fallback_chart(out_img, f"Magistrala N3: {direction_name}")
        return

    plt.figure(figsize=(16, 7))
    sns.heatmap(
        pivot, 
        cmap="YlGnBu", 
        annot=True, 
        fmt="d", 
        cbar_kws={'label': 'Pasażerowie na pokładzie'},
        linewidths=0.5,
        linecolor='lightgray'
    )
    plt.title(f"Obłożenie Odcinków Magistrali N3: {direction_name}", fontsize=14, pad=15)
    plt.xlabel("Kurs (Data i godzina startu z pętli początkowej)", fontsize=11)
    plt.ylabel("Przebieg trasy (Kolejne węzły)", fontsize=11)
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    plt.savefig(out_img, dpi=200)
    plt.close()
    print(f"📊 Wygenerowano mapę korytarza ({direction_name}): {out_img}")


def main():
    print("=== CHRONOS HEATMAP GENERATOR ===")
    if not os.path.isfile(ARCHIVE_FILE):
        create_fallback_chart(OUT_WEST, "Magistrala N3: Sanok ➔ Wrocław")
        create_fallback_chart(OUT_EAST, "Magistrala N3: Wrocław ➔ Sanok")
        return

    try:
        df = pd.read_csv(ARCHIVE_FILE)
        build_corridor_heatmap(df, "Sanok ➔ Wrocław", ORDER_WESTBOUND, OUT_WEST)
        build_corridor_heatmap(df, "Wrocław ➔ Sanok", ORDER_EASTBOUND, OUT_EAST)
    except Exception as e:
        print(f"[!] Błąd generatora wykresów: {e}")
        create_fallback_chart(OUT_WEST, "Magistrala N3: Sanok ➔ Wrocław")
        create_fallback_chart(OUT_EAST, "Magistrala N3: Wrocław ➔ Sanok")


if __name__ == "__main__":
    main()
