import os
import sys
import asyncio
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

sys.stdout.reconfigure(encoding='utf-8')

async def scrape():
    browser_config = BrowserConfig(headless=True, verbose=True, extra_args=["--disable-blink-features=AutomationControlled"])
    run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, word_count_threshold=10)
    imo = "9374595"
    url = f"https://www.vesselfinder.com/vessels/details/{imo}"
    
    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)
        if result.success:
            print(result.markdown)
        else:
            print("Failed:", result.error_message)

asyncio.run(scrape())
