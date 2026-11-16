import os
import sys

# Pastikan console Windows mendukung karakter Unicode
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

import asyncio
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client
import httpx

# Load Environment Variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment variables")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
except Exception as e:
    print(f"Gagal inisialisasi Supabase Client: {e}")
    sys.exit(1)

# API di balik https://www.tslines.com/en/arrivaldate (Actual Sailing Schedule)
TSLINES_API = "https://epd.tslines.com/v1"

# Service yang diambil dari TS Lines. KCI adalah joint service (TS Lines, Interasia, Namsung):
# TS Lines memuat jadwal semua kapalnya, tapi jadwal kapal partner kadang tidak diperbarui.
TARGET_SERVICES = ["KCI"]

# Waktu TS Lines adalah waktu lokal pelabuhan. Jakarta = WIB (UTC+7).
JAKARTA_UTC_OFFSET = timedelta(hours=7)
# Toleransi tumpang-tindih antar pelabuhan sebelum rotasi dianggap tidak konsisten
OVERLAP_TOLERANCE = timedelta(hours=6)


def is_target_service(service: str) -> bool:
    if not service:
        return False
    s = service.upper().strip()
    return any(s == t or s.startswith(t) for t in TARGET_SERVICES)


def voyage_candidates(v: str) -> list:
    """Nomor voyage NPCT ("382N", "S046") dan varian tanpa / dengan nol di depan."""
    if not v:
        return []
    clean = re.sub(r'[^A-Z0-9]', '', v.upper())
    stripped = re.sub(r'^0+([1-9])', r'\1', clean)
    out = [clean, stripped]
    if stripped[:1].isdigit() and len(stripped) < 5:
        out.append(stripped.zfill(5))
    return list(dict.fromkeys(out))


def parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M")
    except ValueError:
        return None


def is_jakarta_port(name: str) -> bool:
    n = (name or "").upper()
    return any(k in n for k in ["JAKARTA", "TANJUNG PRIOK", "PRIOK"])


def format_port_title(name: str) -> str:
    # "Jakarta,Indonesia" -> "Jakarta"
    return " ".join(w.capitalize() for w in (name or "").split(',')[0].strip().split())


