import os
import sys

# Pastikan console Windows mendukung karakter Unicode
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

import asyncio
import re
from datetime import datetime, timedelta, timezone
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

MAERSK_BASE = "https://api.maersk.com/synergy/schedules"
MAERSK_CONSUMER_KEY = "uXe7bxTHLY0yY0e8jnS6kotShkLuAAqG"
MAERSK_CARRIER_CODES = "MAEU,SEAU,SEJJ"

# Service Maersk yang diambil jadwalnya
TARGET_SERVICES = ["IN1", "JKF", "IA1", "IA8", "IA15", "I15", "IA4"]

# Fallback mapping jika pencarian dinamis terkendala
STATIC_VESSEL_CODES = {
    "9294537": "82V",  # AS PENELOPE
    "1034400": "KA8",  # SKY PEACE
    "9300142": "MI0",  # BIG BREEZY
    "9301445": "21C",  # MARSA PRIDE
    "9232412": "19X",  # SPIL NISAKA
    "9294018": "C10",  # JULIUS-S
    "9650066": "EZ5",  # ERASMUS LEO
    "9273947": "GF2",  # NIKEN
    "9322504": "L1T",  # IRENES RAY
}


def is_target_service(service: str) -> bool:
    if not service:
        return False
    s = service.upper().strip()
    return any(t == s or t in s for t in TARGET_SERVICES)


def normalize_voyage(v: str) -> str:
    if not v:
        return ""
    clean = re.sub(r'[^A-Z0-9]', '', v.upper())
    # Hapus awalan angka nol jika ada (e.g. 0640S -> 640S)
    clean = re.sub(r'^0+([1-9])', r'\1', clean)
    return clean


def format_port_title(name: str) -> str:
    if not name:
        return ""
    return " ".join(w.capitalize() for w in name.strip().split())


def is_jakarta_port(name: str, un_loc: str = "") -> bool:
    if un_loc and un_loc.upper() == "IDJKT":
        return True
    if not name:
        return False
    n = name.upper()
    return any(k in n for k in ["JAKARTA", "TANJUNG PRIOK", "PRIOK"])


async def fetch_active_vessels(client: httpx.AsyncClient) -> tuple[dict, dict]:
    """
    Mengambil daftar kapal aktif dari Maersk Synergy API.
    Return: (imo_to_code, name_to_code)
    """
    url = f"{MAERSK_BASE}/active-vessels"
    params = {"carrierCodes": MAERSK_CARRIER_CODES}
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json().get("vessels", [])

    imo_map = {}
    name_map = {}
    for v in data:
        code = v.get("vesselMaerskCode")
        imo = str(v.get("vesselIMONumber") or "").strip()
        vname = (v.get("vesselName") or "").strip().upper()
        if code:
            if imo and imo != "0":
                imo_map[imo] = code
            if vname:
                name_map[vname] = code
                # Clean name without punctuation
                clean_name = re.sub(r'[^A-Z0-9 ]', '', vname)
                name_map[clean_name] = code

    return imo_map, name_map


def parse_schedule_stops(stops: list, target_voyage: str) -> tuple[list, str | None]:
    """
    Filter dan parse stops dari Maersk schedule yang cocok dengan target_voyage.
    Return: (ports, jakarta_arr_iso_utc)
    """
    norm_target = normalize_voyage(target_voyage)
    matching_stops = []
    for s in stops:
        in_v = normalize_voyage(s.get("arrivalVoyageNumber"))
        out_v = normalize_voyage(s.get("departureVoyageNumber"))
        if in_v == norm_target or out_v == norm_target:
            matching_stops.append(s)

    if not matching_stops:
        return [], None

    ports = []
    jakarta_arr_utc = None

    for s in matching_stops:
        p_name = s.get("portName") or s.get("cityName") or ""
        un_loc = s.get("unLocationCode") or ""
        arr_str = s.get("arrivalTime")
        dep_str = s.get("departureTime")

        arr_date = None
        dep_date = None
        stay_h = 24.0

        if arr_str:
            try:
                arr_dt = datetime.fromisoformat(arr_str)
                arr_date = arr_dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        if dep_str:
            try:
                dep_dt = datetime.fromisoformat(dep_str)
                dep_date = dep_dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        if arr_str and dep_str:
            try:
                diff_sec = (dep_dt - arr_dt).total_seconds()
                stay_h = round(diff_sec / 3600.0, 1) if diff_sec > 0 else 24.0
            except Exception:
                pass
        elif not arr_date and dep_date:
            arr_date = dep_date
        elif not dep_date and arr_date:
            dep_date = arr_date

        port_title = format_port_title(p_name)
        ports.append({
            "port": p_name.upper(),
            "port_name": port_title,
            "sequence": len(ports) + 1,
            "arr": arr_date,
            "dep": dep_date,
            "avg_port_stay_hours": stay_h,
        })

        if is_jakarta_port(p_name, un_loc):
            if arr_str:
                try:
                    arr_dt = datetime.fromisoformat(arr_str)
                    jakarta_arr_utc = arr_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    jakarta_arr_utc = f"{arr_date}T05:00:00Z" if arr_date else None
            elif arr_date:
                jakarta_arr_utc = f"{arr_date}T05:00:00Z"

    return ports, jakarta_arr_utc


