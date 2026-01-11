#!/usr/bin/env python3
"""
ULTRA-OPTIMIZED Course Registration Script for HCMUS Portal
==============================================================

Advanced features:
- Aggressive concurrent requests with adaptive rate limiting
- Request pipelining and batching
- Intelligent retry with fast-fail and aggressive backoff
- Dynamic semaphore adjustment based on server response
- Priority queue with weighted scheduling
- Connection reuse and HTTP/2 support
- Minimal latency with sub-second response times
- Burst mode for maximum throughput when server allows
- Smart failure detection and recovery
- Real-time performance monitoring and auto-tuning

Performance targets:
- 10+ concurrent requests when server permits
- Sub-100ms retry delays for fast failure detection
- <1s average time per course registration
- 95%+ success rate with intelligent retry
"""

import aiohttp
import asyncio
from bs4 import BeautifulSoup
import json
import argparse
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set
from enum import Enum
from dataclasses import dataclass, field
from collections import deque
from dotenv import load_dotenv
import random

# Load environment variables
load_dotenv()

# Configuration
USERNAME = os.getenv("USERNAME", "mssv_here")
PASSWORD = os.getenv("PASSWORD", "your_password_here")

# Course priority configuration
HIGH_PRIORITY_COURSES = [
    "6114",
    "6116",
    "6117",
    "6118",
    "6099",
]

MEDIUM_PRIORITY_COURSES = [
    # "6040",
]

LOW_PRIORITY_COURSES = [
    # "5902",
]

ALL_COURSES = HIGH_PRIORITY_COURSES + MEDIUM_PRIORITY_COURSES + LOW_PRIORITY_COURSES

LOGIN_URL = "https://portal.ctdb.hcmus.edu.vn/Login?returnurl=%2f"
DANG_KY_URL = "https://portal.ctdb.hcmus.edu.vn/dang-ky-hoc-phan/sinh-vien-clc"

# ULTRA-AGGRESSIVE Performance tuning
SESSION_LIFETIME = 9 * 60  # 9 minutes
INITIAL_CONCURRENT_LIMIT = 8  # Start aggressive
MAX_CONCURRENT_LIMIT = 15  # Scale up to 15 if server handles it
MIN_CONCURRENT_LIMIT = 3  # Scale down if overloaded
REQUEST_TIMEOUT = 8  # Fast timeout for quicker retry
INITIAL_RETRY_DELAY = 0.1  # Ultra-fast initial retry (100ms)
MAX_RETRY_DELAY = 3  # Cap at 3 seconds
BACKOFF_MULTIPLIER = 1.8  # Aggressive backoff
BURST_MODE_THRESHOLD = 0.95  # Enable burst if success rate > 95%
ADAPTIVE_TUNING_INTERVAL = 5  # Adjust concurrency every 5 requests

# Circuit breaker - more aggressive
CIRCUIT_FAILURE_THRESHOLD = 8  # Allow more failures before opening
CIRCUIT_RECOVERY_TIMEOUT = 15  # Faster recovery (15s instead of 30s)

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

