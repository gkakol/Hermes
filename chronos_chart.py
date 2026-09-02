import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

CSV_SAN_WRO = "ceny_sanok_wroclaw.csv"
CSV_WRO_SAN = "ceny_wroclaw_sanok.csv"
IMG_SAN_WRO = "heatmapa_sanok_wroclaw.png"
IMG_WRO_SAN = "heatmapa_wroclaw_sanok.png"


def create_blank_chart(output_img: str, title: str):
    plt.figure(figsize=(12, 4))
    plt.text(0.5, 0.5, "Trwa zbieranie danych...", horizontalalignment='center', verticalalignment='center', fontsize=12, color='gray')
    plt.title(title, fontsize=12, pad=10)
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(output_img, dpi=150)
    plt.close()


def generate_heatmap(csv_path: str, output_img: str, title: str):
    if not os.path.isfile(csv_path):
        create_blank_chart(output_img, title)
        return

    try:
        df = pd.read_csv(csv_path)
        if df.empty or "Wolne miejsca" not in df.columns:
            create_blank_chart(output_img, title)
            return

        df["Wolne miejsca"] = pd.to_numeric(df["Wolne miejsca"], errors="coerce").fillna(0).astype(int)
        df = df.drop_duplicates(subset=["Data kursu", "Godzina"], keep="last")

        pivot = df.pivot(index="Godzina", columns="Data kursu", values="Wolne miejsca")
        if pivot.empty:
            create_blank_chart(output_img, title)
            return

        pivot = pivot.sort_index()
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
            cbar_kws={'label': 'Wolne miejsca'},
            linewidths=0.4,
            linecolor='lightgray'
        )
        
        clean_title = title.replace("➔", "->")
        plt.title(clean_title, fontsize=13, pad=12)
        plt.xlabel("Data kursu", fontsize=10)
        plt.ylabel("Godzina odjazdu", fontsize=10)
        plt.xticks(rotation=45, ha="right", fontsize=8)
        plt.yticks(rotation=0)
        plt.tight_layout()
        plt.savefig(output_img, dpi=180)
        plt.close()
        print(f"Wygenerowano heatmapę: {output_img}")
    except Exception as e:
        print(f"[!] Błąd generowania heatmapy dla {csv_path}: {e}")
        create_blank_chart(output_img, title)


def main():
    print("=== GENEROWANIE HEATMAP CHRONOS ===")
    generate_heatmap(CSV_SAN_WRO, IMG_SAN_WRO, "Dostepnosc miejsc: Sanok -> Wroclaw")
    generate_heatmap(CSV_WRO_SAN, IMG_WRO_SAN, "Dostepnosc miejsc: Wroclaw -> Sanok")


if __name__ == "__main__":
    main()import os
import re
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

CSV_SAN_WRO = "ceny_sanok_wroclaw.csv"
CSV_WRO_SAN = "ceny_wroclaw_sanok.csv"
IMG_SAN_WRO = "heatmapa_sanok_wroclaw.png"
IMG_WRO_SAN = "heatmapa_wroclaw_sanok.png"


def create_blank_chart(output_img: str, title: str):
    plt.figure(figsize=(12, 4))
    plt.text(0.5, 0.5, "Trwa zbieranie danych...", horizontalalignment='center', verticalalignment='center', fontsize=12, color='gray')
    plt.title(title, fontsize=12, pad=10)
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(output_img, dpi=150)
    plt.close()


def generate_heatmap(csv_path: str, output_img: str, title: str):
    if not os.path.isfile(csv_path):
        create_blank_chart(output_img, title)
        return

    try:
        df = pd.read_csv(csv_path)
        if df.empty or "Wolne miejsca" not in df.columns:
            create_blank_chart(output_img, title)
            return

        # Zamiana wartości B/D i nieliczbowych na NaN, a potem 0 do wizualizacji
        df["Wolne miejsca"] = pd.to_numeric(df["Wolne miejsca"], errors="coerce").fillna(0).astype(int)

        # Pobieramy najświeższy odczyt dla danej pary (Data kursu, Godzina)
        df = df.drop_duplicates(subset=["Data kursu", "Godzina"], keep="last")

        pivot = df.pivot(index="Godzina", columns="Data kursu", values="Wolne miejsca")
        if pivot.empty:
            create_blank_chart(output_img, title)
            return

        # Sortowanie osi Y (godziny) i osi X (daty)
        pivot = pivot.sort_index()
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
            cbar_kws={'label': 'Wolne miejsca'},
            linewidths=0.4,
            linecolor='lightgray'
        )
        # Tytuł bez znaków specjalnych Unicode, aby uniknąć błędów czcionki w Linuxie
        clean_title = title.replace("➔", "->")
        plt.title(clean_title, fontsize=13, pad=12)
        plt.xlabel("Data kursu", fontsize=10)
        plt.ylabel("Godzina odjazdu", fontsize=10)
        plt.xticks(rotation=45, ha="right", fontsize=8)
        plt.yticks(rotation=0)
        plt.tight_layout()
        plt.savefig(output_img, dpi=180)
        plt.close()
        print(f"Wygenerowano heatmapę: {output_img}")
    except Exception as e:
        print(f"[!] Blad generowania heatmapy dla {csv_path}: {e}")
        create_blank_chart(output_img, title)


def main():
    print("=== GENEROWANIE HEATMAP CHRONOS ===")
    generate_heatmap(CSV_SAN_WRO, IMG_SAN_WRO, "Dostepnosc miejsc: Sanok -> Wroclaw")
    generate_heatmap(CSV_WRO_SAN, IMG_WRO_SAN, "Dostepnosc miejsc: Wroclaw -> Sanok")


if __name__ == "__main__":
    main()
