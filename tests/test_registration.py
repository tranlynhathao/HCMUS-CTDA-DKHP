import asyncio
import io
import tempfile
import time
import unittest
from contextlib import ExitStack
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from rich.console import Console
from rich.live import Live

import register_courses as app


def fields(request):
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
        for part in message.iter_parts()
    }


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("register_courses.TRUST_FILE", self.directory / ".2fa_device"))
        self.config = app.Config("student", "test_password", ["123"])
        self.requests = []
        self.reply = {"Status": "FAILED", "Message": "Không tìm thấy lớp học phần!"}
        real_client = httpx.AsyncClient

        def dispatch(request):
            self.requests.append(request)
            return self.handler(request)

        self.stack.enter_context(
            patch(
                "register_courses.httpx.AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(dispatch), **kwargs),
            )
        )

    def handler(self, request):
        return httpx.Response(200, json=self.reply)

    async def test_every_tracked_and_spam_request_has_own_response_log(self):
        async with app.PortalClient(self.config) as client:
            with self.assertLogs(app.logger, level="INFO") as logs:
                await client.register("123")
                await client.fire_register("123")
                await client.register("123")
        lines = [record.getMessage() for record in logs.records if "Response:" in record.getMessage()]
        self.assertEqual(len(lines), 3)
        for index, line in enumerate(lines, 1):
            self.assertIn(f"#{index}", line)
            self.assertIn("HTTP 200", line)
            self.assertIn("FAILED", line)
            self.assertIn("Không tìm thấy lớp học phần!", line)
            self.assertIn("Body=", line)
            self.assertIn("ms", line)
        self.assertIn("TRACKED", lines[0])
        self.assertIn("SPAM", lines[1])
        self.assertEqual([fields(request)["action"] for request in self.requests], ["addMonDangKy"] * 3)

    async def test_network_failure_and_cancellation_are_logged(self):
        async with app.PortalClient(self.config) as client:
            with patch.object(client.client, "post", side_effect=httpx.ReadTimeout("timeout")):
                with self.assertLogs(app.logger, level="INFO") as logs:
                    result, _ = await client.register("123")
                self.assertEqual(result, app.Result.NETWORK)
                self.assertIn("NETWORK", logs.output[0])
            with patch.object(client.client, "post", side_effect=asyncio.CancelledError()):
                with self.assertLogs(app.logger, level="INFO") as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await client.fire_register("123")
                self.assertIn("CANCELLED", logs.output[0])

    async def test_http_200_rate_limit_preserves_message_and_pauses(self):
        self.reply = {"Status": "FAILED", "Message": "Bạn thao tác quá nhanh. Vui lòng chờ một phút rồi thử lại."}
        async with app.PortalClient(self.config) as client:
            before = time.monotonic()
            result, message = await client.register("123")
            self.assertEqual(result, app.Result.RATE_LIMIT)
            self.assertEqual(message, self.reply["Message"])
            self.assertGreaterEqual(client.cooldown_until, before + 61)

    async def test_http_error_keeps_body_in_log(self):
        self.handler = lambda request: httpx.Response(503, json={"Status": "ERROR", "Message": "busy"})
        async with app.PortalClient(self.config) as client:
            with self.assertLogs(app.logger, level="INFO") as logs:
                result, _ = await client.register("123")
            self.assertEqual(result, app.Result.NETWORK)
            self.assertIn("HTTP 503", logs.output[0])
            self.assertIn("busy", logs.output[0])

    async def test_throttled_requests_do_not_send_before_deadline(self):
        clock = [100.0]
        sent = []

        async def advance(seconds):
            clock[0] += seconds

        self.handler = lambda request: sent.append(clock[0]) or httpx.Response(200, json=self.reply)
        async with app.PortalClient(self.config) as client:
            client.cooldown_until = 161.0
            with (
                patch("register_courses.time", SimpleNamespace(monotonic=lambda: clock[0])),
                patch.object(app.asyncio, "sleep", side_effect=advance),
            ):
                await client.fire_register("123")
        self.assertEqual(sent, [161.0])

    async def test_spam_success_is_returned(self):
        self.reply = {"Status": "Success", "Message": "OK"}
        async with app.PortalClient(self.config) as client:
            self.assertEqual(await client.fire_register("123"), (app.Result.SUCCESS, "OK"))

    async def test_json_array_is_handled_without_crashing(self):
        self.reply = []
        async with app.PortalClient(self.config) as client:
            result, _ = await client.register("123")
        self.assertEqual(result, app.Result.UNKNOWN)

    async def test_old_response_does_not_invalidate_new_login(self):
        async with app.PortalClient(self.config) as client:
            client.auth_generation = 2
            client._last_login = 123
            client.invalidate_session(1)
            self.assertEqual(client._last_login, 123)

    async def test_login_preserves_original_form_fields(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text='<input name="__VIEWSTATE" value="original-state">')
            values = fields(request)
            self.assertEqual(values["dnn$ctr$Login$Login_DNN$txtUsername"], "student")
            self.assertEqual(values["dnn$ctr$Login$Login_DNN$txtPassword"], "test_password")
            self.assertEqual(values["__EVENTTARGET"], "dnn$ctr$Login$Login_DNN$cmdLogin")
            self.assertEqual(values["__VIEWSTATE"], "original-state")
            return httpx.Response(200, text="txtOtp", headers={"Set-Cookie": ".DOTNETNUKE=fake; Path=/"})

        self.handler = handler
        async with app.PortalClient(self.config) as client:
            with patch.object(client, "_verify_2fa", new_callable=AsyncMock, return_value=True) as verify:
                self.assertTrue(await client.ensure_logged_in())
                verify.assert_awaited_once()

    async def test_markup_in_portal_message_is_not_executed(self):
        self.reply["Message"] = "[bold]server text[/bold]"
        async with app.PortalClient(self.config) as client:
            with self.assertLogs(app.logger, level="INFO") as logs:
                await client.register("123")
        self.assertIn(r"\[bold]", logs.output[0])


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = app.Config(
            "student", "test", ["123"], delay_min=0.001, delay_max=0.001, spam_delay_min=0.001, spam_delay_max=0.001
        )
        self.client = SimpleNamespace(
            ensure_logged_in=AsyncMock(return_value=True),
            register=AsyncMock(),
            fire_register=AsyncMock(),
            is_registered=AsyncMock(return_value=False),
            cooldown_until=0.0,
        )
        self.dashboard = app.Dashboard(self.config)
        self.registrar = app.Registrar(self.client, self.config, self.dashboard)

    async def test_tracked_and_spam_remain_concurrent_and_spam_can_finish(self):
        active = 0
        peak = 0
        primary_started = asyncio.Event()
        primary_cancelled = asyncio.Event()

        async def primary(course):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            primary_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
                primary_cancelled.set()

        async def spam(course):
            nonlocal active, peak
            await primary_started.wait()
            active += 1
            peak = max(peak, active)
            active -= 1
            return app.Result.SUCCESS, "OK"

        self.client.register.side_effect = primary
        self.client.fire_register.side_effect = spam
        await asyncio.wait_for(self.registrar._course_loop("123"), 1)
        self.assertTrue(self.dashboard.workers["123"].completed)
        self.assertTrue(primary_cancelled.is_set())
        self.assertGreaterEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertGreaterEqual(self.dashboard.workers["123"].spam_count, 1)

    async def test_spam_backlog_is_bounded_and_cancelled_on_stop(self):
        active = 0
        peak = 0

        async def spam(course):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        self.client.fire_register.side_effect = spam
        task = asyncio.create_task(self.registrar._spam_loop("123"))
        try:
            for _ in range(100):
                if active == self.registrar.MAX_SPAM_IN_FLIGHT:
                    break
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
            self.assertEqual(peak, self.registrar.MAX_SPAM_IN_FLIGHT)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(active, 0)

    async def test_late_failure_cannot_overwrite_success(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def tracked(course):
            started.set()
            await release.wait()
            return app.Result.FAILED, "late failure"

        self.client.register.side_effect = tracked
        self.client.fire_register.return_value = app.Result.SUCCESS, "OK"
        task = asyncio.create_task(self.registrar._try("123"))
        await started.wait()
        await self.registrar._try("123", background=True)
        release.set()
        await task
        state = self.dashboard.workers["123"]
        self.assertTrue(state.completed)
        self.assertEqual(state.tag, "OK")
        self.assertNotEqual(state.message, "late failure")

    async def test_negative_already_registered_message_is_not_success(self):
        self.client.register.return_value = (app.Result.FAILED, "Không thể đăng ký vì đã đăng ký lớp trùng lịch")
        await self.registrar._try("123")
        self.assertFalse(self.dashboard.workers["123"].completed)
        self.client.is_registered.assert_awaited_once_with("123")

    async def test_verified_already_registered_is_success(self):
        self.client.register.return_value = app.Result.FAILED, "Bạn đã đăng ký lớp này"
        self.client.is_registered.return_value = True
        await self.registrar._try("123")
        self.assertTrue(self.dashboard.workers["123"].completed)

    async def test_cooldown_does_not_increment_attempts_or_send(self):
        self.client.cooldown_until = time.monotonic() + 60
        task = asyncio.create_task(self.registrar._try("123", background=True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.fire_register.assert_not_awaited()
        self.assertEqual(self.dashboard.workers["123"].spam_count, 0)


class DisplayTests(unittest.TestCase):
    def test_original_dashboard_counters_and_history_are_both_visible(self):
        output = io.StringIO()
        terminal = Console(file=output, width=160)
        dashboard = app.Dashboard(app.Config("student", "unused", ["123"]))
        with patch("register_courses.console", terminal):
            with Live(dashboard, console=terminal, auto_refresh=False):
                terminal.print("Response: #1 TRACKED HTTP 200 FAILED")
                terminal.print("Response: #2 SPAM HTTP 200 Success")
        rendered = output.getvalue()
        self.assertIn("Response: #1", rendered)
        self.assertIn("Response: #2", rendered)
        self.assertIn("Tracked", rendered)
        self.assertIn("Spam", rendered)


if __name__ == "__main__":
    unittest.main()
