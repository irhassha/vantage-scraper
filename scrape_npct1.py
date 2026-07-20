import os
import sys
import asyncio
from datetime import datetime, timedelta
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client, Client

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

NPCT1_SCHEDULE_URL = "https://www.npct1.co.id/vessel-schedule"

def parse_wib_to_utc(date_str: str) -> str:
    """Konversi waktu dari WIB (YYYY-MM-DD HH:MM:SS) ke UTC ISO 8601 string"""
    if not date_str or not date_str.strip():
        return None
    try:
        # Asumsikan format "2026-06-18 17:00:00"
        dt_wib = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
        # Kurangi 7 jam untuk mendapatkan UTC
        dt_utc = dt_wib - timedelta(hours=7)
        return dt_utc.isoformat() + "Z"
    except Exception as e:
        print(f"Gagal parse tanggal {date_str}: {e}")
        return None

async def scrape_npct1():
    print("Mulai scraping jadwal NPCT1...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with httpx.AsyncClient(verify=False) as client:
        try:
            response = await client.get(NPCT1_SCHEDULE_URL, headers=headers, timeout=30.0)
            response.raise_for_status()
        except Exception as e:
            print(f"Gagal fetch NPCT1: {e}")
            return
            
    soup = BeautifulSoup(response.text, 'html.parser')
    table = soup.find('table', id='idTableVesselSchedule')
    
    if not table:
        print("Tabel jadwal tidak ditemukan di halaman.")
        return
        
    tbody = table.find('tbody')
    if not tbody:
        print("Tbody tidak ditemukan.")
        return
        
    rows = tbody.find_all('tr')
    print(f"Ditemukan {len(rows)} baris data.")
    
    # Hitung batas waktu: max 8 hari kedepan
    eight_days_from_now = datetime.now() + timedelta(days=8)
    
    success_count = 0
    
    for row in rows:
        cols = row.find_all('td')
        if len(cols) < 10:
            continue
            
        vessel_name = cols[0].text.strip()
        line = cols[1].text.strip()
        voy_in = cols[2].text.strip()
        voy_out = cols[3].text.strip()
        service = cols[4].text.strip()
        status = cols[5].text.strip().upper()
        etb_raw = cols[6].text.strip()
        ata_raw = cols[7].text.strip()
        
        # Handle SAILED status — auto-mark as Departed
        if status == 'SAILED':
            voyage = voy_in if voy_in else voy_out
            if vessel_name and voyage:
                try:
                    # Cari kapal di master_vessels
                    res_vessel = supabase.table('master_vessels').select('id').ilike('vessel_name', vessel_name).limit(1).execute()
                    if res_vessel.data and len(res_vessel.data) > 0:
                        v_id = res_vessel.data[0]['id']
                        # Cari jadwal yang sudah ada dan belum Departed
                        existing = supabase.table('vessel_schedules').select('id, status').eq('vessel_id', v_id).eq('voyage', voyage).execute()
                        if existing.data and len(existing.data) > 0:
                            sched = existing.data[0]
                            if sched.get('status') != 'Departed':
                                supabase.table('vessel_schedules').update({
                                    'status': 'Departed',
                                    'is_watchlist': False
                                }).eq('id', sched['id']).execute()
                                success_count += 1
                                print(f"  ⛵ {vessel_name} (Voyage: {voyage}) → Otomatis ditandai DEPARTED (SAILED di schedule)")
                except Exception as e:
                    print(f"  - Error update SAILED→Departed untuk {vessel_name}: {e}")
            continue

        # Filter Status — hanya proses ACTIVE dan REGISTER
        if status not in ['ACTIVE', 'REGISTER']:
            continue
            
        # Filter ETB Max 8 days
        if etb_raw:
            try:
                etb_dt = datetime.strptime(etb_raw, "%Y-%m-%d %H:%M:%S")
                if etb_dt > eight_days_from_now:
                    # Skip jika lebih dari 8 hari
                    continue
            except ValueError:
                pass
                
        # Voyage akan digabung In / Out atau pakai salah satu. Biasanya di sistem kita pakai string misal "0480-110S"
        # Kita pakai voy_in sebagai voyage
        voyage = voy_in if voy_in else voy_out
        
        if not vessel_name or not voyage:
            continue
            
        print(f"Memproses {vessel_name} (Voyage: {voyage}, Status: {status})")
        
        # 1. Cek atau Buat Kapal di master_vessels
        vessel_id = None
        
        try:
            # Cari berdasarkan nama persis (case insensitive)
            res_vessel = supabase.table('master_vessels').select('id').ilike('vessel_name', vessel_name).limit(1).execute()
            
            if res_vessel.data and len(res_vessel.data) > 0:
                vessel_id = res_vessel.data[0]['id']
            else:
                # Insert baru dengan N/A untuk IMO beserta nama kapal agar unik
                new_vessel = supabase.table('master_vessels').insert({
                    'vessel_name': vessel_name,
                    'imo_number': f'N/A-{vessel_name}'
                }).execute()
                
                if new_vessel.data and len(new_vessel.data) > 0:
                    vessel_id = new_vessel.data[0]['id']
                    print(f"  + Kapal baru ditambahkan: {vessel_name} dengan IMO = N/A-{vessel_name}")
                    
        except Exception as e:
            print(f"  - Error cek/insert master_vessels: {e}")
            continue
            
        if not vessel_id:
            print("  - Gagal mendapatkan vessel_id")
            continue
            
        # 2. Upsert ke vessel_schedules
        etb_utc = parse_wib_to_utc(etb_raw)
        ata_utc = parse_wib_to_utc(ata_raw)
        
        # Mapping status NPCT1 ke status kita
        vessel_status = "En Route"
        if status == 'ACTIVE' or ata_utc:
            vessel_status = "Arrived"
            
        schedule_data = {
            'vessel_id': vessel_id,
            'voyage': voyage,
            'service': service,
            'etb': etb_utc,
            'status': vessel_status,
            'is_watchlist': False
        }
        

        
        if ata_utc:
            schedule_data['actual_atb'] = ata_utc
            
        try:
            # Cari jadwal yang sudah ada
            existing_sched = supabase.table('vessel_schedules').select('id').eq('vessel_id', vessel_id).eq('voyage', voyage).execute()
            
            if existing_sched.data and len(existing_sched.data) > 0:
                sched_id = existing_sched.data[0]['id']
                supabase.table('vessel_schedules').update(schedule_data).eq('id', sched_id).execute()
            else:
                supabase.table('vessel_schedules').insert(schedule_data).execute()
                
            success_count += 1
            print("  + Jadwal berhasil diupdate")
        except Exception as e:
            print(f"  - Error update/insert jadwal: {e}")
            
    # Cleanup: Tandai jadwal lama sebagai Departed
    # Jika ETB sudah lewat dari 3 hari yang lalu, kita anggap kapal sudah selesai (Departed)
    three_days_ago = (datetime.now() - timedelta(days=3)).isoformat() + "Z"
    try:
        cleanup_res = supabase.table('vessel_schedules') \
            .update({'status': 'Departed', 'is_watchlist': False}) \
            .lt('etb', three_days_ago) \
            .neq('status', 'Departed') \
            .execute()
        
        cleaned_count = len(cleanup_res.data) if cleanup_res.data else 0
        print(f"\nCleanup: {cleaned_count} jadwal lama otomatis ditandai sebagai DEPARTED.")
    except Exception as e:
        print(f"\nError saat cleanup jadwal lama: {e}")
        
    print(f"\nSelesai! Berhasil memproses/mengupdate {success_count} jadwal kapal dari NPCT1.")

if __name__ == "__main__":
    asyncio.run(scrape_npct1())
