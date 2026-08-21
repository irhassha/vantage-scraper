import os
import sys

# Pastikan console Windows mendukung karakter Unicode
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

import asyncio
import re
from urllib.parse import quote_plus
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

async def scrape_imo_resolution(max_vessels: int = 2):
    print(f"Memeriksa hingga {max_vessels} kapal dengan IMO 'N/A' di Supabase...")
    
    # 1. Ambil kapal yang IMO-nya 'N/A%' (dibatasi max_vessels agar tidak overload)
    res = supabase.table('master_vessels').select('id, vessel_name').ilike('imo_number', 'N/A%').limit(max_vessels).execute()
    vessels = res.data
    
    if not vessels:
        print("Tidak ada kapal yang membutuhkan pencarian IMO saat ini.")
        return
        
    print(f"Ditemukan {len(vessels)} kapal yang membutuhkan IMO.")
    
    # 2. Setup HTTP client ringan (tanpa browser headless)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    success_count = 0
    
    async with httpx.AsyncClient(headers=headers, verify=False, timeout=httpx.Timeout(30.0)) as client:
        for v in vessels:
            v_id = v['id']
            v_name = v['vessel_name']
            
            print(f"\nMencari IMO untuk: {v_name}...")
            
            # Buat URL Pencarian VesselFinder
            # Gunakan quote_plus untuk mengencode spasi menjadi + atau %20
            search_url = f"https://www.vesselfinder.com/vessels?name={quote_plus(v_name)}"
            
            try:
                response = await client.get(search_url)
                response.raise_for_status()
                    
                # Ekstrak IMO dari HTML content menggunakan Regex
                # Link di VesselFinder memiliki format /vessels/details/1234567
                # Kita cari angka 7 digit pertama setelah /vessels/details/
                match = re.search(r'/vessels/details/(\d{7})', response.text)
                
                if match:
                    found_imo = match.group(1)
                    print(f"  + Berhasil menemukan IMO: {found_imo}")
                    
                    # Update Supabase
                    update_res = supabase.table('master_vessels').update({'imo_number': found_imo}).eq('id', v_id).execute()
                    
                    if update_res.data:
                        print("  + Database diupdate!")
                        success_count += 1
                    else:
                        print("  - Gagal update database.")
                else:
                    print(f"  - IMO tidak ditemukan di hasil pencarian untuk {v_name}.")
                    
            except Exception as e:
                print(f"  - Error saat mencari {v_name}: {e}")
                
            # Beri jeda acak agar tidak terkena rate limit
            await asyncio.sleep(3)

    print(f"\nSelesai! Berhasil merevolusi {success_count} dari {len(vessels)} kapal.")

if __name__ == "__main__":
    # Fix untuk masalah async di Windows
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    asyncio.run(scrape_imo_resolution())
