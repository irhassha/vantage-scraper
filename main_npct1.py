import sys
import asyncio

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

from scrape_npct1 import scrape_npct1

async def main():
    print("Mulai scraping jadwal NPCT1 (Standalone Workflow)...")
    await scrape_npct1()
    print("Scraping NPCT1 selesai.")

if __name__ == "__main__":
    asyncio.run(main())