def build_rotation(docs: list, service: str):
    """
    Rotasi voyage dari respons ActSalingByVesselCode: hanya baris service target yang tidak
    di-omit, diurutkan menurut waktu (urutan baris TS Lines bukan urutan rute).
    Return: (ports, jakarta_arr_iso_utc, reason). ports kosong + reason kalau tidak dipakai.
    """
    stops = []
    for d in docs:
        if (d.get("SERVICE") or "").upper() != service.upper():
            continue
        if (d.get("STATUS") or "").lower() == "omit":
            continue
        eta, ata = parse_ts(d.get("ETA")), parse_ts(d.get("ATA"))
        etd, atd = parse_ts(d.get("ETD")), parse_ts(d.get("ATD"))
        arr = ata or eta
        dep = atd or etd or arr
        if not arr:
            continue
        stops.append({"port": d.get("PORT") or "", "arr": arr, "dep": max(dep, arr), "eta": eta})

    if not stops:
        return [], None, "tidak ada pelabuhan aktif untuk service ini"
    stops.sort(key=lambda s: s["arr"])

    # Jadwal yang tidak diperbarui membuat kapal "berada" di dua pelabuhan sekaligus.
    # Contoh AS CASPRIA 382N: Jakarta 9-15 Nov sekaligus Qingdao 12 Nov. Jangan dipakai.
    for prev, nxt in zip(stops, stops[1:]):
        if nxt["arr"] + OVERLAP_TOLERANCE < prev["dep"]:
            return [], None, (f"rotasi tidak konsisten ({format_port_title(prev['port'])} sampai "
                              f"{prev['dep']:%d/%m %H:%M}, {format_port_title(nxt['port'])} tiba "
                              f"{nxt['arr']:%d/%m %H:%M})")

    ports = []
    jakarta_arr_utc = None
    for s in stops:
        stay_h = round((s["dep"] - s["arr"]).total_seconds() / 3600.0, 1) or 24.0
        ports.append({
            "port": format_port_title(s["port"]).upper(),
            "port_name": format_port_title(s["port"]),
            "sequence": len(ports) + 1,
            "arr": s["arr"].strftime("%Y-%m-%d"),
            "dep": s["dep"].strftime("%Y-%m-%d"),
            "avg_port_stay_hours": stay_h,
        })
        if is_jakarta_port(s["port"]) and not jakarta_arr_utc and s["eta"]:
            # Pakai ETA (jadwal liner), bukan ATA, sebagai liner_eta
            jakarta_arr_utc = (s["eta"] - JAKARTA_UTC_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ports, jakarta_arr_utc, None


async def scrape_tslines():
    print("Mulai scraping Route & ETA Liner dari TS Lines...")

    res = supabase.table('vessel_schedules') \
        .select('id, voyage, service, liner_eta, master_vessels(vessel_name, imo_number)') \
        .neq('status', 'Departed') \
        .is_('actual_atb', 'null') \
        .execute()

    schedules = [s for s in (res.data or []) if is_target_service(s.get('service'))]
    if not schedules:
        print(f"Tidak ada jadwal dengan service TS Lines ({', '.join(TARGET_SERVICES)}) yang perlu diperbarui.")
        return

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": "https://www.tslines.com/",
        "Origin": "https://www.tslines.com",
    }

    updated = 0
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0)) as client:
        try:
            r = await client.get(f"{TSLINES_API}/VesselCode")
            r.raise_for_status()
            codes = {(d.get("VESSELNAME") or "").strip().upper(): d.get("VESSELCODE") for d in r.json().get("docs", [])}
            print(f"Berhasil memuat {len(codes)} kapal dari TS Lines.")
        except Exception as e:
            print(f"Gagal memuat daftar kapal TS Lines: {e}")
            return

        for sch in schedules:
            vessel_name = ((sch.get('master_vessels') or {}).get('vessel_name') or '').strip()
            voyage = sch.get('voyage')
            service = (sch.get('service') or '').strip()
            code = codes.get(vessel_name.upper())
            if not code:
                print(f"  - {vessel_name} {voyage}: tidak ada di daftar kapal TS Lines, skip.")
                continue

            docs = []
            for voy in voyage_candidates(voyage):
                try:
                    rr = await client.post(f"{TSLINES_API}/ActSalingByVesselCode", json={"VESSEL_CODE": code, "VOYAGE": voy})
                    rr.raise_for_status()
                    docs = rr.json().get("docs") or []
                except Exception as e:
                    print(f"  - Error fetch jadwal TS Lines {vessel_name} {voy}: {e}")
                await asyncio.sleep(1)
                if docs:
                    break

            if not docs:
                print(f"  - {vessel_name} {voyage} [{service}]: voyage tidak ditemukan di TS Lines.")
                continue

            ports, jkt_eta, reason = build_rotation(docs, service)
            if not ports:
                print(f"  - {vessel_name} {voyage} [{service}]: dilewati, {reason}.")
                continue

            update = {'liner_route': ports}
            if jkt_eta:
                update['liner_eta'] = jkt_eta
            try:
                supabase.table('vessel_schedules').update(update).eq('id', sch['id']).execute()
                updated += 1
                print(f"  + {vessel_name} {voyage} [{service}] ETA Jakarta: {jkt_eta or 'N/A'} ({len(ports)} ports)")
            except Exception as e:
                print(f"  - Gagal update Supabase untuk {vessel_name} {voyage}: {e}")

    print(f"\nSelesai! {updated} jadwal diperbarui dari TS Lines.")


if __name__ == "__main__":
    asyncio.run(scrape_tslines())
