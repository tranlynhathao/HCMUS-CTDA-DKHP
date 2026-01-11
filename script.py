import aiohttp
import asyncio
from bs4 import BeautifulSoup
import json
import argparse
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

USERNAME = os.getenv("USERNAME", "mssv_here")
PASSWORD = os.getenv("PASSWORD", "your_password_here")
DANH_SACH_MON_HOC = []

LOGIN_URL = "https://portal.ctdb.hcmus.edu.vn/Login?returnurl=%2f"
DANG_KY_URL = "https://portal.ctdb.hcmus.edu.vn/dang-ky-hoc-phan/sinh-vien-clc"
LOGIN_INTERVAL = 10 * 60
DELAY_TIMEOUT = 1

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8,ko;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
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


def create_multipart_data(form_data: dict) -> aiohttp.FormData:
    """Tạo multipart form data cho aiohttp"""
    data = aiohttp.FormData()
    for key, value in form_data.items():
        data.add_field(key, str(value))
    return data


class AsyncSession:
    """
    Async session manager
    Quản lý session và cookies cho các request async
    """

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session: aiohttp.ClientSession | None = None
        self.last_login_time = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self.init_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def init_session(self):
        """Khởi tạo aiohttp session"""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                headers=BASE_HEADERS, connector=connector, timeout=timeout
            )

    async def close(self):
        """Đóng session"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def ensure_logged_in(self) -> bool:
        """Đảm bảo đã login, login lại nếu cần"""
        async with self._lock:
            current_time = asyncio.get_event_loop().time()

            if (
                self.session is None
                or (current_time - self.last_login_time) > LOGIN_INTERVAL
            ):
                return await self._do_login()

            return True

    async def force_relogin(self) -> bool:
        """Buộc login lại"""
        async with self._lock:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Force re-login...")
            return await self._do_login()

    async def _do_login(self) -> bool:
        """Thực hiện login"""
        max_retries = 3

        for attempt in range(max_retries):
            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔐 Đăng nhập (lần {attempt + 1})..."
            )

            # Reset session
            await self.close()
            await self.init_session()

            if not await self._login():
                print(f"Login thất bại, thử lại sau 5s...")
                await asyncio.sleep(5)
                continue

            if not await self._check_dang_ky_page():
                print(f"Không thể truy cập trang đăng ký, thử lại sau 5s...")
                await asyncio.sleep(5)
                continue

            self.last_login_time = asyncio.get_event_loop().time()
            print(f"✅ Login thành công!")
            return True

        print(f"❌ Login thất bại sau {max_retries} lần thử!")
        return False

    async def _login(self) -> bool:
        """Thực hiện login"""
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] login voi user: {self.username}"
        )

        try:
            async with self.session.get(LOGIN_URL) as response:
                response.raise_for_status()
                html = await response.text()

            soup = BeautifulSoup(html, "html.parser")

            form_data = {}
            for inp in soup.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                value = inp.get("value", "")
                form_data[name] = value
                print(f"Found input: {name} = {value}")

            form_data["dnn$ctr$Login$Login_DNN$txtUsername"] = self.username
            form_data["dnn$ctr$Login$Login_DNN$txtPassword"] = self.password
            form_data["__EVENTTARGET"] = "dnn$ctr$Login$Login_DNN$cmdLogin"

            multipart_data = create_multipart_data(form_data)

            headers = {
                "Referer": LOGIN_URL,
                "Origin": "https://portal.ctdb.hcmus.edu.vn",
            }

            async with self.session.post(
                LOGIN_URL, data=multipart_data, headers=headers, allow_redirects=True
            ) as post_resp:
                print("Status:", post_resp.status)
                print("Final URL:", str(post_resp.url))
                print("Cookies after login:")
                for cookie in self.session.cookie_jar:
                    print(f"  {cookie.key} = {cookie.value}")

                cookie_names = [
                    cookie.key.lower() for cookie in self.session.cookie_jar
                ]
                if any(".aspxauth" in name or "auth" in name for name in cookie_names):
                    print("login complete have cookie auth.")
                    return True
                else:
                    print("login fail.")
                    return False

        except Exception as e:
            print(f"Login error: {e}")
            return False

    async def _check_dang_ky_page(self) -> bool:
        """Kiểm tra trang đăng ký học phần"""
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Kiểm tra trang đăng ký học phần..."
        )

        try:
            async with self.session.get(DANG_KY_URL) as response:
                response.raise_for_status()
                html = await response.text()

                print(f"\ntruy cap dang ky hoc phan")
                print(f"Status: {response.status}")
                print(f"Final URL: {str(response.url)}")

                if "Sinh viên CLC" in html:
                    print("Đã vào trang đăng ký học phần thành công!")
                    return True
                else:
                    print("Chưa vào được trang đăng ký học phần")
                    return False

        except Exception as e:
            print(f"Check dang ky page error: {e}")
            return False


async def load_dang_ky_hoc_phan(async_session: AsyncSession, username: str):
    """
    Load danh sách đăng ký học phần của sinh viên
    Gửi POST request với action loadDangKyHocPhan
    Lưu kết quả vào file [Timestamp]_Courses.json
    """
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Load danh sách đăng ký học phần cho user: {username}"
    )

    form_data = aiohttp.FormData()
    form_data.add_field("action", "loadDangKyHocPhan")
    form_data.add_field("data", str(username))

    try:
        async with async_session.session.post(
            DANG_KY_URL, data=form_data, headers=AJAX_HEADERS
        ) as response:
            print(f"Response Status: {response.status}")

            if response.status == 200:
                text = await response.text()
                try:
                    json_data = json.loads(text)

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{timestamp}_Courses.json"

                    with open(filename, "w", encoding="utf-8") as f:
                        json.dump(json_data, f, ensure_ascii=False, indent=2)

                    print(f"✅ Đã lưu dữ liệu vào file: {filename}")
                    return json_data

                except json.JSONDecodeError as e:
                    print(f"❌ Response không phải JSON: {e}")
                    print(f"Response text: {text[:500]}")
                    return None
            else:
                print(f"❌ HTTP request thất bại! Status: {response.status}")
                return None

    except Exception as e:
        print(f"Load dang ky hoc phan error: {e}")
        return None


async def dang_ky_mon_hoc(async_session: AsyncSession, ma_lop_hp: str) -> str:
    """
    Đăng ký một môn học
    Returns: "Success", "ALREADY_REGISTERED", "FAILED", "NEED_RELOGIN", "ERROR", "UNKNOWN"
    """
    print(f"dang ky mon hoc : {ma_lop_hp}")

    form_data = aiohttp.FormData()
    form_data.add_field("action", "addMonDangKy")
    form_data.add_field("data", str(ma_lop_hp))

    try:
        async with async_session.session.post(
            DANG_KY_URL, data=form_data, headers=AJAX_HEADERS
        ) as response:
            print(f"Response Status: {response.status}")
            text = await response.text()
            print(f"Response Text: {text}")

            if response.status == 200:
                try:
                    json_response = json.loads(text)
                    status = json_response.get("Status", "")
                    message = json_response.get("Message", "")

                    if status == "Success":
                        print(f"Lụm được một môn {message}")
                        return "Success"
                    elif status == "FAILED":
                        if "Bạn đã đăng ký môn này" in message:
                            print(f"📌 Môn này đã được đăng ký trước đó rồi!")
                            return "ALREADY_REGISTERED"
                        print(f"Đăng ký thất bại: {message}")
                        return "FAILED"
                    else:
                        print(f"không xác định: {status} - {message}")
                        return "UNKNOWN"

                except json.JSONDecodeError:
                    print("Response không phải JSON, restart login")
                    return "NEED_RELOGIN"
            else:
                print("HTTP request thất bại!")
                return "ERROR"

    except Exception as e:
        print(f"Dang ky mon hoc error: {e}")
        return "ERROR"


async def dang_ky_worker(async_session: AsyncSession, ma_mon: str, results: dict):
    """
    Worker coroutine để đăng ký một môn học
    Retry cho đến khi thành công hoặc đã đăng ký
    """
    task_name = asyncio.current_task().get_name()
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] [{task_name}] 🚀 Bắt đầu đăng ký môn: {ma_mon}"
    )

    while True:
        if not await async_session.ensure_logged_in():
            print(f"[{task_name}] ❌ Không thể login, thử lại sau 5s...")
            await asyncio.sleep(5)
            continue

        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] [{task_name}] 📚 Đăng ký môn: {ma_mon}"
        )

        try:
            result = await dang_ky_mon_hoc(async_session, ma_mon)

            if result == "Success":
                print(f"[{task_name}] 🎊 Môn {ma_mon} đã đăng ký thành công!")
                results[ma_mon] = "Success"
                return
            elif result == "ALREADY_REGISTERED":
                print(f"[{task_name}] ✅ Môn {ma_mon} đã có trong danh sách đăng ký.")
                results[ma_mon] = "ALREADY_REGISTERED"
                return
            elif result == "NEED_RELOGIN":
                print(f"[{task_name}] 🔄 Session hết hạn, yêu cầu re-login...")
                await async_session.force_relogin()
                continue

            # Delay trước khi thử lại
            print(f"[{task_name}] ⏳ Chờ {DELAY_TIMEOUT}s trước khi thử lại...")
            await asyncio.sleep(DELAY_TIMEOUT)

        except Exception as e:
            print(f"[{task_name}] Lỗi khi đăng ký môn {ma_mon}: {e}")
            await asyncio.sleep(1)
            continue


async def auto_dang_ky_hoc_phan(username: str, password: str, danh_sach_mon_hoc: list):
    """
    Tự động đăng ký học phần
    Chạy tất cả môn học song song bằng asyncio tasks
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] bắt đầu Tự động đăng ký học phần")
    print(f"Danh sách môn cần đăng ký: {danh_sach_mon_hoc}")

    if len(danh_sach_mon_hoc) == 0:
        print("❌ Không có môn nào để đăng ký!")
        return

    async with AsyncSession(username, password) as async_session:
        # Login trước
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔐 Khởi tạo session...")
        if not await async_session.ensure_logged_in():
            print("❌ Không thể login, dừng chương trình!")
            return

        results = {}

        tasks = [
            asyncio.create_task(
                dang_ky_worker(async_session, ma_mon, results), name=f"Task-{ma_mon}"
            )
            for ma_mon in danh_sach_mon_hoc
        ]

        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] 🎬 Khởi động {len(tasks)} tasks..."
        )

        await asyncio.gather(*tasks)

        print(f"\n{'=' * 50}")
        print(f"🎉 [{datetime.now().strftime('%H:%M:%S')}] KẾT QUẢ ĐĂNG KÝ:")
        print(f"{'=' * 50}")
        for ma_mon, result in results.items():
            status_icon = "✅" if result in ["Success", "ALREADY_REGISTERED"] else "❌"
            print(f"{status_icon} Môn {ma_mon}: {result}")

        success_count = sum(
            1 for r in results.values() if r in ["Success", "ALREADY_REGISTERED"]
        )
        print(
            f"\n📊 Tổng kết: {success_count}/{len(danh_sach_mon_hoc)} môn đăng ký thành công"
        )


