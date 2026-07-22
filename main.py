import os
import sys
import asyncio
import re       # Tambahkan ini untuk Regex
import json     # Tambahkan ini untuk parsing JSON djson
import math # Untuk kalkulasi Haversine
import random # Untuk jitter
from datetime import datetime, timedelta # Tambahkan ini untuk format waktu

# Pastikan console Windows mendukung karakter Unicode (seperti tanda panah dari Crawl4AI)
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
from supabase import create_client, Client
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from scrape_npct1 import scrape_npct1

# Load Environment Variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment variables")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
except Exception as e:
    print(f"Gagal inisialisasi Supabase Client. Periksa koneksi atau kredensial: {e}")
    sys.exit(1)

# --- KONSTANTA KOORDINAT TUJUAN (Jakarta / NPCT1) ---
JAKARTA_LAT = -6.09
JAKARTA_LON = 106.90

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Menghitung jarak antara dua titik koordinat (GPS) menggunakan Haversine Formula.
    Hasil dalam satuan Nautical Miles (NM).
    
    Args:
        lat1, lon1: Koordinat titik asal (posisi kapal)
        lat2, lon2: Koordinat titik tujuan (Jakarta/NPCT1)
    
    Returns:
        Jarak dalam Nautical Miles (float)
    """
    # Radius bumi dalam Nautical Miles
    R_NM = 3440.065
    
    # Konversi derajat ke radian
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    # Haversine formula
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    distance = R_NM * c
    return round(distance, 2)

def extract_coordinates(html_text: str, markdown_text: str):
    """
    Ekstrak koordinat Latitude/Longitude kapal sesuai prioritas spesifik VesselFinder.
    1. Parsing <div id="djson" data-json="..."> (paling presisi)
    2. Fallback: Meta Description
    3. Fallback: JSON-LD
    4. Fallback: Global Regex
    """
    def clean_coord(val):
        if val is None:
            return None
        cleaned = re.sub(r'[^\d\.-]', '', str(val))
        if not cleaned or cleaned == '.' or cleaned == '-':
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    # STRATEGI UTAMA (Akurat): Extract dari djson elemen khusus VesselFinder
    # Mencari <div id="djson" data-json='{...}'>
    djson_match = re.search(r'<div[^>]*id=["\']djson["\'][^>]*data-json=([\'"])(.*?)\1', html_text, re.IGNORECASE | re.DOTALL)
    if djson_match:
        try:
            # Decode elemen JSON yang sering di-escape string pada HTML attribute
            raw_json = djson_match.group(2).replace('&quot;', '"')
            parsed_data = json.loads(raw_json)
            
            lat = clean_coord(parsed_data.get('ship_lat'))
            lon = clean_coord(parsed_data.get('ship_lon'))
            
            if lat is not None and lon is not None:
                return lat, lon
        except Exception as e:
            print(f"[DEBUG] Gagal decode djson: {e}")

    # Pola spesifik yang diminta user untuk keperluan fallback global 
    global_regex = r'([-+]?\d{1,2}\.\d+)\s*/\s*([-+]?\d{1,4}\.\d+)'

    # STRATEGI FALLBACK 1: Meta Description
    meta_desc = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    if meta_desc:
        match = re.search(global_regex, meta_desc.group(1))
        if match:
            lat = clean_coord(match.group(1))
            lon = clean_coord(match.group(2))
            if lat is not None and lon is not None:
                return lat, lon

    # STRATEGI FALLBACK 2: JSON-LD
    json_ld_blocks = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text, re.IGNORECASE | re.DOTALL)
    for block in json_ld_blocks:
        match = re.search(global_regex, block)
        if match:
            lat = clean_coord(match.group(1))
            lon = clean_coord(match.group(2))
            if lat is not None and lon is not None:
                return lat, lon

    # STRATEGI FALLBACK 3: Global Scan di HTML dan Markdown
    match = re.search(global_regex, html_text)
    if match:
        lat = clean_coord(match.group(1))
        lon = clean_coord(match.group(2))
        if lat is not None and lon is not None:
            return lat, lon

    match = re.search(global_regex, markdown_text)
    if match:
        lat = clean_coord(match.group(1))
        lon = clean_coord(match.group(2))
        if match and lat is not None and lon is not None:
            return lat, lon

    return None, None

def clean_markdown_link(text: str) -> str:
    """
    Membersihkan format markdown link seperti [Jakarta, Indonesia](url) menjadi teks saja.
    """
    if not text:
        return text
    return re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text).strip()

async def scrape_vessel_data():
    print("Mulai mengambil data kapal dari Supabase...")
    
    # 1. Ambil data jadwal kapal yang belum sandar (actual_atb is null)
    # Kita join dengan master_vessels untuk mendapatkan imo_number
    response = supabase.table('vessel_schedules') \
        .select('id, liner_eta, master_vessels(imo_number, vessel_name)') \
        .is_('actual_atb', 'null') \
        .neq('status', 'Departed') \
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
        extra_args=["--disable-blink-features=AutomationControlled"],
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
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
            liner_eta_raw = schedule.get('liner_eta')  # TIMESTAMPTZ string dari Supabase
            
            if not imo or imo.upper().startswith("N/A"):
                print(f"Skipping {vessel_name} karena IMO Number belum tersedia ({imo}).")
                continue

            print(f"Tracking {vessel_name} (IMO: {imo})...")
            
            # Target URL (VesselFinder menggunakan IMO di URL-nya)
            url = f"https://www.vesselfinder.com/vessels/details/{imo}"
            
            result = await crawler.arun(url=url, config=run_config)
            
            if result.success:
                # Di sini kita akan memparsing result.markdown atau result.extracted_content
                # Untuk MVP, kita mock data ini. Nanti kita buat Regex/LLM extraction dari raw markdown-nya
                print(f"Berhasil scrape data untuk {vessel_name}")
                
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
                
                # Kalkulasi jarak ke Jakarta menggunakan Haversine
                # Ekstrak koordinat dari HTML source (bukan markdown)
                vessel_lat, vessel_lon = extract_coordinates(
                    result.html if hasattr(result, 'html') and result.html else '',
                    markdown_text
                )
                
                # Data Validation untuk latitude dan longitude
                def is_valid_coord(val):
                    if val is None or val == "":
                        return False
                    try:
                        f = float(val)
                        return not math.isnan(f)
                    except (ValueError, TypeError):
                        return False
                
                is_valid_location = is_valid_coord(vessel_lat) and is_valid_coord(vessel_lon)
                
                if is_valid_location:
                    vessel_lat = float(vessel_lat)
                    vessel_lon = float(vessel_lon)
                    scraped_distance = haversine_nm(vessel_lat, vessel_lon, JAKARTA_LAT, JAKARTA_LON)
                    print(f"📍 Koordinat: ({vessel_lat}, {vessel_lon}) | Jarak ke Jakarta: {scraped_distance} NM")
                else:
                    vessel_lat = None
                    vessel_lon = None
                    scraped_distance = 0.0
                    print(f"⚠ Koordinat tidak ditemukan, distance_to_jkt diset 0.0")

                # 4. Ekstrak Destination menggunakan Regex
                dest_match = re.search(r'en route to\s*\*\*([^*]+)\*\*', markdown_text, re.IGNORECASE)
                if not dest_match:
                    dest_match = re.search(r'en route to the port of ([^,]+)', markdown_text, re.IGNORECASE)
                if not dest_match:
                    dest_match = re.search(r'Destination\s*\n\s*([^\n\r]+)', markdown_text, re.IGNORECASE)
                if not dest_match:
                    dest_match = re.search(r'Destination\s*[:|]\s*([^\n\r]+)', markdown_text, re.IGNORECASE)
                
                scraped_destination = dest_match.group(1).replace('*', '').strip() if dest_match else 'Unknown'
                scraped_destination = clean_markdown_link(scraped_destination)
                
                # 5. Ekstrak Previous Port (Last Port) menggunakan Regex
                # Format di markdown: "Last Port\n[Port Name, Country](url)"
                last_port_match = re.search(r'Last Port\s*\n\s*\[([^\]]+)\]', markdown_text, re.IGNORECASE)
                if not last_port_match:
                    # Fallback: cari pola "Departure Port" atau "Last Port  |  Port Name"
                    last_port_match = re.search(r'Last Port\s*[:|]\s*([^\n\r]+)', markdown_text, re.IGNORECASE)
                scraped_previous_port = last_port_match.group(1).strip() if last_port_match else None
                if scraped_previous_port:
                    scraped_previous_port = clean_markdown_link(scraped_previous_port)
                
                # 6. Ekstrak Navigation Status menggunakan Regex
                # Format: "Navigation Status  |  Moored  |" atau "Navigation Status  |  Under way  |"
                nav_status_match = re.search(r'Navigation Status\s*\|\s*([^|]+?)\s*\|', markdown_text, re.IGNORECASE)
                scraped_nav_status = nav_status_match.group(1).strip() if nav_status_match else None
                
                print(f"Ekstraksi Berhasil -> Speed: {scraped_speed} kn | ETA: {scraped_eta} | Dest: {scraped_destination} | Last Port: {scraped_previous_port} | Nav: {scraped_nav_status}")

                # 7. Insert ke Supabase tracking_logs
                log_data = {
                    "schedule_id": vessel_id,
                    "speed_sog": scraped_speed,
                    "distance_to_jkt": scraped_distance,
                    "preview_eta_ais": scraped_eta,
                    "destination": scraped_destination
                }
                
                if is_valid_location:
                    log_data["latitude"] = vessel_lat
                    log_data["longitude"] = vessel_lon
                
                supabase.table('tracking_logs').insert(log_data).execute()
                print(f"Data log {vessel_name} tersimpan di Supabase.")
                
                # --- UPDATE VESSEL_SCHEDULES ---
                schedule_update = {}
                
                if is_valid_location:
                    schedule_update['latitude'] = vessel_lat
                    schedule_update['longitude'] = vessel_lon
                
                # 8. Update previous_port jika berhasil diekstrak
                if scraped_previous_port:
                    schedule_update['previous_port'] = scraped_previous_port
                
                # 9. Update speed_sog secara konsisten ke vessel_schedules
                if scraped_speed is not None:
                    schedule_update['speed_sog'] = scraped_speed
                
                # 10. Update distance_to_jkt_nm jika koordinat berhasil diekstrak
                if scraped_distance > 0:
                    schedule_update['distance_to_jkt_nm'] = float(scraped_distance)
                
                # 11. Auto-Detect ATA (Actual Time of Arrival)
                # Kondisi: destination adalah Jakarta/IDJKT DAN (speed < 1.0 kn ATAU nav status Moored/At Anchor)
                jakarta_keywords = ['jakarta', 'idjkt', 'tanjung priok', 'jkt', 'id jkt']
                is_destination_jakarta = any(
                    kw in scraped_destination.lower() for kw in jakarta_keywords
                ) if scraped_destination else False
                
                is_arrived = (
                    scraped_speed < 1.0 or 
                    (scraped_nav_status and scraped_nav_status.lower() in ['moored', 'at anchor', 'arrived'])
                )
                
                if is_destination_jakarta and is_arrived:
                    ata_now = datetime.utcnow().isoformat()
                    schedule_update['ata'] = ata_now
                    schedule_update['status'] = 'Arrived'
                    print(f"⚓ ATA terdeteksi! {vessel_name} telah tiba di Jakarta pada {ata_now}")
                    
                    # 12. Kalkulasi Delay: selisih antara ATA dan liner_eta
                    if liner_eta_raw:
                        try:
                            # Parse liner_eta dari Supabase (format ISO 8601 / TIMESTAMPTZ)
                            # Coba beberapa format umum dari Supabase
                            liner_eta_str = liner_eta_raw.replace('Z', '+00:00')
                            if '+' in liner_eta_str or liner_eta_str.endswith('00:00'):
                                liner_eta_dt = datetime.fromisoformat(liner_eta_str)
                            else:
                                liner_eta_dt = datetime.fromisoformat(liner_eta_str)
                            
                            ata_dt = datetime.fromisoformat(ata_now)
                            delay_delta = ata_dt - liner_eta_dt.replace(tzinfo=None)
                            delay_hours = round(delay_delta.total_seconds() / 3600, 2)
                            
                            schedule_update['ml_delay_hours'] = delay_hours
                            
                            if delay_hours > 0:
                                print(f"📊 Delay terdeteksi: {delay_hours} jam terlambat dari liner ETA")
                            else:
                                print(f"📊 Kapal tiba {abs(delay_hours)} jam lebih awal dari liner ETA")
                        except Exception as e:
                            print(f"⚠ Gagal menghitung delay: {e}")
                
                # Lakukan update ke vessel_schedules jika ada data yang perlu di-update
                if schedule_update:
                    try:
                        supabase.table('vessel_schedules').update(schedule_update).eq('id', vessel_id).execute()
                        print(f"✅ vessel_schedules updated: {list(schedule_update.keys())}")
                    except Exception as e:
                        print(f"⚠ Gagal update vessel_schedules: {e}")
                
                # Beri jeda acak (jitter) antar kapal agar tidak di-ban
                await asyncio.sleep(random.uniform(3, 7))
            else:
                print(f"Gagal scrape {vessel_name}: {result.error_message}")

async def archive_and_cleanup():
    print("\n[MAINTENANCE] Memulai proses archive dan cleanup tracking_logs...")
    
    # 1. Tentukan batas waktu (3 hari yang lalu)
    three_days_ago = datetime.now() - timedelta(days=3)
    three_days_ago_iso = three_days_ago.isoformat()
    
    # 2. Ambil data yang lebih tua dari 3 hari
    try:
        # Tambahkan inner/left join ke vessel_schedules untuk ambil 'service'
        response = supabase.table('tracking_logs').select('*, vessel_schedules(service)').lt('scraped_at', three_days_ago_iso).execute()
        logs = response.data
    except Exception as e:
        print(f"Gagal mengambil data lama: {e}")
        return

    if not logs:
        print("[MAINTENANCE] Tidak ada data tracking yang perlu di-archive (lebih tua dari 3 hari).")
        return

    # 3. Kelompokkan berdasarkan schedule_id dan tanggal
    # Key: (schedule_id, date_str) -> List of logs
    daily_groups = {}
    for log in logs:
        # Asumsikan scraped_at formatnya ISO 8601 string misal "2026-04-06T14:30:00+00:00"
        scraped_at_str = log.get('scraped_at', '2026-01-01T00:00:00')
        date_str = scraped_at_str[:10]  # Ambil YYYY-MM-DD (10 karakter pertama)
        
        key = (log['schedule_id'], date_str)
        if key not in daily_groups:
            daily_groups[key] = []
        daily_groups[key].append(log)
        
    # 4. Buat data summary (1 baris per hari)
    summary_data = []
    log_ids_to_delete = []
    
    for (schedule_id, date_str), group_logs in daily_groups.items():
        # Urutkan berdasarkan waktu (terlama ke terbaru) supaya bisa ambil data terakhir di hari tsb
        group_logs.sort(key=lambda x: x.get('scraped_at', ''))
        latest_log = group_logs[-1]
        
        # Hitung rata-rata dengan mengabaikan tipe data None
        speeds = [float(l.get('speed_sog', 0) or 0) for l in group_logs]
        distances = [float(l.get('distance_to_jkt', 0) or 0) for l in group_logs]
        
        avg_speed = sum(speeds) / len(speeds) if speeds else 0.0
        avg_distance = sum(distances) / len(distances) if distances else 0.0
        
        # Ambil kolom service, default None jika tidak ada
        vessel_sched = latest_log.get('vessel_schedules')
        service = vessel_sched.get('service') if isinstance(vessel_sched, dict) else None
        
        summary_data.append({
            "schedule_id": schedule_id,
            "summary_date": date_str,
            "service": service,
            "avg_speed_sog": round(avg_speed, 2),
            "avg_distance_to_jkt": round(avg_distance, 2),
            "latest_eta_ais": latest_log.get('preview_eta_ais'),
            "latest_destination": latest_log.get('destination')
        })
        
        for l in group_logs:
            if 'id' in l:
                log_ids_to_delete.append(l['id'])
            
    # 5. Insert ke vessel_daily_summary
    if summary_data:
        try:
            # Batch insert otomatis
            supabase.table('vessel_daily_summary').insert(summary_data).execute()
            print(f"[MAINTENANCE] Berhasil insert {len(summary_data)} baris summary harian ke tabel vessel_daily_summary.")
        except Exception as e:
            print(f"[MAINTENANCE] Gagal melakukan insert ke vessel_daily_summary: {e}")
            return
            
    # 6. Hapus data detail di tracking_logs
    if log_ids_to_delete:
        try:
            # Hapus bertahap per batch untuk menghindari limit di query string
            batch_size = 100
            for i in range(0, len(log_ids_to_delete), batch_size):
                batch_ids = log_ids_to_delete[i:i+batch_size]
                supabase.table('tracking_logs').delete().in_('id', batch_ids).execute()
            print(f"[MAINTENANCE] Berhasil menghapus {len(log_ids_to_delete)} baris data log detail yang lama dari tracking_logs.")
        except Exception as e:
            print(f"[MAINTENANCE] Gagal menghapus data lama dari tracking_logs: {e}")
    
    print("[MAINTENANCE] Proses archive dan cleanup selesai.\n")

async def main():
    # Jalankan scraping jadwal NPCT1 dulu (update ETB, detect SAILED → Departed)
    await scrape_npct1()
    # Lalu jalankan tracking posisi kapal via VesselFinder
    await scrape_vessel_data()
    # Lalu jalankan opsi cleanup & archiving database
    await archive_and_cleanup()

if __name__ == "__main__":
    asyncio.run(main())