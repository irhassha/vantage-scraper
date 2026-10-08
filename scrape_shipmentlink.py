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
from bs4 import BeautifulSoup

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

SL_BASE = "https://ss.shipmentlink.com/tvs2/jsp"
SL_VESSEL_LIST_URL = f"{SL_BASE}/TVS2_QueryVessel.jsp"
SL_SCHEDULE_URL = f"{SL_BASE}/TVS2_QueryScheduleByVessel.jsp"

# Service Evergreen yang diambil jadwalnya dari ShipmentLink
TARGET_SERVICES = ["JPI-A", "JPI-B", "CIT", "CIM"]


def is_target_service(service: str) -> bool:
    if not service:
        return False
    s = service.upper()
    return any(t in s for t in TARGET_SERVICES)


def infer_date(mm_dd: str, ref: datetime) -> datetime:
    """Ubah 'MM/DD' menjadi tanggal lengkap dengan tahun terdekat dari tanggal referensi."""
    month, day = (int(x) for x in mm_dd.split('/'))
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = datetime(year, month, day)
        except ValueError:
            continue
        if best is None or abs(cand - ref) < abs(best - ref):
            best = cand
    return best


def format_port_title(name: str) -> str:
    """Format nama pelabuhan ke Title Case (contoh: KAOHSIUNG -> Kaohsiung)."""
    if not name:
        return ""
    return " ".join(w.capitalize() for w in name.strip().split())


def parse_schedule_html(html: str, ref: datetime) -> list:
    """
    Parse halaman TVS2_QueryScheduleByVessel.jsp.
    Return list voyage: {voyage, service, ports: [{port, port_name, sequence, arr, dep, avg_port_stay_hours}]}
    """
    soup = BeautifulSoup(html, 'html.parser')
    results = []

    for span in soup.find_all('span', class_='f12wrdb2'):
        voyage_text = span.get_text(" ", strip=True)  # contoh: "EVER OBEY 0496-058S" atau "YM CENTENNIAL 071S"
        tokens = voyage_text.split()
        if not tokens:
            continue
        voyage = tokens[-1]

        line_span = span.find_next_sibling('span', class_='f12wrdn2')
        line_text = line_span.get_text(" ", strip=True) if line_span else ''
        sm = re.search(r'\(([^)]+)\)\s*$', line_text)
        service = sm.group(1).strip() if sm else line_text.replace('LINE:', '').strip()

        table = span.find_next('table', class_='f12rowb3')
        if not table:
            continue
        rows = table.find_all('tr')
        # Cari baris header (nama pelabuhan) dan baris ARR/DEP
        port_names, time_cells = [], []
        for tr in rows:
            tds = tr.find_all('td', recursive=False)
            if tr.find('td', class_='f09rown1'):
                time_cells = tr.find_all('td', class_='f09rown2')
            elif tr.get('class') and 'f09tilb1' in tr.get('class'):
                port_names = [td.get_text(strip=True) for td in tds[1:]]

        ports = []
        for name, cell in zip(port_names, time_cells):
            dates = re.findall(r'\d{2}/\d{2}', cell.get_text(" ", strip=True))
            if not dates:
                continue
            arr = infer_date(dates[0], ref)
            dep = infer_date(dates[1], ref) if len(dates) > 1 else arr
            diff_days = (dep - arr).days
            stay_h = float(diff_days * 24) if diff_days > 0 else 24.0
            ports.append({
                'port': name,
                'port_name': format_port_title(name),
                'sequence': len(ports) + 1,
                'arr': arr.strftime('%Y-%m-%d'),
                'dep': dep.strftime('%Y-%m-%d'),
                'avg_port_stay_hours': stay_h,
            })

        results.append({'voyage': voyage, 'service': service, 'ports': ports})

    return results


def normalize_voyage(v: str) -> str:
    return re.sub(r'\s+', '', (v or '')).upper()


def date_to_utc_iso(date_str: str) -> str:
    """Hanya tanggal yang tersedia dari liner; pakai 12:00 WIB (05:00 UTC) agar tanggal aman di WIB/UTC."""
    return f"{date_str}T05:00:00Z"


async def fetch_vessel_codes(client: httpx.AsyncClient) -> dict:
    resp = await client.post(SL_VESSEL_LIST_URL, data={'vslCode': ''})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    codes = {}
    for opt in soup.find_all('option'):
        code = (opt.get('value') or '').strip()
        name = opt.get_text(strip=True).upper()
        if code:
            codes[name] = code
    return codes


async def scrape_shipmentlink():
    print("Mulai scraping Route & ETA Liner dari ShipmentLink...")

    res = supabase.table('vessel_schedules') \
        .select('id, voyage, service, liner_eta, master_vessels(vessel_name)') \
        .neq('status', 'Departed') \
        .execute()
    schedules = [s for s in (res.data or []) if is_target_service(s.get('service'))]

    if not schedules:
        print("Tidak ada jadwal dengan service JPI-A/JPI-B/CIT/CIM yang perlu diperbarui.")
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    }
    now = datetime.now()
    updated = 0
    cache = {}  # vessel_name -> hasil parse

    async with httpx.AsyncClient(headers=headers, verify=False, timeout=httpx.Timeout(30.0)) as client:
        try:
            vessel_codes = await fetch_vessel_codes(client)
        except Exception as e:
            print(f"Gagal mengambil daftar kapal ShipmentLink: {e}")
            return

        for sch in schedules:
            vessel_name = ((sch.get('master_vessels') or {}).get('vessel_name') or '').strip()
            voyage = sch.get('voyage')
            code = vessel_codes.get(vessel_name.upper())
            if not code:
                print(f"  - {vessel_name}: tidak ada di ShipmentLink (bukan kapal Evergreen?), skip.")
                continue

            if vessel_name not in cache:
                try:
                    r = await client.post(SL_SCHEDULE_URL, data={'vslCode': code, 'vslName': vessel_name})
                    r.raise_for_status()
                    cache[vessel_name] = parse_schedule_html(r.text, now)
                except Exception as e:
                    print(f"  - Error fetch jadwal {vessel_name}: {e}")
                    cache[vessel_name] = []
                await asyncio.sleep(2)

            match = next((v for v in cache[vessel_name]
                          if normalize_voyage(v['voyage']) == normalize_voyage(voyage)), None)
            if not match:
                print(f"  - {vessel_name} {voyage}: voyage tidak ditemukan di ShipmentLink.")
                continue

            jkt = next((p for p in match['ports'] if 'JAKARTA' in p['port'].upper()), None)
            update = {'liner_route': match['ports']}
            if jkt:
                update['liner_eta'] = date_to_utc_iso(jkt['arr'])

            try:
                try:
                    supabase.table('vessel_schedules').update(update).eq('id', sch['id']).execute()
                except Exception:
                    # Fallback jika kolom liner_route belum dibuat (lihat migrations/add_liner_route.sql)
                    update.pop('liner_route', None)
                    supabase.table('vessel_schedules').update(update).eq('id', sch['id']).execute()
                updated += 1
                print(f"  + {vessel_name} {voyage} [{match['service']}] ETA Jakarta: {jkt['arr'] if jkt else 'N/A'} "
                      f"({len(match['ports'])} port)")
            except Exception as e:
                print(f"  - Gagal update {vessel_name} {voyage}: {e}")

    print(f"\nSelesai! {updated} jadwal diperbarui dari ShipmentLink.")


if __name__ == "__main__":
    asyncio.run(scrape_shipmentlink())
