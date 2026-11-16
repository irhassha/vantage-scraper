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

# Endpoint yang dipakai halaman https://www.msc.com/en/search-a-schedule (tab Vessel)
MSC_SCHEDULE_URL = "https://www.msc.com/api/feature/tools/SearchLiveSchedule"

# Service MSC yang diambil jadwalnya
TARGET_SERVICES = ["PERTIWI"]

# Waktu di jadwal MSC adalah waktu lokal pelabuhan (tanpa zona). Jakarta = WIB (UTC+7).
JAKARTA_UTC_OFFSET = timedelta(hours=7)


def is_target_service(service: str) -> bool:
    if not service:
        return False
    s = service.upper().strip()
    return any(s == t or s.startswith(t) for t in TARGET_SERVICES)


def normalize_voyage(v: str) -> str:
    if not v:
        return ""
    clean = re.sub(r'[^A-Z0-9]', '', v.upper())
    return re.sub(r'^0+([1-9])', r'\1', clean)


def format_port_title(name: str) -> str:
    if not name:
        return ""
    # "JAKARTA, JAVA" -> "Jakarta"
    return " ".join(w.capitalize() for w in name.split(',')[0].strip().split())


def is_jakarta_port(name: str, un_loc: str = "") -> bool:
    if un_loc and un_loc.upper() in ["IDJKT", "IDJTK"]:
        return True
    if not name:
        return False
    n = name.upper()
    return any(k in n for k in ["JAKARTA", "TANJUNG PRIOK", "PRIOK"])


def parse_local(raw: str):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_msc_stops(stops: list, target_voyage: str) -> tuple[list, str | None]:
    """
    Ambil pelabuhan voyage target dari respons SearchLiveSchedule MSC.
    Setiap baris: PortName, PortIsoCode, LiveArrivalDate, LiveDepartureDate (waktu lokal),
    DepartureVoyageNo. Return: (ports, jakarta_arr_iso_utc)
    """
    norm_target = normalize_voyage(target_voyage)
    ports = []
    jakarta_arr_utc = None

    for s in stops:
        if normalize_voyage(s.get("DepartureVoyageNo")) != norm_target:
            continue
        p_name = s.get("PortName") or ""
        un_loc = s.get("PortIsoCode") or ""
        arr_dt = parse_local(s.get("LiveArrivalDate"))
        dep_dt = parse_local(s.get("LiveDepartureDate"))
        arr_dt = arr_dt or dep_dt
        dep_dt = dep_dt or arr_dt

        stay_h = 24.0
        if arr_dt and dep_dt and dep_dt > arr_dt:
            stay_h = round((dep_dt - arr_dt).total_seconds() / 3600.0, 1)

        ports.append({
            "port": (p_name.split(',')[0].strip() or un_loc).upper(),
            "port_name": format_port_title(p_name),
            "sequence": len(ports) + 1,
            "arr": arr_dt.strftime("%Y-%m-%d") if arr_dt else None,
            "dep": dep_dt.strftime("%Y-%m-%d") if dep_dt else None,
            "avg_port_stay_hours": stay_h,
        })

        if is_jakarta_port(p_name, un_loc) and arr_dt and not jakarta_arr_utc:
            jakarta_arr_utc = (arr_dt - JAKARTA_UTC_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")

    return ports, jakarta_arr_utc


async def scrape_msc():
    print("Mulai scraping Route & ETA Liner dari MSC...")

    res = supabase.table('vessel_schedules') \
        .select('id, voyage, service, liner_eta, master_vessels(vessel_name, imo_number)') \
        .neq('status', 'Departed') \
        .is_('actual_atb', 'null') \
        .execute()

    schedules = [s for s in (res.data or []) if is_target_service(s.get('service'))]

    if not schedules:
        print(f"Tidak ada jadwal dengan service MSC ({', '.join(TARGET_SERVICES)}) yang perlu diperbarui.")
        return

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": "https://www.msc.com/en/search-a-schedule",
        "Origin": "https://www.msc.com",
    }

    updated = 0
    cache = {}  # imo -> stops

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0)) as client:
        for sch in schedules:
            vessel_meta = sch.get('master_vessels') or {}
            vessel_name = (vessel_meta.get('vessel_name') or '').strip()
            imo = str(vessel_meta.get('imo_number') or '').strip()
            voyage = sch.get('voyage')
            service = sch.get('service')

            if not imo or not imo.isdigit():
                print(f"  - {vessel_name} {voyage}: IMO belum ada, skip.")
                continue

            if imo not in cache:
                try:
                    r = await client.post(MSC_SCHEDULE_URL, json={"VesselIMONumber": imo, "language": "en"})
                    r.raise_for_status()
                    body = r.json()
                    cache[imo] = (body.get("Data") or []) if body.get("IsSuccess") else []
                except Exception as e:
                    print(f"  - Error fetch jadwal MSC {vessel_name} (IMO {imo}): {e}")
                    cache[imo] = []
                await asyncio.sleep(1)

            stops = cache[imo]
            ports, jkt_eta = parse_msc_stops(stops, voyage)

            if not ports:
                print(f"  - {vessel_name} {voyage} [{service}]: voyage tidak ditemukan di jadwal MSC ({len(stops)} pelabuhan).")
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

    print(f"\nSelesai! {updated} jadwal diperbarui dari MSC.")


if __name__ == "__main__":
    asyncio.run(scrape_msc())
