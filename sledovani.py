import csv
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# --- NASTAVENÍ (1 blok = 5.8 hodiny, 4 bloky za sebou = ~24 hodin) ---
DOBA_BEHU_HODIN = 5.8
INTERVAL_SEKUND = 45
VYSTUPNI_SOUBOR = "turnusy_hotovo.csv"
DIAGNOSTIKA_SOUBOR = "ukazka_dat.json"
DB_SOUBOR = "odis_data.db"

URL = "https://mpvnet.cz/odis/map/mapData"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/json; charset=UTF-8",
    "Referer": "https://mpvnet.cz/odis/map"
}
PAYLOAD = {"w": 17.0, "s": 49.3, "e": 19.0, "n": 50.5, "zoom": 10}


def inicializuj_db():
    conn = sqlite3.connect(DB_SOUBOR)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS odjete_spoje (
            datum TEXT,
            vozidlo TEXT,
            linka TEXT,
            spoj TEXT,
            zacatek TEXT,
            konec TEXT,
            PRIMARY KEY (datum, vozidlo, linka, spoj)
        )
    """)
    conn.commit()
    return conn


def vyexportuj_turnusy_do_csv(conn):
    c = conn.cursor()
    c.execute("""
        SELECT datum, vozidlo, linka, spoj, zacatek, konec
        FROM odjete_spoje
        ORDER BY datum, vozidlo, zacatek
    """)
    radky = c.fetchall()

    turnusy = {}
    for datum, vozidlo, linka, spoj, zacatek, konec in radky:
        klic = (datum, vozidlo)
        if klic not in turnusy:
            turnusy[klic] = []
        turnusy[klic].append(f"{zacatek[:5]}-{konec[:5]} (L{linka}/{spoj})")

    with open(VYSTUPNI_SOUBOR, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Datum", "ID Vozidla / Kurzu", "Počet spojů", "Celý denní turnus (Od-Do Linka/Spoj)"])
        for (datum, vozidlo), spoje in turnusy.items():
            writer.writerow([datum, vozidlo, len(spoje), " | ".join(spoje)])


def uloz_na_github(zprava):
    """Průběžně nahraje aktualizovanou tabulku i databázi přímo do vašeho GitHubu."""
    try:
        subprocess.run(["git", "config", "user.name", "ODIS-Automat"], check=False)
        subprocess.run(["git", "config", "user.email", "automat@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", VYSTUPNI_SOUBOR, DB_SOUBOR, DIAGNOSTIKA_SOUBOR], check=False)
        subprocess.run(["git", "commit", "-m", zprava], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception as e:
        print(f"Chyba při průběžném ukládání na GitHub: {e}")


def najdi_seznam_vozidel(data):
    """Najde seznam vozidel v JSON odpovědi bez ohledu na pojmenování klíče."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for klic, hodnota in data.items():
            if isinstance(hodnota, list) and len(hodnota) > 0 and isinstance(hodnota[0], dict):
                return hodnota
    return []


def spust_automat():
    conn = inicializuj_db()
    c = conn.cursor()
    pocet_cyklu = int((DOBA_BEHU_HODIN * 3600) / INTERVAL_SEKUND)
    ulozit_kazdych_cyklu = 20  # Uloží tabulku na web každých 15 minut (20 * 45 s)

    for i in range(pocet_cyklu):
        nyni = datetime.now(ZoneInfo("Europe/Prague"))
        datum_str = nyni.strftime("%Y-%m-%d")
        cas_str = nyni.strftime("%H:%M:%S")

        try:
            r = requests.post(URL, json=PAYLOAD, headers=HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()

            # Při prvním stažení uloží ukázku surových dat pro případnou kontrolu
            if not os.path.exists(DIAGNOSTIKA_SOUBOR):
                with open(DIAGNOSTIKA_SOUBOR, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

            vozidla = najdi_seznam_vozidel(data)
            zaznamenano = 0

            for idx, v in enumerate(vozidla):
                if not isinstance(v, dict):
                    continue
                vuz_id = str(
                    v.get("id") or v.get("reg") or v.get("rz") or v.get("vId")
                    or v.get("evc") or v.get("label") or v.get("num") or f"vuz_{idx}"
                )
                linka = str(v.get("line") or v.get("l") or v.get("route") or v.get("ln") or "")
                spoj = str(v.get("conn") or v.get("cn") or v.get("trip") or v.get("c") or "?")

                if linka:
                    c.execute("""
                        INSERT INTO odjete_spoje (datum, vozidlo, linka, spoj, zacatek, konec)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(datum, vozidlo, linka, spoj)
                        DO UPDATE SET konec = excluded.konec
                    """, (datum_str, vuz_id, linka, spoj, cas_str, cas_str))
                    zaznamenano += 1

            conn.commit()
            vyexportuj_turnusy_do_csv(conn)
            print(f"[{cas_str}] Cyklus {i+1}/{pocet_cyklu}: Zpracováno {zaznamenano} vozů.")

            if (i + 1) % ulozit_kazdych_cyklu == 0:
                uloz_na_github(f"Průběžná aktualizace turnusů ({cas_str})")

        except Exception as e:
            print(f"[{cas_str}] Chyba spojení: {e}")

        time.sleep(INTERVAL_SEKUND)

    uloz_na_github("Uložení po dokončení 6h bloku")
    conn.close()


if __name__ == "__main__":
    spust_automat()
