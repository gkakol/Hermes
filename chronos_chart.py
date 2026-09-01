import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

CSV_SAN_WRO = "ceny_sanok_wroclaw.csv"
CSV_WRO_SAN = "ceny_wroclaw_sanok.csv"
IMG_SAN_WRO = "heatmapa_sanok_wroclaw.png"
IMG_WRO_SAN = "heatmapa_wroclaw_sanok.png"


def generate_heatmap(csv_path: str, output_img: str, title: str):
    if not os.path.isfile(csv_path):
        return

    try:
        df = pd.read_csv(csv_path)
        if df.empty or "Wolne miejsca" not in df.columns:
            return

        df["Wolne miejsca"] = pd.to_numeric(df["Wolne miejsca"], errors="coerce")
        df = df.dropna(subset=["Wolne miejsca"])
        df["Wolne miejsca"] = df["Wolne miejsca"].astype(int)

        # Klucz unikalności: bierzemy najświeższy odczyt dla (Data, Kod kursu)
        df = df.drop_duplicates(subset=["Data kursu", "Kod kursu"], keep="last")

        pivot = df.pivot(index="Kod kursu", columns="Data kursu", values="Wolne miejsca")
        if pivot.empty:
            return

        # Sortowanie kursów K1, K2... na osi Y
        sorted_index = sorted(pivot.index, key=lambda x: int(re.sub(r'\D', '', str(x))) if re.search(r'\d', str(x)) else 99)
        pivot = pivot.reindex(sorted_index)

        # Sortowanie dat na osi X
        sorted_cols = sorted(pivot.columns, key=lambda x: pd.to_datetime(x, format="%d.%m.%Y", errors="coerce"))
        pivot = pivot[sorted_cols]

        fig_w = max(14, len(sorted_cols) * 0.28)
        plt.figure(figsize=(fig_w, 5))

        sns.heatmap(
            pivot,
            cmap="RdYlGn",
            annot=True,
            fmt="d",
            vmin=0,
            vmax=65,
            cbar_kws={'label': 'Wolne miejsca (max 65)'},
            linewidths=0.5,
            linecolor='lightgray'
        )
        plt.title(title, fontsize=13, pad=12)
        plt.xlabel("Data kursu", fontsize=10)
        plt.ylabel("Kurs w danym dniu", fontsize=10)
        plt.xticks(rotation=45, ha="right", fontsize=8)
        plt.yticks(rotation=0)
        plt.tight_layout()
        plt.savefig(output_img, dpi=180)
        plt.close()
        print(f"📊 Wygenerowano poprawną heatmapę: {output_img}")
    except Exception as e:
        print(f"[!] Błąd generowania heatmapy dla {csv_path}: {e}")


def main():
    print("=== GENEROWANIE HEATMAP CHRONOS (K1, K2...) ===")
    generate_heatmap(CSV_SAN_WRO, IMG_SAN_WRO, "Dostępność wolnych miejsc: Sanok ➔ Wrocław")
    generate_heatmap(CSV_WRO_SAN, IMG_WRO_SAN, "Dostępność wolnych miejsc: Wrocław ➔ Sanok")


if __name__ == "__main__":
    main()