async def cmd_register(args):
    """Command: Đăng ký môn học"""
    username = args.username or USERNAME
    password = args.password or PASSWORD
    courses = args.courses if args.courses else DANH_SACH_MON_HOC

    print(f"📚 Bắt đầu đăng ký môn học cho user: {username}")
    print(f"📋 Danh sách môn: {courses}")

    await auto_dang_ky_hoc_phan(username, password, list(courses))


async def cmd_fetch(args):
    """Command: Lấy danh sách môn học đã đăng ký"""
    username = args.username or USERNAME
    password = args.password or PASSWORD

    print(f"📥 Lấy danh sách môn học cho user: {username}")

    async with AsyncSession(username, password) as async_session:
        if not await async_session.ensure_logged_in():
            print("❌ Login thất bại!")
            return

        data = await load_dang_ky_hoc_phan(async_session, username)
        if data:
            print(f"✅ Đã lấy và lưu dữ liệu thành công!")
        else:
            print(f"❌ Không thể lấy dữ liệu!")


def main():
    parser = argparse.ArgumentParser(
        description="🎓 Tool đăng ký học phần HCMUS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  %(prog)s register                          # Đăng ký với config mặc định
  %(prog)s register -c 6040 6041 6042        # Đăng ký các môn cụ thể
  %(prog)s register -u 23127469 -p mypass    # Đăng ký với tài khoản khác
  %(prog)s fetch                             # Lấy danh sách môn học
  %(prog)s fetch -u 23127469 -p mypass       # Lấy với tài khoản khác

Chạy fetch -> xem file JSON kết quả mục MaMG để biết mã môn học cần đăng ký -> thêm vào lệnh register hoặc script.
""",
    )

    subparsers = parser.add_subparsers(dest="command", help="Các lệnh có sẵn")

    # Register command
    register_parser = subparsers.add_parser("register", help="Đăng ký môn học")
    register_parser.add_argument(
        "-u", "--username", help="Tên đăng nhập (mặc định từ config)"
    )
    register_parser.add_argument(
        "-p", "--password", help="Mật khẩu (mặc định từ config)"
    )
    register_parser.add_argument(
        "-c", "--courses", nargs="+", help="Danh sách mã môn học cần đăng ký"
    )

    # Fetch command
    fetch_parser = subparsers.add_parser(
        "fetch", help="Lấy danh sách môn học đã đăng ký"
    )
    fetch_parser.add_argument(
        "-u", "--username", help="Tên đăng nhập (mặc định từ config)"
    )
    fetch_parser.add_argument("-p", "--password", help="Mật khẩu (mặc định từ config)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    # Run commands
    if args.command == "register":
        asyncio.run(cmd_register(args))
    elif args.command == "fetch":
        asyncio.run(cmd_fetch(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] Dừng chương trình bởi người dùng"
        )
    except Exception as e:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Lỗi không mong muốn: {e}")