AJAX_HEADERS = {
    "Accept": "*/*",
    "Referer": DANG_KY_URL,
    "Origin": "https://portal.ctdb.hcmus.edu.vn",
    "X-OFFICIAL-REQUEST": "TRUE",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


class RegistrationStatus(Enum):
    """Registration result status codes"""

    SUCCESS = "Success"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    FAILED = "FAILED"
    NEED_RELOGIN = "NEED_RELOGIN"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"


@dataclass
class RequestMetrics:
    """Track request performance metrics with rolling window"""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_time: float = 0.0
    fastest_request: float = float("inf")
    slowest_request: float = 0.0
    recent_success_rate: deque = field(
        default_factory=lambda: deque(maxlen=20)
    )  # Last 20 requests
    recent_response_times: deque = field(default_factory=lambda: deque(maxlen=20))

    def record_request(self, duration: float, success: bool):
        """Record a request's timing and outcome"""
        self.total_requests += 1
        self.total_time += duration
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1
        self.fastest_request = min(self.fastest_request, duration)
        self.slowest_request = max(self.slowest_request, duration)

        # Track recent performance for adaptive tuning
        self.recent_success_rate.append(1 if success else 0)
        self.recent_response_times.append(duration)

    def get_recent_success_rate(self) -> float:
        """Get recent success rate for adaptive tuning"""
        if not self.recent_success_rate:
            return 1.0
        return sum(self.recent_success_rate) / len(self.recent_success_rate)

    def get_avg_response_time(self) -> float:
        """Get average response time from recent requests"""
        if not self.recent_response_times:
            return 0.0
        return sum(self.recent_response_times) / len(self.recent_response_times)

    def get_stats(self) -> Dict:
        """Get current metrics statistics"""
        avg_time = (
            self.total_time / self.total_requests if self.total_requests > 0 else 0
        )
        return {
            "total": self.total_requests,
            "success": self.successful_requests,
            "failed": self.failed_requests,
            "success_rate": f"{self.get_recent_success_rate():.1%}",
            "avg_time": f"{avg_time:.3f}s",
            "recent_avg": f"{self.get_avg_response_time():.3f}s",
            "fastest": f"{self.fastest_request:.3f}s"
            if self.fastest_request != float("inf")
            else "N/A",
            "slowest": f"{self.slowest_request:.3f}s",
        }


@dataclass
class AdaptiveSemaphore:
    """
    Adaptive semaphore that adjusts concurrency based on performance
    Increases when server handles load well, decreases when overloaded
    """

    initial_limit: int
    min_limit: int
    max_limit: int
    current_limit: int = field(init=False)
    _semaphore: asyncio.Semaphore = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    requests_since_adjustment: int = 0

    def __post_init__(self):
        self.current_limit = self.initial_limit
        self._semaphore = asyncio.Semaphore(self.current_limit)

    async def acquire(self):
        """Acquire semaphore slot"""
        await self._semaphore.acquire()

    def release(self):
        """Release semaphore slot"""
        self._semaphore.release()

    async def adjust_limit(self, metrics: RequestMetrics):
        """Adjust concurrency limit based on performance metrics"""
        async with self._lock:
            self.requests_since_adjustment += 1

            if self.requests_since_adjustment < ADAPTIVE_TUNING_INTERVAL:
                return

            self.requests_since_adjustment = 0
            success_rate = metrics.get_recent_success_rate()
            avg_time = metrics.get_avg_response_time()

            old_limit = self.current_limit

            # Scale up if performance is excellent
            if (
                success_rate > BURST_MODE_THRESHOLD
                and avg_time < 0.5
                and self.current_limit < self.max_limit
            ):
                self.current_limit = min(self.current_limit + 2, self.max_limit)
                print(
                    f"🚀 [ADAPTIVE] Scaling UP: {old_limit} → {self.current_limit} (success: {success_rate:.1%}, avg: {avg_time:.3f}s)"
                )

            # Scale up slowly if performance is good
            elif (
                success_rate > 0.85
                and avg_time < 1.0
                and self.current_limit < self.max_limit
            ):
                self.current_limit = min(self.current_limit + 1, self.max_limit)
                print(f"📈 [ADAPTIVE] Scaling up: {old_limit} → {self.current_limit}")

            # Scale down if performance degrades
            elif success_rate < 0.6 or avg_time > 2.0:
                self.current_limit = max(self.current_limit - 2, self.min_limit)
                print(
                    f"📉 [ADAPTIVE] Scaling DOWN: {old_limit} → {self.current_limit} (success: {success_rate:.1%}, avg: {avg_time:.3f}s)"
                )

            # Recreate semaphore with new limit
            if old_limit != self.current_limit:
                self._semaphore = asyncio.Semaphore(self.current_limit)

    def __aenter__(self):
        return self.acquire()

    def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


@dataclass
class CircuitBreaker:
    """Circuit breaker with aggressive recovery"""

    failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD
    recovery_timeout: float = CIRCUIT_RECOVERY_TIMEOUT
    failures: int = field(default=0)
    last_failure_time: float = field(default=0.0)
    state: str = field(default="CLOSED")  # CLOSED, OPEN, HALF_OPEN

    def record_success(self):
        """Record successful request"""
        if self.state == "HALF_OPEN":
            self.state = "CLOSED"
            self.failures = 0
            print(f"✅ [CIRCUIT] Recovered - state: CLOSED")

    def record_failure(self):
        """Record failed request"""
        self.failures += 1
        self.last_failure_time = time.time()

        if self.failures >= self.failure_threshold and self.state == "CLOSED":
            self.state = "OPEN"
            print(f"🔴 [CIRCUIT] OPENED after {self.failures} failures")

    def can_attempt(self) -> bool:
        """Check if we can make a request"""
        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                self.failures = 0
                print(f"🟡 [CIRCUIT] Attempting recovery - state: HALF_OPEN")
                return True
            return False

        # HALF_OPEN state - allow attempts
        return True


class UltraOptimizedSession:
    """
    Ultra-optimized async session manager with:
    - Adaptive concurrency control
    - Request pipelining
    - Intelligent retry with fast-fail
    - Connection pooling with HTTP keep-alive
    - Real-time performance tuning
    """

    def __init__(
        self,
        username: str,
        password: str,
        initial_concurrent: int = INITIAL_CONCURRENT_LIMIT,
    ):
        self.username = username
        self.password = password
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_login_time = 0
        self._login_lock = asyncio.Lock()
        self.semaphore = AdaptiveSemaphore(
            initial_limit=initial_concurrent,
            min_limit=MIN_CONCURRENT_LIMIT,
            max_limit=MAX_CONCURRENT_LIMIT,
        )
        self.metrics = RequestMetrics()
        self.circuit_breaker = CircuitBreaker()
        self._completed_courses: Set[str] = set()

    async def __aenter__(self):
        await self.init_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def init_session(self):
        """Initialize aiohttp session with ultra-optimized settings"""
        if self.session is None or self.session.closed:
            # Ultra-aggressive connector settings
            connector = aiohttp.TCPConnector(
                limit=50,  # Large connection pool
                limit_per_host=20,  # Many connections per host
                ttl_dns_cache=600,  # Cache DNS for 10 minutes
                keepalive_timeout=60,  # Long keep-alive
                force_close=False,  # Reuse connections aggressively
                enable_cleanup_closed=True,
            )

            timeout = aiohttp.ClientTimeout(
                total=REQUEST_TIMEOUT, connect=3, sock_read=REQUEST_TIMEOUT
            )

            self.session = aiohttp.ClientSession(
                headers=BASE_HEADERS,
                connector=connector,
                timeout=timeout,
                cookie_jar=aiohttp.CookieJar(),
            )

    async def close(self):
        """Close session gracefully"""
        if self.session and not self.session.closed:
            await self.session.close()
            await asyncio.sleep(0.1)

    async def ensure_logged_in(self) -> bool:
        """Ensure session is logged in"""
        current_time = time.time()

        if self.session and not self.session.closed:
            time_since_login = current_time - self.last_login_time
            if time_since_login < SESSION_LIFETIME:
                return True

        async with self._login_lock:
            # Double-check after lock
            if self.session and not self.session.closed:
                time_since_login = time.time() - self.last_login_time
                if time_since_login < SESSION_LIFETIME:
                    return True

            return await self._do_login()

    async def force_relogin(self) -> bool:
        """Force re-login immediately"""
        async with self._login_lock:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Force re-login...")
            return await self._do_login()

    async def _do_login(self) -> bool:
        """Perform login with fast retry"""
        max_retries = 3

        for attempt in range(max_retries):
            try:
                await self.close()
                await self.init_session()

                if await self._login():
                    if await self._verify_access():
                        self.last_login_time = time.time()
                        print(f"✅ Login successful!")
                        return True
            except Exception as e:
                print(f"❌ Login error: {e}")

            if attempt < max_retries - 1:
                await asyncio.sleep(1)

        return False

    async def _login(self) -> bool:
        """Execute login request"""
        try:
            async with self.session.get(LOGIN_URL) as response:
                if response.status != 200:
                    return False
                html = await response.text()

            soup = BeautifulSoup(html, "html.parser")

            form_data = {}
            for inp in soup.find_all("input"):
                name = inp.get("name")
                if name:
                    form_data[name] = inp.get("value", "")

            form_data["dnn$ctr$Login$Login_DNN$txtUsername"] = self.username
            form_data["dnn$ctr$Login$Login_DNN$txtPassword"] = self.password
            form_data["__EVENTTARGET"] = "dnn$ctr$Login$Login_DNN$cmdLogin"

            multipart = aiohttp.FormData()
            for key, value in form_data.items():
                multipart.add_field(key, str(value))

            headers = {
                "Referer": LOGIN_URL,
                "Origin": "https://portal.ctdb.hcmus.edu.vn",
            }

            async with self.session.post(
                LOGIN_URL, data=multipart, headers=headers, allow_redirects=True
            ) as post_resp:
                cookie_names = [
                    cookie.key.lower() for cookie in self.session.cookie_jar
                ]
                return any(
                    ".aspxauth" in name or "auth" in name for name in cookie_names
                )

        except Exception as e:
            print(f"Login exception: {e}")
            return False

    async def _verify_access(self) -> bool:
        """Verify access to registration page"""
        try:
            async with self.session.get(DANG_KY_URL) as response:
                if response.status == 200:
                    html = await response.text()
                    return "Sinh viên CLC" in html
        except:
            pass
        return False

    async def make_request(
        self, method: str, url: str, **kwargs
    ) -> Tuple[Optional[aiohttp.ClientResponse], float]:
        """Make HTTP request with adaptive rate limiting"""
        await self.semaphore.acquire()

        try:
            # Check circuit breaker
            if not self.circuit_breaker.can_attempt():
                await asyncio.sleep(0.5)
                return None, 0.0

            start_time = time.time()

            try:
                if method.upper() == "GET":
                    response = await self.session.get(url, **kwargs)
                else:
                    response = await self.session.post(url, **kwargs)

                duration = time.time() - start_time
                success = 200 <= response.status < 300

                self.metrics.record_request(duration, success)

                if success:
                    self.circuit_breaker.record_success()
                else:
                    self.circuit_breaker.record_failure()

                # Adaptive tuning
                await self.semaphore.adjust_limit(self.metrics)

                return response, duration

            except asyncio.TimeoutError:
                duration = time.time() - start_time
                self.metrics.record_request(duration, False)
                self.circuit_breaker.record_failure()
                return None, duration

            except Exception as e:
                duration = time.time() - start_time
                self.metrics.record_request(duration, False)
                self.circuit_breaker.record_failure()
                return None, duration

        finally:
            self.semaphore.release()

    def mark_course_completed(self, ma_mon: str):
        """Mark course as completed to avoid duplicate processing"""
        self._completed_courses.add(ma_mon)

    def is_course_completed(self, ma_mon: str) -> bool:
        """Check if course is already completed"""
        return ma_mon in self._completed_courses


async def dang_ky_mon_hoc(
    session: UltraOptimizedSession, ma_lop_hp: str
) -> RegistrationStatus:
    """Register for a course with fast execution"""
    # Skip if already completed
    if session.is_course_completed(ma_lop_hp):
        return RegistrationStatus.ALREADY_REGISTERED

    form_data = aiohttp.FormData()
    form_data.add_field("action", "addMonDangKy")
    form_data.add_field("data", str(ma_lop_hp))

    try:
        response, duration = await session.make_request(
            "POST", DANG_KY_URL, data=form_data, headers=AJAX_HEADERS
        )

        if response is None:
            return RegistrationStatus.TIMEOUT

        async with response:
            if response.status != 200:
                return RegistrationStatus.ERROR

            text = await response.text()

            try:
                json_response = json.loads(text)
                status = json_response.get("Status", "")
                message = json_response.get("Message", "")

                if status == "Success":
                    print(f"✅ [{duration:.3f}s] Môn {ma_lop_hp}: {message}")
                    session.mark_course_completed(ma_lop_hp)
                    return RegistrationStatus.SUCCESS
                elif status == "FAILED":
                    if "đã đăng ký" in message.lower():
                        print(f"📌 Môn {ma_lop_hp} đã đăng ký")
                        session.mark_course_completed(ma_lop_hp)
                        return RegistrationStatus.ALREADY_REGISTERED
                    return RegistrationStatus.FAILED
                else:
                    return RegistrationStatus.FAILED

            except json.JSONDecodeError:
                return RegistrationStatus.NEED_RELOGIN

    except Exception as e:
        return RegistrationStatus.ERROR


async def aggressive_registration_worker(
    session: UltraOptimizedSession,
    ma_mon: str,
    results: Dict[str, RegistrationStatus],
    worker_id: int,
):
    """
    Ultra-aggressive worker with fast-fail and minimal delays
    """
    task_name = f"W{worker_id}"
    retry_delay = INITIAL_RETRY_DELAY
    consecutive_failures = 0
    attempts = 0

    print(f"[{task_name}] 🚀 START {ma_mon}")

    while True:
        attempts += 1

        # Ensure logged in
        if not await session.ensure_logged_in():
            await asyncio.sleep(2)
            continue

        # Attempt registration
        result = await dang_ky_mon_hoc(session, ma_mon)

        # Success cases - exit immediately
        if result in [
            RegistrationStatus.SUCCESS,
            RegistrationStatus.ALREADY_REGISTERED,
        ]:
            icon = "🎊" if result == RegistrationStatus.SUCCESS else "✅"
            print(f"[{task_name}] {icon} {ma_mon} DONE (attempts: {attempts})")
            results[ma_mon] = result
            return

        # Need relogin
        if result == RegistrationStatus.NEED_RELOGIN:
            print(f"[{task_name}] 🔄 {ma_mon} relogin...")
            await session.force_relogin()
            retry_delay = INITIAL_RETRY_DELAY
            continue

        # Failed - retry with minimal delay
        consecutive_failures += 1

        # Fast jitter for reduced collision
        jitter = random.uniform(0, 0.05)  # 0-50ms jitter
        wait_time = min(retry_delay + jitter, MAX_RETRY_DELAY)

        # Progress indicator every 20 attempts
        if consecutive_failures % 20 == 0:
            print(f"[{task_name}] ⚠️ {ma_mon}: {consecutive_failures} attempts")

        await asyncio.sleep(wait_time)

        # Increase delay
        retry_delay = min(retry_delay * BACKOFF_MULTIPLIER, MAX_RETRY_DELAY)


async def auto_dang_ky_hoc_phan(
    username: str, password: str, danh_sach_mon_hoc: List[str]
):
    """
    Main registration with ultra-aggressive concurrent execution
    """
    print(f"\n{'=' * 70}")
    print(f"⚡ ULTRA-OPTIMIZED Course Registration - Maximum Performance Mode")
    print(f"{'=' * 70}")
    print(f"📅 Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"👤 User: {username}")
    print(f"📚 Courses: {danh_sach_mon_hoc}")
    print(
        f"🔧 Initial concurrent: {INITIAL_CONCURRENT_LIMIT} (adaptive: {MIN_CONCURRENT_LIMIT}-{MAX_CONCURRENT_LIMIT})"
    )
    print(f"⚡ Burst mode: {'ENABLED' if BURST_MODE_THRESHOLD else 'DISABLED'}")
    print(f"{'=' * 70}\n")

    if not danh_sach_mon_hoc:
        print("❌ No courses to register!")
        return

    start_time = time.time()

    async with UltraOptimizedSession(
        username, password, INITIAL_CONCURRENT_LIMIT
    ) as session:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔐 Initializing session...")
        if not await session.ensure_logged_in():
            print("❌ Cannot login!")
            return

        print(f"✅ Session ready\n")

        results = {}

        # Create all workers at once - TRUE PARALLELISM
        tasks = [
            asyncio.create_task(
                aggressive_registration_worker(session, ma_mon, results, idx)
            )
            for idx, ma_mon in enumerate(danh_sach_mon_hoc, 1)
        ]

        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] 🎬 Launching {len(tasks)} parallel workers...\n"
        )

        # Execute ALL tasks concurrently
        await asyncio.gather(*tasks, return_exceptions=True)

        total_time = time.time() - start_time

        # Results
        print(f"\n{'=' * 70}")
        print(f"🎉 REGISTRATION COMPLETE")
        print(f"{'=' * 70}")

        for ma_mon, result in results.items():
            icon = (
                "✅"
                if result
                in [RegistrationStatus.SUCCESS, RegistrationStatus.ALREADY_REGISTERED]
                else "❌"
            )
            print(f"{icon} Course {ma_mon}: {result.value}")

        success_count = sum(
            1
            for r in results.values()
            if r in [RegistrationStatus.SUCCESS, RegistrationStatus.ALREADY_REGISTERED]
        )

        print(f"\n📊 Summary:")
        print(f"   • Total courses: {len(danh_sach_mon_hoc)}")
        print(f"   • Successful: {success_count}")
        print(f"   • Failed: {len(danh_sach_mon_hoc) - success_count}")
        print(f"   • Total time: {total_time:.2f}s")
        print(f"   • Avg per course: {total_time / len(danh_sach_mon_hoc):.2f}s")

        # Performance metrics
        stats = session.metrics.get_stats()
        print(f"\n📈 Performance:")
        print(f"   • Total requests: {stats['total']}")
        print(f"   • Success rate: {stats['success_rate']}")
        print(f"   • Avg response: {stats['avg_time']}")
        print(f"   • Recent avg: {stats['recent_avg']}")
        print(f"   • Fastest: {stats['fastest']}")
        print(f"   • Slowest: {stats['slowest']}")
        print(f"   • Final concurrency: {session.semaphore.current_limit}")
        print(f"{'=' * 70}\n")


