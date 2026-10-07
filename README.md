# HCMUS CTDA DKHP

Công cụ đăng ký học phần trên [portal CTĐB HCMUS](https://portal.ctdb.hcmus.edu.vn/dang-ky-hoc-phan/sinh-vien-clc).
Chạy trực tiếp script, với một tracked worker và một spam loop cho mỗi lớp như bản gốc.

## Cài đặt

Dùng Python 3.10 trở lên. Từ thư mục repo:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Trên Windows PowerShell, kích hoạt môi trường bằng `.venv\Scripts\Activate.ps1`.

## Cấu hình

Tạo `.env` theo `.env.example` nếu chưa có và điền tài khoản cùng mã lớp:

```dotenv
PORTAL_USERNAME=23xxxxxx
PORTAL_PASSWORD=your_password_here
PORTAL_COURSES=6557,6558
PORTAL_DELAY_MIN=0.2
PORTAL_DELAY_MAX=0.4
PORTAL_SPAM_DELAY_MIN=0.5
PORTAL_SPAM_DELAY_MAX=0.5
```

Các MaMG ở trên chỉ là ví dụ. Dùng đúng **MaMG** của lớp, không phải mã môn `KyHieu`.
Nếu cấu hình cũ dùng `USERNAME` / `PASSWORD`, đổi thành `PORTAL_USERNAME` / `PORTAL_PASSWORD`.
Mã lớp trùng lặp được loại bỏ trước khi tạo worker.

## Chạy

```bash
python register_courses.py
```

Hoặc gọi Python của môi trường ảo:

```bash
.venv/bin/python register_courses.py
```

Script nạp `.env`, đăng nhập, yêu cầu nhập OTP nếu portal cần xác thực, rồi đăng ký các lớp đã cấu hình.
Giữ cách đăng nhập và form OTP của bản gốc. Cookie thiết bị tin cậy nằm trong `.2fa_device` ở thư mục repo,
nên chạy từ thư mục nào cũng dùng lại đúng cookie đó.

Mỗi lớp có hai luồng:

- **Tracked:** gửi một request, chờ phản hồi, cập nhật kết quả rồi chờ theo `PORTAL_DELAY_MIN/MAX`.
- **Spam:** tạo request nền theo `PORTAL_SPAM_DELAY_MIN/MAX`, có thể chạy song song với tracked và các request nền khác.

Hai delay bằng nhau tạo khoảng chờ cố định; khác nhau tạo khoảng chờ ngẫu nhiên trong khoảng cấu hình.
Các giá trị phải hữu hạn, lớn hơn 0 và MIN không vượt MAX.
Không có hàng đợi tuần tự chung và không tự thay đổi delay cấu hình sau một đợt giới hạn.

## Theo dõi từng request

Mỗi request đăng ký đều có một dòng kết quả trong lịch sử terminal, kể cả request nền:

```text
[16:20:01] Response: #12 TRACKED HTTP 200 6463 83ms · Status=FAILED · Body={...}
[16:20:01] Response: #13 SPAM HTTP 200 6463 76ms · Status=Success · Body={...}
```

Log ghi số thứ tự, nguồn request, HTTP status, MaMG, thời gian phản hồi và JSON trả về.
Lỗi mạng được ghi là `NETWORK`; request bị hủy khi dừng được ghi là `CANCELLED`.
Request đối chiếu danh sách đã đăng ký được ghi với nhãn `CHECK`.

Dashboard gốc vẫn nằm dưới log, với bộ đếm `Tracked + Spam`, trạng thái từng môn và kết quả gần nhất.
Dừng chương trình bằng `Ctrl+C`, sau đó kiểm tra kết quả trên portal.

## Các sửa lỗi xử lý

- Kết quả thành công của request nền được ghi nhận ngay.
- Phản hồi thất bại đến chậm không ghi đè trạng thái đã thành công.
- Khi portal trả HTTP 429 hoặc thông báo JSON “thao tác quá nhanh”, cả tracked và spam tạm dừng theo thời gian server yêu cầu. “Chờ một phút” tương ứng ít nhất 61 giây, sau đó tiếp tục dùng delay cũ.
- Spam loop giữ tối đa 4 request đang chờ mỗi lớp để tránh tích tụ task khi mạng chậm. Đây là giới hạn request đồng thời, không phải số lần thử tối đa.
- Khi thành công hoặc dừng chương trình, các task đang chờ được hủy và thu dọn.
- Lỗi mạng được chờ lại tạm thời. Phản hồi chứa “đã đăng ký” được đối chiếu với danh sách đăng ký để tránh hiểu nhầm thông báo trùng lịch.

Các lượt đã gửi trước khi nhận thông báo giới hạn vẫn có thể trả phản hồi. Script không gửi thêm trong thời gian chờ.
Chỉ chạy một bản script cho cùng tài khoản để các tiến trình khác không tiếp tục gây giới hạn.

## Lấy JSON khi cần

```bash
python fetch_courses.py
```

Lệnh lưu dữ liệu vào `monhoc_current.json` ở gốc repo. Có thể chọn tên khác:

```bash
python fetch_courses.py -o monhoc.json
```

Chỉ cần tài khoản; `PORTAL_COURSES` có thể để trống khi lấy JSON.
Lệnh chỉ đọc dữ liệu, không đăng ký hoặc hủy lớp. Phản hồi lỗi không ghi đè JSON cũ.

Tra `MaMG`, `TenMH`, `MaLopSH`, `LichHocLT` và `SoSVTT` trong `Results.ListChuaDangKy`.
Tên môn trong file cũ không chứng minh MaMG đó vẫn hợp lệ ở đợt đăng ký hiện tại.
Nếu portal báo “Không tìm thấy lớp học phần”, lấy dữ liệu mới và kiểm tra MaMG trong `.env`.

## Kiểm tra mã nguồn

```bash
python -m unittest discover -s tests -v
```

Test dùng tài khoản giả và HTTP mock, không gửi yêu cầu thật lên portal.
Git bỏ qua `.env`, cookie thiết bị, môi trường ảo và `monhoc_current.json`.
