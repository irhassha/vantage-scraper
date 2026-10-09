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

HMM_MAIN_URL = "https://www.hmm21.com/e-service/general/schedule/ScheduleMain.do"
HMM_OPTIONS_URL = "https://www.hmm21.com/e-service/general/schedule/selectScheduleOptionJson.do"
HMM_SCHEDULE_URL = "https://www.hmm21.com/e-service/general/schedule/selectByVesselPagingList.do"

# Service HMM yang diambil jadwalnya
TARGET_SERVICES = ["KIS"]


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
    # Hapus suffix negara e.g. ", KOREA", ", INDONESIA", ", CHINA"
    clean = re.sub(r',\s*[A-Z\s]+$', '', name.strip())
    return " ".join(w.capitalize() if not w.startswith('(') else '(' + w[1:].capitalize() for w in clean.split())


def is_jakarta_port(name: str, code: str = "") -> bool:
    if code and code.upper() == "IDJKT":
        return True
    if not name:
        return False
    n = name.upper()
    return any(k in n for k in ["JAKARTA", "TANJUNG PRIOK", "PRIOK", "IDJKT"])


async def scrape_hmm():
    print("Mulai scraping Route & ETA Liner dari HMM (Service KIS)...")

    res = supabase.table('vessel_schedules') \
        .select('id, voyage, service, liner_eta, master_vessels(vessel_name, imo_number)') \
        .neq('status', 'Departed') \
        .execute()

    schedules = [s for s in (res.data or []) if is_target_service(s.get('service'))]

    if not schedules:
        print("Tidak ada jadwal dengan service HMM (KIS) yang perlu diperbarui.")
        return

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }

    updated = 0

    async with httpx.AsyncClient(headers=base_headers, timeout=25.0, follow_redirects=True) as client:
        # 1. Buka ScheduleMain.do untuk mendapatkan session cookies dan CSRF token
        try:
            r_page = await client.get(HMM_MAIN_URL)
            r_page.raise_for_status()
            soup = BeautifulSoup(r_page.text, "html.parser")
            csrf_token = soup.find("meta", {"name": "_csrf"}).get("content")
            csrf_header = soup.find("meta", {"name": "_csrf_header"}).get("content")
        except Exception as e:
            print(f"Gagal inisialisasi sesi HMM / CSRF token: {e}")
            return

        post_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            csrf_header: csrf_token,
            "Content-Type": "application/json; charset=UTF-8",
            "Referer": HMM_MAIN_URL,
            "Origin": "https://www.hmm21.com",
        }

        # 2. Dapatkan mapping nama kapal ke kode kapal HMM (optCd)
        vessel_name_to_code = {}
        try:
            r_opt = await client.post(HMM_OPTIONS_URL, json={}, headers=post_headers)
            if r_opt.status_code == 200:
                opt_data = r_opt.json()
                for item in (opt_data.get("RTN_JSON1") or []):
                    code = item.get("optCd")
                    name = item.get("optNm")
                    if code and name:
                        clean_name = re.sub(r'[^A-Z0-9]', '', name.upper())
                        vessel_name_to_code[clean_name] = code
                        vessel_name_to_code[name.upper().strip()] = code
            print(f"Berhasil memuat {len(vessel_name_to_code)} mapping kapal dari HMM.")
        except Exception as e:
            print(f"Peringatan: Gagal load selectScheduleOptionJson ({e}), menggunakan static mapping.")

        # Static fallback mapping untuk service KIS jika options gagal
        static_code_map = {
            "MARINA ONE": "HTMR",
            "SEABREEZE": "HTZE",
            "HMM HARVEST": "HHHV",
        }

        # 3. Proses tiap jadwal
        for sch in schedules:
            vessel_meta = sch.get('master_vessels') or {}
            vessel_name = (vessel_meta.get('vessel_name') or '').strip()
            voyage = (sch.get('voyage') or '').strip()
            service = sch.get('service')

            clean_name = re.sub(r'[^A-Z0-9]', '', vessel_name.upper())
            vsl_cd = vessel_name_to_code.get(clean_name) or vessel_name_to_code.get(vessel_name.upper()) or static_code_map.get(vessel_name.upper())

            if not vsl_cd:
                print(f"  - {vessel_name}: kode kapal HMM tidak ditemukan, skip.")
                continue

            # Bersihkan target voyage
            clean_voyage = re.sub(r'[^A-Z0-9]', '', voyage.upper())
            voy_num = re.sub(r'[^0-9]', '', clean_voyage)

            # Query jadwal kapal
            # Coba query dengan vvdCd terisi (misal HTMR0004), lalu fallback tanpa filter vvdCd
            candidates = [f"{vsl_cd}{voy_num.zfill(4)[:4]}", ""]
            board_list = []

            for cand_vvd in candidates:
                payload = {
                    "page": 1,
                    "srchByVesselVslCd": vsl_cd,
                    "vvdCd": cand_vvd,
                }
                try:
                    r_sched = await client.post(HMM_SCHEDULE_URL, json=payload, headers=post_headers)
                    if r_sched.status_code == 200:
                        b_list = r_sched.json().get("boardList") or []
                        if b_list:
                            board_list = b_list
                            break
                except Exception as e:
                    print(f"  - Error query HMM schedule ({vsl_cd}, {cand_vvd}): {e}")

            if not board_list:
                print(f"  - {vessel_name} (voyage {voyage}): jadwal tidak ditemukan di HMM.")
                continue

            # Filter/match stops yang sesuai dengan target voyage
            # Format ltdvvd di HMM: misal HTMR0004S atau HTMR0004N
            norm_target = normalize_voyage(voyage)
            target_num = re.sub(r'[^0-9]', '', norm_target)

            matched_stops = []
            # 1. Cek stop yang ltdvvd-nya cocok dengan target_num
            for stop in board_list:
                vvd = stop.get("ltdvvd") or ""
                vvd_num = re.sub(r'[^0-9]', '', vvd)
                if vvd_num == target_num:
                    matched_stops.append(stop)

            # Jika tidak ada yang cocok secara spesifik nomor voyage, pakai seluruh stops di round-trip/board_list
            if not matched_stops:
                matched_stops = board_list

            ports = []
            jakarta_arr_utc = None

            for s in matched_stops:
                p_name = (s.get("portNm") or "").strip()
                p_code = (s.get("portCd") or "").strip()
                arr_raw = (s.get("arrDt") or "").strip()
                dep_raw = (s.get("depDt") or "").strip()

                arr_date = None
                dep_date = None
                arr_dt = None
                dep_dt = None
                stay_h = 24.0

                if arr_raw:
                    try:
                        arr_dt = datetime.strptime(arr_raw, "%Y-%m-%d %H:%M")
                        arr_date = arr_dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass

                if dep_raw:
                    try:
                        dep_dt = datetime.strptime(dep_raw, "%Y-%m-%d %H:%M")
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
                    # Waktu lokal pelabuhan Jakarta (WIB = UTC+7)
                    if arr_dt:
                        dt_utc = arr_dt - timedelta(hours=7)
                        jakarta_arr_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    elif arr_date:
                        jakarta_arr_utc = f"{arr_date}T05:00:00Z"

            if not ports:
                print(f"  - {vessel_name} (voyage {voyage}): stops kosong.")
                continue

            update_payload = {"liner_route": ports}
            if jakarta_arr_utc:
                update_payload["liner_eta"] = jakarta_arr_utc

            try:
                supabase.table('vessel_schedules') \
                    .update(update_payload) \
                    .eq('id', sch['id']) \
                    .execute()

                print(f"  ✓ {vessel_name} ({voyage}): {len(ports)} ports, liner_eta={jakarta_arr_utc or '-'}")
                updated += 1
            except Exception as e:
                print(f"  - Gagal update {vessel_name} ({sch['id']}): {e}")

    print(f"Selesai HMM: {updated}/{len(schedules)} jadwal berhasil diperbarui.")


if __name__ == "__main__":
    asyncio.run(scrape_hmm())