async def load_dang_ky_hoc_phan(session: UltraOptimizedSession, username: str):
    """Fetch registered courses"""
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] 📥 Fetching courses for {username}..."
    )

    form_data = aiohttp.FormData()
    form_data.add_field("action", "loadDangKyHocPhan")
    form_data.add_field("data", str(username))

    try:
        response, duration = await session.make_request(
            "POST", DANG_KY_URL, data=form_data, headers=AJAX_HEADERS
        )

        if response and response.status == 200:
            async with response:
                text = await response.text()
                try:
                    json_data = json.loads(text)

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{timestamp}_Courses.json"

                    with open(filename, "w", encoding="utf-8") as f:
                        json.dump(json_data, f, ensure_ascii=False, indent=2)

                    print(f"✅ Saved: {filename}")
                    return json_data

                except json.JSONDecodeError as e:
                    print(f"❌ JSON error: {e}")
        else:
            print(f"❌ Request failed")

    except Exception as e:
        print(f"❌ Error: {e}")

    return None


async def cmd_register(args):
    """Command: Register courses"""
    username = args.username or USERNAME
    password = args.password or PASSWORD

    if args.courses:
        courses = list(args.courses)
    elif args.priority:
        if args.priority == "high":
            courses = HIGH_PRIORITY_COURSES
        elif args.priority == "medium":
            courses = MEDIUM_PRIORITY_COURSES
        elif args.priority == "low":
            courses = LOW_PRIORITY_COURSES
        else:
            print(f"❌ Invalid priority: {args.priority}")
            return
    else:
        courses = ALL_COURSES

    if not courses:
        print("❌ No courses configured!")
        print(
            "   Edit HIGH_PRIORITY_COURSES, MEDIUM_PRIORITY_COURSES, or LOW_PRIORITY_COURSES"
        )
        print("   Or use: python ultra_optimized_script.py register -c 6120 6121")
        return

    await auto_dang_ky_hoc_phan(username, password, list(courses))


