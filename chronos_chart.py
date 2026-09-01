import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

CSV_SAN_WRO = "ceny_sanok_wroclaw.csv"
CSV_WRO_SAN = "ceny_wroclaw_sanok.csv"
IMG_SAN_WRO = "heatmapa_sanok_wroclaw.png"
IMG_WRO_SAN = "heatmapa_wroclaw_sanok.png"


def create_fallback(out_img: str, title: str):
    plt.figure(figsize=(10, 4))
    plt.text(0.5, 0.5, "Oczekiwanie na dane...", horizontalalignment='center', verticalalignment='center', fontsize=12, color='gray')
    plt.title(title, fontsize=12, pad=10)
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(out_img, dpi=150)
    plt.close()


def generate_heatmap(csv_path: str, output_img: str, title: str):
    if not os.path.isfile(csv_path):
        create_fallback(output_img, title)
        return

    try:
        df = pd.read_csv(csv_path)
        if df.empty or "Wolne miejsca" not in df.columns:
            create_fallback(output_img, title)
            return

        # Zachowujemy tylko liczbowe wartości miejsc
        df = df[df["Wolne miejsca"].astype(str).str.isdigit()].copy()
        if df.empty:
            create_fallback(output_img, title)
            return

        df["Wolne miejsca"] = df["Wolne miejsca"].astype(int)
        df = df.drop_duplicates(subset=["Data kursu", "Godzina kursu"], keep="last")

        pivot = df.pivot(index="Godzina kursu", columns="Data kursu", values="Wolne miejsca")
        if pivot.empty:
            create_fallback(output_img, title)
            return

        # Sortowanie kolumn chronologicznie
        sorted_cols = sorted(pivot.columns, key=lambda x: pd.to_datetime(x, format="%d.%m.%Y", errors="coerce"))
        pivot = pivot[sorted_cols]

        plt.figure(figsize=(max(16, len(sorted_cols) * 0.35), 6))
        sns.heatmap(
            pivot,
            cmap="RdYlGn",
            annot=True,
            fmt="d",
            cbar_kws={'label': 'Wolne miejsca'},
            linewidths=0.5,
            linecolor='lightgray'
        )
        plt.title(title, fontsize=14, pad=15)
        plt.xlabel("Data kursu", fontsize=11)
        plt.ylabel("Godzina kursu", fontsize=11)
        plt.xticks(rotation=45, ha="right", fontsize=9)
        plt.yticks(rotation=0)
        plt.tight_layout()
        plt.savefig(output_img, dpi=200)
        plt.close()
        print(f"📊 Wygenerowano heatmapę: {output_img}")
    except Exception as e:
        print(f"[!] Błąd generowania wykresu dla {csv_path}: {e}")
        create_fallback(output_img, title)


def main():
    print("=== GENEROWANIE HEATMAP SANOK ⇄ WROCŁAW ===")
    generate_heatmap(CSV_SAN_WRO, IMG_SAN_WRO, "Dostępność wolnych miejsc: Sanok ➔ Wrocław")
    generate_heatmap(CSV_WRO_SAN, IMG_WRO_SAN, "Dostępność wolnych miejsc: Wrocław ➔ Sanok")


if __name__ == "__main__":
    main()
