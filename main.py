import os
import asyncio
import re # Tambahkan ini untuk Regex
from datetime import datetime # Tambahkan ini untuk format waktu
from dotenv import load_dotenv
from supabase import create_client, Client
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

# Load Environment Variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

async def scrape_vessel_data():
    print("Mulai mengambil data kapal dari Supabase...")
    
    # 1. Ambil data jadwal kapal yang belum sandar (actual_atb is null)
    # Kita join dengan master_vessels untuk mendapatkan imo_number
    response = supabase.table('vessel_schedules') \
        .select('id, master_vessels(imo_number, vessel_name)') \
        .is_('actual_atb', 'null') \
        .execute()
    
    schedules = response.data
    if not schedules:
        print("Tidak ada kapal yang sedang ditracking saat ini.")
        return

    # 2. Setup Crawl4ai agar terlihat seperti browser manusia
    browser_config = BrowserConfig(
        headless=True,
        verbose=True,
        # Menambahkan argumen anti-bot standar
        extra_args=["--disable-blink-features=AutomationControlled"]
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        # CSS Selector untuk mengambil elemen teks dari VesselFinder (contoh)
        # Catatan: Selector ini mungkin perlu disesuaikan dengan struktur web aslinya
        word_count_threshold=10 
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for schedule in schedules:
            vessel_id = schedule['id']
            imo = schedule['master_vessels']['imo_number']
            vessel_name = schedule['master_vessels']['vessel_name']
            
            print(f"Tracking {vessel_name} (IMO: {imo})...")
            
            # Target URL (VesselFinder menggunakan IMO di URL-nya)
            url = f"https://www.vesselfinder.com/vessels/details/{imo}"
            
            result = await crawler.arun(url=url, config=run_config)
            
            if result.success:
                # Di sini kita akan memparsing result.markdown atau result.extracted_content
                # Untuk MVP, kita mock data ini. Nanti kita buat Regex/LLM extraction dari raw markdown-nya
                print(f"Berhasil scrape data untuk {vessel_name}")
                
                # Simpan full teks ke file agar mudah dibaca di VS Code
                with open(f"{vessel_name}_data.txt", "w", encoding="utf-8") as file:
                    file.write(result.markdown)
                print(f"Silakan cek file {vessel_name}_data.txt di sebelah kiri (Explorer)!")
                
                # MOCK DATA PASING (Ganti dengan logika parsing teks yang sebenarnya nanti)
                # --- EKSTRAKSI DATA ASLI DARI MARKDOWN ---
                markdown_text = result.markdown
                
                # 1. Ekstrak Speed menggunakan Regex
                # Mencari pola: "Course / Speed  | 195.2° / 10.6 kn" dan mengambil angka 10.6
                speed_match = re.search(r'Course / Speed\s*\|\s*[\d\.]+°\s*/\s*([\d\.]+)\s*kn', markdown_text)
                scraped_speed = float(speed_match.group(1)) if speed_match else 0.0
                
                # 2. Ekstrak ETA menggunakan Regex
                # Mencari pola: "ETA: Apr 6, 17:30 (" dan mengambil "Apr 6, 17:30"
                eta_match = re.search(r'ETA:\s*(.*?)\s*\(', markdown_text)
                scraped_eta_raw = eta_match.group(1) if eta_match else None
                
                # 3. Format ETA agar diterima oleh Supabase (TIMESTAMPTZ)
                scraped_eta = None
                if scraped_eta_raw:
                    try:
                        # Tambahkan tahun berjalan (2026) agar format waktunya lengkap
                        eta_str = f"{scraped_eta_raw} 2026" 
                        # Parse format "Apr 6, 17:30 2026"
                        parsed_date = datetime.strptime(eta_str, "%b %d, %H:%M %Y")
                        scraped_eta = parsed_date.isoformat() # Hasil: "2026-04-06T17:30:00"
                    except Exception as e:
                        print(f"Gagal memformat tanggal ETA: {e}")
                
                # VesselFinder tidak menampilkan sisa jarak (Distance) secara publik
                # Untuk sementara kita set 0. Nanti ML bisa menghitung dari kordinat jika diperlukan.
                scraped_distance = 0.0 
                
                print(f"Ekstraksi Berhasil -> Speed: {scraped_speed} kn | ETA: {scraped_eta}")

                # 4. Insert ke Supabase tracking_logs
                log_data = {
                    "schedule_id": vessel_id,
                    "speed_sog": scraped_speed,
                    "distance_to_jkt": scraped_distance,
                    "preview_eta_ais": scraped_eta
                }
                
                # 3. Insert ke Supabase tracking_logs
                log_data = {
                    "schedule_id": vessel_id,
                    "speed_sog": scraped_speed,
                    "distance_to_jkt": scraped_distance,
                    "preview_eta_ais": scraped_eta
                }
                
                supabase.table('tracking_logs').insert(log_data).execute()
                print(f"Data log {vessel_name} tersimpan di Supabase.")
                
                # Beri jeda acak antar kapal agar tidak di-ban
                await asyncio.sleep(5)
            else:
                print(f"Gagal scrape {vessel_name}: {result.error_message}")

if __name__ == "__main__":
    asyncio.run(scrape_vessel_data())