async def scrape_maersk():
    print("Mulai scraping Route & ETA Liner dari Maersk...")

    res = supabase.table('vessel_schedules') \
        .select('id, voyage, service, liner_eta, master_vessels(vessel_name, imo_number)') \
        .neq('status', 'Departed') \
        .execute()

    schedules = [s for s in (res.data or []) if is_target_service(s.get('service'))]

    if not schedules:
        print("Tidak ada jadwal dengan service Maersk (IN1, JKF, IA1, IA8, IA15, I15, IA4) yang perlu diperbarui.")
        return

    headers = {
        "Accept": "application/json",
        "Consumer-Key": MAERSK_CONSUMER_KEY,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": "https://www.maersk.com/schedules/vesselSchedules",
        "Origin": "https://www.maersk.com",
    }

    now = datetime.now()
    from_date = (now - timedelta(days=21)).strftime("%Y-%m-%d")
    to_date = (now + timedelta(days=45)).strftime("%Y-%m-%d")

    updated = 0
    cache = {}  # vessel_code -> raw schedule stops list

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20.0)) as client:
        # 1. Dapatkan mapping kapal ke vesselMaerskCode
        try:
            imo_map, name_map = await fetch_active_vessels(client)
            print(f"Berhasil memuat {len(imo_map)} data kapal aktif dari Maersk.")
        except Exception as e:
            print(f"Peringatan: Gagal memuat active-vessels ({e}), menggunakan fallback static mapping.")
            imo_map = {}
            name_map = {}

        # 2. Proses tiap jadwal kapal
        for sch in schedules:
            vessel_meta = sch.get('master_vessels') or {}
            vessel_name = (vessel_meta.get('vessel_name') or '').strip()
            imo = str(vessel_meta.get('imo_number') or '').strip()
            voyage = sch.get('voyage')
            service = sch.get('service')

            # Cari kode Maersk berdasarkan IMO -> Nama Kapal -> Static fallback
            vessel_code = (
                STATIC_VESSEL_CODES.get(imo)
                or imo_map.get(imo)
                or name_map.get(vessel_name.upper())
                or name_map.get(re.sub(r'[^A-Z0-9 ]', '', vessel_name.upper()))
            )

            if not vessel_code:
                print(f"  - {vessel_name} (IMO: {imo}): tidak ditemukan di Maersk, skip.")
                continue

            # Ambil jadwal dari API jika belum dicache
            if vessel_code not in cache:
                try:
                    params = {
                        "vesselMaerskCode": vessel_code,
                        "fromDate": from_date,
                        "toDate": to_date,
                        "carrierCodes": MAERSK_CARRIER_CODES,
                    }
                    r = await client.get(f"{MAERSK_BASE}/vessel-schedules", params=params)
                    r.raise_for_status()
                    cache[vessel_code] = r.json().get("vesselSchedules", [])
                except Exception as e:
                    print(f"  - Error fetch jadwal {vessel_name} ({vessel_code}): {e}")
                    cache[vessel_code] = []
                await asyncio.sleep(1)

            stops = cache.get(vessel_code, [])
            ports, jkt_eta = parse_schedule_stops(stops, voyage)

            if not ports:
                print(f"  - {vessel_name} {voyage} [{service}]: voyage tidak ditemukan di jadwal Maersk ({len(stops)} stops).")
                continue

            update = {'liner_route': ports}
            if jkt_eta:
                update['liner_eta'] = jkt_eta

            try:
                try:
                    supabase.table('vessel_schedules').update(update).eq('id', sch['id']).execute()
                except Exception:
                    # Fallback jika kolom liner_route belum ada
                    update.pop('liner_route', None)
                    supabase.table('vessel_schedules').update(update).eq('id', sch['id']).execute()

                updated += 1
                eta_display = jkt_eta if jkt_eta else 'N/A'
                print(f"  + {vessel_name} {voyage} [{service}] ETA Jakarta: {eta_display} ({len(ports)} ports)")
            except Exception as e:
                print(f"  - Gagal update Supabase untuk {vessel_name} {voyage}: {e}")

    print(f"\nSelesai! {updated} jadwal diperbarui dari Maersk.")


if __name__ == "__main__":
    asyncio.run(scrape_maersk())
