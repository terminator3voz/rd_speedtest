# Real-Debrid Speedtest

![Downloads](https://img.shields.io/github/downloads/terminator3voz/rd_speedtest/total?cacheSeconds=300)

A multi-threaded download speed testing application for Real-Debrid CDN servers, featuring both a graphical user interface (Tkinter) and a command-line interface (CLI). Test results can also be exported as PNG infographic images.

Ứng dụng đo tốc độ tải đa luồng tới các máy chủ CDN của Real-Debrid, có giao diện (Tkinter) và chế độ dòng lệnh. Có thể xuất kết quả thành ảnh infographic PNG.

Phương pháp đo bám theo `speedtest.js` của trang gốc: nhiều luồng song song, giai đoạn làm nóng, hệ số bù overhead 1.1, đơn vị chia cho 1048576.

## Tính năng

- Đo 12 máy chủ: AMS, RBX, HKG1, SGP1, SGPO1, SGPL1, KUL1, TYO1, LAX1, TLV1, MUM1, Cloudflare.
- Giao diện hiển thị tốc độ theo thời gian thực, thanh tiến độ, máy chủ nhanh nhất và tốc độ trung bình.
- Chế độ CLI, chọn máy chủ, số luồng và thời gian đo.
- Xuất ảnh kết quả dạng infographic PNG.
- Phím tắt: `F5` bắt đầu test, `Esc` dừng, `Ctrl+E` xuất ảnh.

## Download

Tải `rd_speedtest.exe` ở trang [Releases](https://github.com/terminator3voz/rd_speedtest/releases/latest) rồi chạy, không cần cài đặt và không cần quyền Administrator.

> **Lưu ý:** file exe đóng gói bằng PyInstaller chưa có chữ ký số, nên Windows SmartScreen hoặc phần mềm diệt virus có thể cảnh báo. Nếu bạn không yên tâm, hãy chạy từ mã nguồn hoặc tự build theo hướng dẫn bên dưới.

## Đóng gói thành file exe (PyInstaller)

```
pip install pyinstaller Pillow
pyinstaller --onefile --noconsole --add-data "rd_assets;rd_assets" rd_speedtest.py
```

Trên Linux/macOS dùng dấu `:` thay cho `;` trong `--add-data`.

Nếu không muốn nhúng asset vào exe, bỏ `--add-data` và để thư mục `rd_assets` cạnh file exe. Ứng dụng tìm `rd_assets` theo thứ tự: bên trong exe, cạnh exe, cạnh file `.py`, thư mục hiện tại.

## Yêu cầu

- Python 3.8 trở lên.
- Tkinter, nếu chạy giao diện. Trên Linux có thể cần `sudo apt install python3-tk`.
- [Pillow](https://pypi.org/project/Pillow/), chỉ khi dùng nút **Xuất kết quả** hoặc muốn hiện cờ trong giao diện:

```
py -m pip install Pillow
```

Phần đo tốc độ chỉ dùng thư viện chuẩn của Python.

## Cấu trúc thư mục

```
rd_speedtest.py       # ứng dụng chính
extract_assets.py     # tạo lại rd_assets từ ảnh mẫu (không bắt buộc)
rd_assets/            # asset cắt từ ảnh mẫu, bắt buộc khi xuất ảnh
```

## Sử dụng

Chạy giao diện:

```
python rd_speedtest.py
```

Chạy dòng lệnh:

```
python rd_speedtest.py --cli
python rd_speedtest.py --cli --only TYO1 SGP1 --duration 8 --connections 6
```

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--cli` | tắt | Chạy dòng lệnh, không mở giao diện |
| `--only` | tất cả | Chỉ test các máy chủ liệt kê, ví dụ `TYO1 SGP1` |
| `--duration` | `10` | Số giây đo, tính sau giai đoạn làm nóng 1.5 giây |
| `--connections` | `6` | Số kết nối song song |

### Tạo lại asset từ ảnh mẫu

```
python extract_assets.py <ảnh_mẫu.png> [thư_mục_đầu_ra]
```

Ảnh mẫu là ảnh 1024×1536 với bố cục 12 dòng. Script cắt và ghi `rd_assets/` cùng `layout.json`. Nếu đổi sang ảnh mẫu khác, cần chỉnh các tọa độ ở đầu `extract_assets.py` (`SEPS`, `TEXTS`, `TABLE_END`, `FOOT_START`...).

## Cách đo

1. Mở N luồng, luồng thứ i bắt đầu trễ 100 ms × i.
2. Mỗi luồng tải URL thêm số ngẫu nhiên để tránh cache. Tải xong thì mở lại ngay, lỗi thì chờ 100 ms rồi thử lại.
3. Mỗi 200 ms kiểm tra. Trong 1.5 giây đầu chỉ làm nóng, sau đó xóa bộ đếm byte.
4. Đo thêm `duration` giây. Tốc độ = byte / giây × 8 × 1.1 / 1048576.

Kết quả là Mbps theo công thức của trang gốc, đơn vị chia 1048576 nên không trùng hoàn toàn với Mbps thập phân (1.000.000).

## Lưu ý

- Kết quả phụ thuộc đường truyền, giờ cao điểm và việc có đang bật VPN hay không.
- Dự án không liên kết với Real-Debrid. Logo và tên thương hiệu thuộc về chủ sở hữu tương ứng.

## Giấy phép

MIT License, xem file [LICENSE](LICENSE).

## Tác giả

Real-Debrid Speedtest by Terminator3 @ voz.vn