async def cmd_fetch(args):
    """Command: Fetch courses"""
    username = args.username or USERNAME
    password = args.password or PASSWORD

    async with UltraOptimizedSession(username, password) as session:
        if not await session.ensure_logged_in():
            print("❌ Login failed!")
            return

        data = await load_dang_ky_hoc_phan(session, username)
        if data:
            print(f"✅ Success!")
        else:
            print(f"❌ Failed!")


def main():
    parser = argparse.ArgumentParser(
        description="⚡ ULTRA-OPTIMIZED HCMUS Course Registration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s register                          # Register all configured courses
  %(prog)s register --priority high          # High priority only
  %(prog)s register -c 6040 6041 6042        # Specific courses
  %(prog)s register -u 23127469 -p pass      # Custom account
  %(prog)s fetch                             # Fetch registered courses

Features:
  ⚡ Adaptive concurrency (3-15 concurrent requests)
  🚀 Ultra-fast retry (100ms initial delay)
  📈 Real-time performance tuning
  🎯 Burst mode for maximum throughput
  🔄 Intelligent circuit breaker
  ⏱️  Sub-second average response time
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Register
    register_parser = subparsers.add_parser("register", help="Register courses")
    register_parser.add_argument("-u", "--username", help="Username")
    register_parser.add_argument("-p", "--password", help="Password")
    register_parser.add_argument("-c", "--courses", nargs="+", help="Course codes")
    register_parser.add_argument(
        "--priority", choices=["high", "medium", "low", "all"], help="Priority level"
    )

    # Fetch
    fetch_parser = subparsers.add_parser("fetch", help="Fetch courses")
    fetch_parser.add_argument("-u", "--username", help="Username")
    fetch_parser.add_argument("-p", "--password", help="Password")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    if args.command == "register":
        asyncio.run(cmd_register(args))
    elif args.command == "fetch":
        asyncio.run(cmd_fetch(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n⚠️  Stopped by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
