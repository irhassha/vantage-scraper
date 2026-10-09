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

SITC_API_URL = "https://api.sitcline.com/svl/voyageInfo/rate"

# Service SITC yang diambil jadwalnya
TARGET_SERVICES = ["CMI", "CMI2", "JTH"]


def is_target_service(service: str) -> bool:
    if not service:
        return False
    s = service.strip().upper()
    return any(s == t or s.startswith(t) for t in TARGET_SERVICES)


def normalize_voyage(v: str) -> str:
    if not v:
        return ""
    clean = re.sub(r'[^A-Z0-9]', '', v.upper())
    clean = re.sub(r'^0+([1-9])', r'\1', clean)
    return clean


def format_port_title(name: str) -> str:
    if not name:
        return ""
    # Handle nama pelabuhan dengan tanda kurung e.g. KUANTAN (TANJONG GELANG)
    return " ".join(w.capitalize() if not w.startswith('(') else '(' + w[1:].capitalize() for w in name.strip().split())


def is_jakarta_port(name: str, un_loc: str = "") -> bool:
    if un_loc and un_loc.upper() == "IDJKT":
        return True
    if not name:
        return False
    n = name.upper()
    return any(k in n for k in ["JAKARTA", "TANJUNG PRIOK", "PRIOK"])


async def fetch_vessel_schedule(client: httpx.AsyncClient, vessel_name: str, year: int, month: int) -> list:
    """
    Ambil daftar voyage plan untuk kapal tertentu pada bulan/tahun dari SITC API.
    """
    params = {
        "vesselName": vessel_name,
        "year": str(year),
        "month": str(month)
    }
    resp = await client.post(SITC_API_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") == 1 and data.get("data") and data["data"].get("resultList"):
        return data["data"]["resultList"]
    return []


def parse_sitc_stops(ports_data: list) -> tuple[list, str | None]:
    """
    Parse rotasi pelabuhan dari SITC voyagePlanPort.
    Return: (ports, jakarta_arr_iso_utc)
    """
    ports = []
    jakarta_arr_utc = None

    for p in ports_data:
        p_name = (p.get("PORT_NAME") or "").strip()
        p_code = (p.get("PORT_CODE") or "").strip()

        arr_raw = p.get("ATA") or p.get("ETA")
        dep_raw = p.get("ATD") or p.get("ETD")

        arr_date = None
        dep_date = None
        arr_dt = None
        dep_dt = None
        stay_h = 24.0

        if arr_raw:
            try:
                arr_dt = datetime.strptime(arr_raw, "%Y-%m-%d %H:%M:%S")
                arr_date = arr_dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        if dep_raw:
            try:
                dep_dt = datetime.strptime(dep_raw, "%Y-%m-%d %H:%M:%S")
                dep_date = dep_dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        if arr_dt and dep_dt:
            diff_sec = (dep_dt - arr_dt).total_seconds()
            stay_h = round(diff_sec / 3600.0, 1) if diff_sec > 0 else 24.0
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

        if is_jakarta_port(p_name, p_code):
            # SITC melaporkan waktu lokal pelabuhan (Jakarta = WIB = UTC+7)
            if arr_dt:
                dt_utc = arr_dt - timedelta(hours=7)
                jakarta_arr_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            elif arr_date:
                jakarta_arr_utc = f"{arr_date}T05:00:00Z"

    return ports, jakarta_arr_utc


async def scrape_sitc():
    print("Mulai scraping Route & ETA Liner dari SITC...")

    res = supabase.table('vessel_schedules') \
        .select('id, voyage, service, liner_eta, master_vessels(vessel_name, imo_number)') \
        .neq('status', 'Departed') \
        .execute()

    schedules = [s for s in (res.data or []) if is_target_service(s.get('service'))]

    if not schedules:
        print("Tidak ada jadwal dengan service SITC (CMI, CMI2, JTH) yang perlu diperbarui.")
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }

    now = datetime.now()
    # Rentang pencarian: bulan lalu, bulan ini, bulan depan
    months_to_query = []
    for offset in [-1, 0, 1]:
        m = now.month + offset
        y = now.year
        if m < 1:
            m += 12
            y -= 1
        elif m > 12:
            m -= 12
            y += 1
        months_to_query.append((y, m))

    updated = 0
    vessel_cache = {}  # vessel_name -> list of all voyage objects

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20.0)) as client:
        for sch in schedules:
            vessel_meta = sch.get('master_vessels') or {}
            vessel_name = (vessel_meta.get('vessel_name') or '').strip()
            voyage = sch.get('voyage')
            service = sch.get('service')

            if not vessel_name:
                continue

            # Ambil data jadwal kapal dari SITC jika belum ada di cache
            if vessel_name not in vessel_cache:
                all_voyages = []
                for y, m in months_to_query:
                    try:
                        v_list = await fetch_vessel_schedule(client, vessel_name, y, m)
                        all_voyages.extend(v_list)
                    except Exception as e:
                        print(f"  - Error fetch jadwal {vessel_name} ({y}-{m:02d}): {e}")
                vessel_cache[vessel_name] = all_voyages

            available_voyages = vessel_cache[vessel_name]
            if not available_voyages:
                print(f"  - {vessel_name} (voyage {voyage}): tidak ada jadwal ditemukan di SITC.")
                continue

            # Matching target voyage
            norm_target = normalize_voyage(voyage)
            matched_voyage = None

            # 1. Exact match (e.g. 2628S == 2628S)
            for v in available_voyages:
                v_code = f"{v.get('voyageNo', '')}{v.get('voyageLeg', '')}"
                if normalize_voyage(v_code) == norm_target:
                    matched_voyage = v
                    break

            # 2. Number-only match fallback (e.g. 2625S -> 2625N atau sebaliknya jika di NPCT1 beda arah)
            if not matched_voyage:
                num_target = re.sub(r'[^0-9]', '', norm_target)
                cand_matches = []
                for v in available_voyages:
                    v_num = re.sub(r'[^0-9]', '', str(v.get('voyageNo', '')))
                    if v_num and v_num == num_target:
                        cand_matches.append(v)
                
                if cand_matches:
                    # Prioritaskan yang memuat pelabuhan Jakarta
                    with_jkt = [
                        c for c in cand_matches
                        if any(is_jakarta_port(p.get('PORT_NAME', ''), p.get('PORT_CODE', '')) for p in c.get('voyagePlanPort', []))
                    ]
                    matched_voyage = with_jkt[0] if with_jkt else cand_matches[0]

            if not matched_voyage:
                avail_names = [f"{v.get('voyageNo')}{v.get('voyageLeg')}" for v in available_voyages]
                print(f"  - {vessel_name} (voyage {voyage}): tidak cocok dengan voyage SITC {avail_names}.")
                continue

            ports_data = matched_voyage.get("voyagePlanPort", [])
            ports, jakarta_arr_utc = parse_sitc_stops(ports_data)

            if not ports:
                print(f"  - {vessel_name} (voyage {voyage}): daftar stop kosong.")
                continue

            # Siapkan payload update
            update_payload = {"liner_route": ports}
            if jakarta_arr_utc:
                update_payload["liner_eta"] = jakarta_arr_utc

            try:
                supabase.table('vessel_schedules') \
                    .update(update_payload) \
                    .eq('id', sch['id']) \
                    .execute()

                matched_code = f"{matched_voyage.get('serviceLineCode')} {matched_voyage.get('voyageNo')}{matched_voyage.get('voyageLeg')}"
                print(f"  ✓ {vessel_name} ({voyage} -> {matched_code}): {len(ports)} ports, liner_eta={jakarta_arr_utc or '-'}")
                updated += 1
            except Exception as e:
                print(f"  - Gagal update {vessel_name} ({sch['id']}): {e}")

    print(f"Selesai SITC: {updated}/{len(schedules)} jadwal berhasil diperbarui.")


if __name__ == "__main__":
    asyncio.run(scrape_sitc())
