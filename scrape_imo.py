import os
import sys

# Pastikan console Windows mendukung karakter Unicode (seperti tanda panah dari Crawl4AI)
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

import asyncio
import re
from urllib.parse import quote_plus
from dotenv import load_dotenv
from supabase import create_client, Client
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

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

async def scrape_imo_resolution():
    print("Memeriksa kapal dengan IMO 'N/A' di Supabase...")
    
    # 1. Ambil kapal yang IMO-nya 'N/A%'
    res = supabase.table('master_vessels').select('id, vessel_name').ilike('imo_number', 'N/A%').execute()
    vessels = res.data
    
    if not vessels:
        print("Tidak ada kapal yang membutuhkan pencarian IMO saat ini.")
        return
        
    print(f"Ditemukan {len(vessels)} kapal yang membutuhkan IMO.")
    
    # 2. Siapkan Crawler
    browser_config = BrowserConfig(
        headless=True,
        verbose=True,
        extra_args=["--disable-blink-features=AutomationControlled"]
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=10 
    )
    
    success_count = 0
    
    async with AsyncWebCrawler(config=browser_config) as crawler:
        for v in vessels:
            v_id = v['id']
            v_name = v['vessel_name']
            
            print(f"\nMencari IMO untuk: {v_name}...")
            
            # Buat URL Pencarian VesselFinder
            # Gunakan quote_plus untuk mengencode spasi menjadi + atau %20
            search_url = f"https://www.vesselfinder.com/vessels?name={quote_plus(v_name)}"
            
            try:
                result = await crawler.arun(
                    url=search_url,
                    config=run_config
                )
                
                if not result.success:
                    print(f"  - Gagal fetch halaman pencarian VesselFinder untuk {v_name}")
                    continue
                    
                # Ekstrak IMO dari Markdown content menggunakan Regex
                # Link di VesselFinder memiliki format /vessels/details/1234567
                # Kita cari angka 7 digit pertama setelah /vessels/details/
                match = re.search(r'/vessels/details/(\d{7})', result.markdown)
                
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
