"""Auto course registration — CTĐB HCMUS portal — async + rich dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table


PROJECT_DIR = Path(__file__).resolve().parent

LOGIN_URL = "https://portal.ctdb.hcmus.edu.vn/Login?returnurl=%2f"
REGISTER_URL = "https://portal.ctdb.hcmus.edu.vn/dang-ky-hoc-phan/sinh-vien-clc"
PORTAL_HOST = "portal.ctdb.hcmus.edu.vn"
TRUST_COOKIE = "TwoFaTrustedDevice"
TRUST_FILE = PROJECT_DIR / ".2fa_device"  # persisted trusted-device cookie (7 days)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

AJAX_HEADERS = {
    "Accept": "*/*",
    "Referer": REGISTER_URL,
    "Origin": f"https://{PORTAL_HOST}",
    "X-OFFICIAL-REQUEST": "TRUE",
    "X-Requested-With": "XMLHttpRequest",
}

console = Console()
logger = logging.getLogger("registrar")


class Result(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RATE_LIMIT = "rate_limit"
    NEED_RELOGIN = "need_relogin"
    NETWORK = "network"
    UNKNOWN = "unknown"


@dataclass
class Config:
    username: str
    password: str
    courses: list[str]
    delay_min: float = 0.3
    delay_max: float = 0.8
    spam_delay_min: float = 0.5
    spam_delay_max: float = 0.5
    login_interval: int = 1200
    request_timeout: float = 5.0
    login_timeout: float = 30.0

    @staticmethod
    def _delay(name: str, raw: str) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError) as e:
            raise RuntimeError(f"{name} must be a number, got {raw!r}") from e
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(f"{name} must be a finite number greater than 0, got {raw!r}")
        return value

    @staticmethod
    def _course_ids(raw: str) -> list[str]:
        ids: list[str] = []
        for chunk in raw.split(","):
            course_id = chunk.strip()
            if not course_id:
                continue
            if not (course_id.isascii() and course_id.isdigit() and int(course_id) > 0):
                raise RuntimeError(
                    f"PORTAL_COURSES contains an invalid MaMG: {course_id!r} — "
                    "use the numeric class id (MaMG), not the subject code"
                )
            if course_id not in ids:
                ids.append(course_id)
        return ids

    @classmethod
    def from_env(cls, require_courses: bool = True) -> Config:
        load_dotenv()

        username = (os.environ.get("PORTAL_USERNAME") or "").strip()
        if not username:
            raise RuntimeError("Missing PORTAL_USERNAME in .env")
        password = os.environ.get("PORTAL_PASSWORD") or ""
        if not password.strip():
            raise RuntimeError("Missing PORTAL_PASSWORD in .env")

        courses = cls._course_ids(os.environ.get("PORTAL_COURSES", ""))
        if require_courses and not courses:
            raise RuntimeError("PORTAL_COURSES empty — set comma-separated MaMG list in .env")

        delay_min = cls._delay("PORTAL_DELAY_MIN", os.environ.get("PORTAL_DELAY_MIN", "0.3"))
        delay_max = cls._delay("PORTAL_DELAY_MAX", os.environ.get("PORTAL_DELAY_MAX", "0.8"))
        if delay_min > delay_max:
            raise RuntimeError(f"PORTAL_DELAY_MIN ({delay_min}) must not exceed PORTAL_DELAY_MAX ({delay_max})")

        spam_min = cls._delay("PORTAL_SPAM_DELAY_MIN", os.environ.get("PORTAL_SPAM_DELAY_MIN", "0.5"))
        spam_max = cls._delay("PORTAL_SPAM_DELAY_MAX", os.environ.get("PORTAL_SPAM_DELAY_MAX", "0.5"))
        if spam_min > spam_max:
            raise RuntimeError(f"PORTAL_SPAM_DELAY_MIN ({spam_min}) must not exceed PORTAL_SPAM_DELAY_MAX ({spam_max})")

        return cls(
            username=username,
            password=password,
            courses=courses,
            delay_min=delay_min,
            delay_max=delay_max,
            spam_delay_min=spam_min,
            spam_delay_max=spam_max,
        )


@dataclass
class WorkerState:
    course_id: str
    tag: str = "INIT"
    tag_style: str = "grey50"
    attempts: int = 0
    spam_count: int = 0
    message: str = "initializing..."
    completed: bool = False
    last_time: str = ""


class Dashboard:
    def __init__(self, config: Config):
        self.config = config
        self.workers: dict[str, WorkerState] = {cid: WorkerState(course_id=cid) for cid in config.courses}
        self.start = time.monotonic()

    def _elapsed(self) -> str:
        total = int(time.monotonic() - self.start)
        m, s = divmod(total, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def __rich__(self) -> Panel:
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(width=2)
        table.add_column(width=8, no_wrap=True)
        table.add_column(width=11, no_wrap=True)
        table.add_column(width=12, justify="right", no_wrap=True)
        table.add_column(width=10, justify="right", no_wrap=True)
        table.add_column(overflow="ellipsis", no_wrap=True, ratio=1)

        for w in self.workers.values():
            if w.completed:
                icon = "[bold green]✓[/]"
                cid = f"[bold green]{w.course_id}[/]"
            else:
                icon = "[cyan]●[/]"
                cid = f"[cyan]{w.course_id}[/]"
            counter = f"[dim]{w.attempts}[/]+[magenta]{w.spam_count}[/]"
            table.add_row(
                icon,
                cid,
                f"[{w.tag_style}]\\[{w.tag}][/]",
                counter,
                f"[dim]{w.last_time}[/]" if w.last_time else "",
                escape(w.message or ""),
            )

        done = sum(1 for w in self.workers.values() if w.completed)
        total_attempts = sum(w.attempts for w in self.workers.values())
        total_spam = sum(w.spam_count for w in self.workers.values())
        title = "[bold]Auto Course Registration — CTĐB HCMUS[/]"
        subtitle = (
            f"User [cyan]{self.config.username}[/]  "
            f"•  Elapsed [cyan]{self._elapsed()}[/]  "
            f"•  Tracked [cyan]{total_attempts}[/]  "
            f"•  Spam [magenta]{total_spam}[/]  "
            f"•  Done [cyan]{done}/{len(self.workers)}[/]"
        )
        return Panel(table, title=title, subtitle=subtitle, border_style="cyan")


class PortalClient:
    LOGIN_BACKOFF_CAP = 60.0
    SNAPSHOT_TTL = 2.0  # seconds a registered-course snapshot stays usable
    DEFAULT_COOLDOWN = 61.0
    # Deliberately narrow: a false positive here parks every worker for a minute
    # during the exact rush the script exists for.
    RATE_LIMIT_HINTS = ("quá nhanh", "qua nhanh", "quá nhiều", "qua nhieu")

    _MINUTES = re.compile(r"(\d+)\s*phút")
    _SECONDS = re.compile(r"(\d+)\s*giây")

    def __init__(self, config: Config):
        self.config = config
        self.client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=config.request_timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        self.cooldown_until = 0.0
        self.auth_generation = 0
        self._auth_lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()
        self._last_login = 0.0
        self._consecutive_failures = 0
        self._requests = 0
        self._mssv = ""
        self._snapshot: tuple[list[dict], list[dict]] | None = None
        self._snapshot_at = 0.0
        self._warned_other_class: set[str] = set()
        if TRUST_FILE.exists():
            self.client.cookies.set(TRUST_COOKIE, TRUST_FILE.read_text().strip(), domain=PORTAL_HOST, path="/")

    async def __aenter__(self) -> PortalClient:
        return self

    async def __aexit__(self, *_) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ auth

    async def ensure_logged_in(self) -> bool:
        async with self._auth_lock:
            elapsed = time.monotonic() - self._last_login
            if self._last_login and elapsed < self.config.login_interval:
                return True

            if self._consecutive_failures > 0:
                backoff = min(5.0 * (2 ** (self._consecutive_failures - 1)), self.LOGIN_BACKOFF_CAP)
                logger.warning(
                    "[yellow]\\[LOGIN][/] backoff %.1fs (attempt %d)", backoff, self._consecutive_failures + 1
                )
                await asyncio.sleep(backoff)

            self._last_login = 0.0
            if await self._login():
                self._last_login = time.monotonic()
                self._consecutive_failures = 0
                self.auth_generation += 1
                self._snapshot = None
                return True
            self._consecutive_failures += 1
            return False

    def invalidate_session(self, generation: int) -> None:
        """Drop the session, unless a newer login already replaced `generation`."""
        if generation != self.auth_generation:
            return
        self._last_login = 0.0
        trusted = self.client.cookies.get(TRUST_COOKIE)
        self.client.cookies.clear()
        if trusted:
            self.client.cookies.set(TRUST_COOKIE, trusted, domain=PORTAL_HOST, path="/")

    @staticmethod
    def _form_fields(html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        return {str(inp["name"]): str(inp.get("value", "")) for inp in soup.find_all("input") if inp.get("name")}

    async def _verify_2fa(self, page: httpx.Response) -> bool:
        """Portal sent a 6-digit code to the user's mail; ask for it and submit."""
        console.print("[yellow]2FA: portal đã gửi mã 6 số tới email của bạn.[/]")
        otp = (await asyncio.to_thread(input, "Nhập mã OTP: ")).strip()
        form = self._form_fields(page.text)
        form["dnn$ctr670$ViewTwoFactor$txtOtp"] = otp
        form["dnn$ctr670$ViewTwoFactor$chkTrustDevice"] = "on"
        try:
            resp = await self.client.post(
                str(page.url),
                files={k: (None, v) for k, v in form.items()},
                headers={"Referer": str(page.url), "Origin": f"https://{PORTAL_HOST}"},
                timeout=self.config.login_timeout,
            )
        except httpx.HTTPError as e:
            logger.warning("[red]\\[2FA][/] POST failed: %s", escape(str(e)))
            return False
        if "txtOtp" in resp.text:
            logger.warning("[red]\\[2FA][/] mã OTP bị từ chối")
            return False
        trusted = self.client.cookies.get(TRUST_COOKIE)
        if trusted:
            TRUST_FILE.write_text(trusted)
        return True

    async def _login(self) -> bool:
        logger.info("[blue]\\[LOGIN][/] [cyan]%s[/] logging in...", self.config.username)
        try:
            r = await self.client.get(LOGIN_URL, timeout=self.config.login_timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("[red]\\[LOGIN][/] [cyan]%s[/] GET failed: %s", self.config.username, escape(str(e)))
            return False

        form = self._form_fields(r.text)
        form["dnn$ctr$Login$Login_DNN$txtUsername"] = self.config.username
        form["dnn$ctr$Login$Login_DNN$txtPassword"] = self.config.password
        form["__EVENTTARGET"] = "dnn$ctr$Login$Login_DNN$cmdLogin"

        # multipart/form-data: (None, value) tuple → field without filename
        files = {k: (None, v) for k, v in form.items()}
        try:
            resp = await self.client.post(
                LOGIN_URL,
                files=files,
                headers={"Referer": LOGIN_URL, "Origin": f"https://{PORTAL_HOST}"},
                timeout=self.config.login_timeout,
            )
        except httpx.HTTPError as e:
            logger.warning("[red]\\[LOGIN][/] [cyan]%s[/] POST failed: %s", self.config.username, escape(str(e)))
            return False

        has_auth = any(name.lower() in (".aspxauth", ".dotnetnuke") for name in self.client.cookies)
        if has_auth and "txtOtp" in resp.text:
            has_auth = await self._verify_2fa(resp)
        if has_auth:
            logger.info("[green]\\[LOGIN][/] [cyan]%s[/] login successful", self.config.username)
        else:
            logger.warning(
                "[red]\\[LOGIN][/] [cyan]%s[/] login failed (status=%s)", self.config.username, resp.status_code
            )
        return has_auth

    # ------------------------------------------------------------- throttling

    def _next_request_id(self) -> int:
        self._requests += 1
        return self._requests

    @classmethod
    def _cooldown_for(cls, message: str) -> float:
        """How long the portal asked us to wait, plus a second of slack."""
        low = message.lower()
        minutes = cls._MINUTES.search(low)
        if minutes:
            return float(minutes.group(1)) * 60 + 1
        seconds = cls._SECONDS.search(low)
        if seconds:
            return float(seconds.group(1)) + 1
        if "một phút" in low or "mot phut" in low:
            return cls.DEFAULT_COOLDOWN
        return cls.DEFAULT_COOLDOWN

    def _start_cooldown(self, seconds: float) -> None:
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)

    async def _respect_cooldown(self) -> None:
        while True:
            remaining = self.cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    def _is_rate_limited(self, message: str) -> bool:
        low = message.lower()
        return any(hint in low for hint in self.RATE_LIMIT_HINTS)

    # ------------------------------------------------------------ registering

    async def _post_register(self, course_id: str, source: str) -> tuple[Result, str]:
        """One register POST, always logged on its own line, never raising on HTTP errors."""
        await self._respect_cooldown()
        generation = self.auth_generation
        number = self._next_request_id()
        files = {"action": (None, "addMonDangKy"), "data": (None, str(course_id))}
        started = time.monotonic()

        try:
            resp = await self.client.post(REGISTER_URL, files=files, headers=AJAX_HEADERS)
        except asyncio.CancelledError:
            logger.info(
                "[bold]Response:[/] #%d [magenta]%s[/] [yellow]CANCELLED[/] [cyan]%s[/] — request dropped",
                number,
                source,
                course_id,
            )
            raise
        except httpx.HTTPError as e:
            logger.warning(
                "[bold]Response:[/] #%d [magenta]%s[/] [red]NETWORK[/] [cyan]%s[/] — %s",
                number,
                source,
                course_id,
                escape(str(e)),
            )
            return Result.NETWORK, str(e)

        elapsed = int((time.monotonic() - started) * 1000)

        if resp.status_code == 429:
            self._start_cooldown(self._retry_after(resp))
            logger.warning(
                "[bold]Response:[/] #%d [magenta]%s[/] [yellow]HTTP 429[/] [cyan]%s[/] [dim]%dms[/] · "
                "rate limited · Body=%s",
                number,
                source,
                course_id,
                elapsed,
                escape(resp.text),
            )
            return Result.RATE_LIMIT, f"HTTP 429 — chờ {int(self.cooldown_until - time.monotonic())}s"

        if resp.status_code != 200:
            logger.warning(
                "[bold]Response:[/] #%d [magenta]%s[/] [red]HTTP %s[/] [cyan]%s[/] [dim]%dms[/] · Body=%s",
                number,
                source,
                resp.status_code,
                course_id,
                elapsed,
                escape(resp.text),
            )
            return Result.NETWORK, f"HTTP {resp.status_code}"

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "[bold]Response:[/] #%d [magenta]%s[/] [blue]HTTP %s[/] [cyan]%s[/] [dim]%dms[/] · "
                "non-JSON (session expired?)",
                number,
                source,
                resp.status_code,
                course_id,
                elapsed,
            )
            self.invalidate_session(generation)
            return Result.NEED_RELOGIN, "Non-JSON response (session expired?)"

        status = str(data.get("Status", "")) if isinstance(data, dict) else ""
        message = str(data.get("Message", "") or "") if isinstance(data, dict) else ""
        status_style = "bold green" if status == "Success" else "yellow" if status == "FAILED" else "red"
        logger.info(
            "[bold]Response:[/] #%d [magenta]%s[/] [dim]HTTP %s[/] [cyan]%s[/] [dim]%dms[/] · "
            "Status=[%s]%s[/] · Body=%s",
            number,
            source,
            resp.status_code,
            course_id,
            elapsed,
            status_style,
            status or "?",
            escape(resp.text),
        )

        if status == "Success":
            return Result.SUCCESS, message
        if status == "FAILED":
            if self._is_rate_limited(message):
                self._start_cooldown(self._cooldown_for(message))
                return Result.RATE_LIMIT, message
            return Result.FAILED, message
        return Result.UNKNOWN, f"{status}: {message}" if status else "Unexpected response body"

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        raw = resp.headers.get("Retry-After", "")
        try:
            return max(float(raw), 1.0)
        except (TypeError, ValueError):
            return PortalClient.DEFAULT_COOLDOWN

    async def register(self, course_id: str) -> tuple[Result, str]:
        """Tracked request: the caller waits for the parsed outcome."""
        return await self._post_register(course_id, "TRACKED")

    async def fire_register(self, course_id: str) -> tuple[Result, str]:
        """Background request. Its outcome still counts — a spam hit can win the slot."""
        return await self._post_register(course_id, "SPAM")

    # ------------------------------------------------------------ verifying

    async def _action(self, action: str, data: str = "") -> dict:
        """POST a read-only portal action; raises unless the portal returns Status=OK."""
        await self._respect_cooldown()
        number = self._next_request_id()
        started = time.monotonic()
        resp = await self.client.post(
            REGISTER_URL, files={"action": (None, action), "data": (None, data)}, headers=AJAX_HEADERS
        )
        elapsed = int((time.monotonic() - started) * 1000)

        payload = None
        if resp.status_code == 200:
            try:
                decoded = resp.json()
            except (json.JSONDecodeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                payload = decoded

        status = str((payload or {}).get("Status", "") or "?")
        logger.info(
            "[bold]Response:[/] #%d [blue]CHECK[/] [dim]HTTP %s[/] [cyan]%s[/] [dim]%dms[/] · Status=%s",
            number,
            resp.status_code,
            action,
            elapsed,
            escape(status),
        )
        if payload is None or payload.get("Status") != "OK":
            raise RuntimeError(f"{action} returned {status}")
        return payload

    async def _registered_snapshot(self) -> tuple[list[dict], list[dict]]:
        """(already registered, still open) rows, cached briefly so a spam burst asks once."""
        async with self._snapshot_lock:
            if self._snapshot is not None and time.monotonic() - self._snapshot_at < self.SNAPSHOT_TTL:
                return self._snapshot

            if not self._mssv:
                info = (await self._action("loadSinhVienInfo")).get("Results")
                if not isinstance(info, dict) or not info.get("MSSV"):
                    raise RuntimeError("loadSinhVienInfo is missing MSSV")
                self._mssv = str(info["MSSV"])

            results = (await self._action("loadDangKyHocPhan", self._mssv)).get("Results")
            if not isinstance(results, dict):
                raise RuntimeError("loadDangKyHocPhan is missing Results")

            snapshot = (
                [row for row in (results.get("ListDaDangKy") or []) if isinstance(row, dict)],
                [row for row in (results.get("ListChuaDangKy") or []) if isinstance(row, dict)],
            )
            self._snapshot = snapshot
            self._snapshot_at = time.monotonic()
            return snapshot

    @staticmethod
    def _same_subject(left: dict, right: dict) -> bool:
        for key in ("MaMH", "KyHieu"):
            a, b = left.get(key), right.get(key)
            if a not in (None, "", 0) and a == b:
                return True
        return False

    async def is_registered(self, course_id: str) -> bool:
        """Ask the portal whether this exact MaMG is in the student's registered list.

        The portal answers a refused registration with one message covering two very
        different cases ("lớp đã đủ số lượng hoặc bạn đã đăng ký môn này ở một lớp khác"),
        so its wording can never stand in for a real confirmation.
        """
        target = str(course_id)
        try:
            registered, available = await self._registered_snapshot()
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            logger.warning(
                "[yellow]\\[CHECK][/] [cyan]%s[/] không đối chiếu được danh sách đã đăng ký: %s", target, escape(str(e))
            )
            return False

        if any(str(row.get("MaMG", "")) == target for row in registered):
            return True

        wanted = next((row for row in available if str(row.get("MaMG", "")) == target), None)
        if wanted and target not in self._warned_other_class:
            held = next((row for row in registered if self._same_subject(wanted, row)), None)
            if held:
                self._warned_other_class.add(target)
                logger.warning(
                    "[yellow]\\[CHECK][/] [cyan]%s[/] bạn đang giữ môn %s ở lớp %s (MaMG %s) — "
                    "huỷ lớp đó trên portal thì mới đăng ký được lớp này",
                    target,
                    escape(str(held.get("KyHieu") or held.get("TenMH") or "?")),
                    escape(str(held.get("MaLopHP") or held.get("MaLopSH") or "?")),
                    escape(str(held.get("MaMG") or "?")),
                )
        return False


class Registrar:
    # A response matching one of these only *starts* a check — it never proves success.
    ALREADY_REGISTERED_HINTS = ("đã đăng ký", "da dang ky", "đã có trong", "da co trong")
    MAX_SPAM_IN_FLIGHT = 4

    def __init__(self, client: PortalClient, config: Config, dashboard: Dashboard):
        self.client = client
        self.config = config
        self.dashboard = dashboard

    async def run(self) -> None:
        if not await self.client.ensure_logged_in():
            logger.error("[red]Initial login failed, stopping.[/]")
            return

        with Live(self.dashboard, console=console, refresh_per_second=4):
            await asyncio.gather(
                *(self._course_loop(course_id) for course_id in self.config.courses), return_exceptions=True
            )

    async def _course_loop(self, course_id: str) -> None:
        """Run the tracked worker and the spam loop together until the course is settled."""
        tracked = asyncio.create_task(self._tracked_loop(course_id))
        spam = asyncio.create_task(self._spam_loop(course_id))
        try:
            await asyncio.wait({tracked, spam}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (tracked, spam):
                task.cancel()
            await asyncio.gather(tracked, spam, return_exceptions=True)

    async def _tracked_loop(self, course_id: str) -> None:
        state = self.dashboard.workers[course_id]
        while not state.completed:
            try:
                await self._try(course_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[red]Worker[/] [cyan]%s[/] unexpected exception: %s", course_id, escape(str(e)))
                state.tag = "ERROR"
                state.tag_style = "red"
                state.message = f"unexpected: {e}"
            if state.completed:
                break
            await asyncio.sleep(self._pause(self.config.delay_min, self.config.delay_max))

    async def _spam_loop(self, course_id: str) -> None:
        state = self.dashboard.workers[course_id]
        delay_desc = self._describe(self.config.spam_delay_min, self.config.spam_delay_max)
        logger.info("[magenta]\\[SPAM][/] [cyan]%s[/] started (delay=%s)", course_id, delay_desc)

        pending: set[asyncio.Task] = set()
        try:
            while not state.completed:
                pending = {task for task in pending if not task.done()}
                if len(pending) >= self.MAX_SPAM_IN_FLIGHT:
                    _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    continue
                pending.add(asyncio.create_task(self._try(course_id, background=True)))
                await asyncio.sleep(self._pause(self.config.spam_delay_min, self.config.spam_delay_max))
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            logger.info("[magenta]\\[SPAM][/] [cyan]%s[/] stopped", course_id)

    @staticmethod
    def _pause(low: float, high: float) -> float:
        return low if low >= high else random.uniform(low, high)

    @staticmethod
    def _describe(low: float, high: float) -> str:
        return f"{low}s fixed" if low >= high else f"{low}-{high}s random"

    async def _try(self, course_id: str, background: bool = False) -> None:
        state = self.dashboard.workers[course_id]

        await self._wait_out_cooldown(state)
        if state.completed:
            return

        if not await self.client.ensure_logged_in():
            if not background:
                state.tag = "AUTH"
                state.tag_style = "red"
                state.message = "not logged in"
            return

        state.last_time = datetime.now().strftime("%H:%M:%S")
        if background:
            state.spam_count += 1
            result, message = await self.client.fire_register(course_id)
        else:
            state.attempts += 1
            result, message = await self.client.register(course_id)

        await self._apply(course_id, result, message)

    async def _wait_out_cooldown(self, state: WorkerState) -> None:
        """Hold every worker while the portal's rate limit is in force."""
        while not state.completed:
            remaining = self.client.cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            state.tag = "LIMIT"
            state.tag_style = "yellow"
            await asyncio.sleep(remaining)

    async def _apply(self, course_id: str, result: Result, message: str) -> None:
        state = self.dashboard.workers[course_id]

        if result == Result.SUCCESS:
            state.tag = "OK"
            state.tag_style = "bold green"
            state.message = "Successfully registered"
            state.completed = True
            return

        # A response that lost the race must never downgrade a slot we already hold.
        if state.completed:
            return

        if result == Result.FAILED:
            if self._looks_already_registered(message) and await self.client.is_registered(course_id):
                state.tag = "ALREADY"
                state.tag_style = "green"
                state.message = "Đã có trong danh sách đăng ký (đã đối chiếu portal)"
                state.completed = True
                return
            state.tag = "RETRY"
            state.tag_style = "yellow"
            state.message = message
            return

        if result == Result.RATE_LIMIT:
            state.tag = "LIMIT"
            state.tag_style = "yellow"
            state.message = message
            return

        if result == Result.NEED_RELOGIN:
            state.tag = "RELOGIN"
            state.tag_style = "blue"
            state.message = message
            return

        state.tag = result.value.upper()
        state.tag_style = "red"
        state.message = message

    def _looks_already_registered(self, message: str) -> bool:
        low = message.lower()
        return any(hint in low for hint in self.ALREADY_REGISTERED_HINTS)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%H:%M:%S]",
        handlers=[RichHandler(console=console, show_path=False, show_level=False, markup=True, rich_tracebacks=True)],
    )
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def print_banner(config: Config) -> None:
    def delay_text(lo: float, hi: float) -> str:
        return f"{lo}s  (fixed)" if lo == hi else f"{lo}s – {hi}s  (random)"

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(style="cyan")
    table.add_row("User", config.username)
    table.add_row("Courses", f"{len(config.courses)}  ({', '.join(config.courses)})")
    table.add_row("Workers", f"1 tracked + 1 spam / course  ({2 * len(config.courses)} concurrent)")
    table.add_row("Spam in flight", f"{Registrar.MAX_SPAM_IN_FLIGHT} max / course")
    table.add_row("Login interval", f"{config.login_interval}s  ({config.login_interval // 60} min)")
    table.add_row("Tracked delay", delay_text(config.delay_min, config.delay_max))
    table.add_row("Spam delay", delay_text(config.spam_delay_min, config.spam_delay_max))
    table.add_row("Register timeout", f"{config.request_timeout}s")
    table.add_row("Login timeout", f"{config.login_timeout}s")

    console.print(
        Panel(table, title="[bold]Auto Course Registration — CTĐB HCMUS[/]", border_style="cyan", expand=False)
    )


async def main() -> None:
    setup_logging()
    config = Config.from_env()
    print_banner(config)
    dashboard = Dashboard(config)
    async with PortalClient(config) as client:
        await Registrar(client, config, dashboard).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[yellow]Stopped by user[/]")
