#!/usr/bin/env python3
"""Real-Debrid speedtest (multi-connection only) - GUI app.

Chạy GUI:   python rd_speedtest.py
Chạy CLI:   python rd_speedtest.py --cli [--only TYO1 SGP1] [--duration 8] [--connections 6]

Chỉ dùng thư viện chuẩn của Python (Tkinter nếu chạy GUI).
Nút "Xuất kết quả" (infographic PNG) cần thêm Pillow: py -m pip install Pillow
và thư mục rd_assets/ (asset trích từ ảnh mẫu) đặt cạnh file này (hoặc cạnh file exe).
Đóng gói 1 file:  pyinstaller --onefile --noconsole --add-data "rd_assets;rd_assets" rd_speedtest.py
"""
import argparse
import datetime
import queue
import random
import socket
import sys
import ssl
import threading
import time
import urllib.error
import urllib.request
import json
import os

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError:  # chỉ cần Pillow khi dùng nút Xuất kết quả
    Image = ImageDraw = ImageOps = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # chạy được chế độ --cli dù không có Tkinter
    tk = None

_CanvasBase = tk.Canvas if tk else object   # để file vẫn import được khi thiếu Tkinter

APP_TITLE = "Real-Debrid Speedtest by Terminator3 @ voz.vn"
USER_AGENT = "Mozilla/5.0 (RD-Speedtest-GUI)"
CHUNK = 256 * 1024
DEFAULT_CONNECTIONS = 6   # giống trang gốc
DEFAULT_DURATION = 10.0   # time_dl: số giây đo SAU giai đoạn làm nóng
WARMUP = 1.5              # time_dlGraceTime: giai đoạn làm nóng, bộ đếm bị xóa khi kết thúc
OVERHEAD = 1.1            # overheadCompensationFactor của trang gốc
MBIT = 1048576.0          # trang gốc chia cho 1048576 (không phải 1.000.000)
STAGGER = 0.1             # luồng thứ i bắt đầu trễ 100 ms * i
TICK = 0.2                # chu kỳ kiểm tra 200 ms
RETRY_DELAY = 0.1         # lỗi -> thử lại sau 100 ms (xhr_ignoreErrors = 1)

# (tên, quốc gia, URL file test) - lấy từ speedtest.js của real-debrid.com/speedtest
SERVERS = [
    ("AMS", "Netherlands", "https://45.download.real-debrid.com/speedtest/testDefault.rar"),
    ("RBX", "France", "https://rbx.download.real-debrid.com/speedtest/test.rar"),
    ("HKG1", "Hong Kong", "https://hkg1.download.real-debrid.com/speedtest/test.rar"),
    ("SGP1", "Singapore", "https://sgp1.download.real-debrid.com/speedtest/test.rar"),
    ("SGPO1", "Singapore", "https://sgpo1.download.real-debrid.com/speedtest/test.rar"),
    ("SGPL1", "Singapore", "https://sgpl1.download.real-debrid.com/speedtest/test.rar"),
    ("KUL1", "Malaysia", "https://kul1.download.real-debrid.com/speedtest/test.rar"),
    ("TYO1", "Japan", "https://tyo1.download.real-debrid.com/speedtest/test.rar"),
    ("LAX1", "United States", "https://lax1.download.real-debrid.com/speedtest/test.rar"),
    ("TLV1", "Israel", "https://tlv1.download.real-debrid.com/speedtest/test.rar"),
    ("MUM1", "India", "https://mum1.download.real-debrid.com/speedtest/test.rar"),
    ("Cloudflare", "Global (Cloudflare)", "https://45.download.real-debrid.cloud/speedtest/testCloudflare.rar"),
]


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def friendly_error(exc):
    """Rút gọn lỗi mạng thành câu ngắn dễ đọc."""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %d" % exc.code
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Hết thời gian chờ"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return "Hết thời gian chờ"
        if isinstance(exc.reason, socket.gaierror):
            return "Không phân giải được tên miền"
        return "Không kết nối được"
    if isinstance(exc, ssl.SSLError):
        return "Lỗi SSL"
    return type(exc).__name__


def make_test_url(url):
    """Thêm phần ngẫu nhiên vào URL để tránh cache, giống speedtest.js gốc.

    Gốc: URL chứa ".download" -> url + "/" + Math.random(); ngược lại -> url + "?" + Math.random().
    """
    salt = repr(random.random())
    if url.find(".download") > 0:
        return "%s/%s" % (url, salt)
    return "%s?%s" % (url, salt)


def measure(url, connections, duration, stop, on_progress=None, warmup=WARMUP):
    """Đo tốc độ tải đa luồng tới `url`, cùng cách đo với speedtest.js của real-debrid.com.

    Cách đo (khớp bản gốc):
      * Mở `connections` luồng, luồng thứ i bắt đầu trễ 1 + 100*i ms.
      * Mỗi luồng tải URL + "/" + số ngẫu nhiên; tải xong (onload) thì mở lại ngay,
        gặp lỗi thì chờ 100 ms rồi mở lại (xhr_ignoreErrors = 1).
      * Mỗi 200 ms kiểm tra. Trong `warmup` giây đầu (time_dlGraceTime = 1.5) chỉ để làm nóng:
        hết giai đoạn này, nếu đã có dữ liệu thì xóa bộ đếm byte và đặt lại mốc thời gian.
      * Sau đó đo thêm `duration` giây (time_dl = 10); tốc độ = byte / giây * 8 * 1.1 / 1048576
        (overheadCompensationFactor = 1.1, đơn vị chia 1048576 chứ không phải 1.000.000).

    Trả về tốc độ (Mbps theo công thức trên), None nếu bị dừng giữa chừng,
    hoặc ném RuntimeError nếu suốt phép đo không nhận được byte nào.
    """
    counters = [0] * connections
    errors = []
    finished = threading.Event()
    ctx = ssl.create_default_context()

    def should_quit():
        return stop.is_set() or finished.is_set()

    def worker(i):
        # setTimeout(..., 1 + 100 * i)
        if finished.wait(0.001 + STAGGER * i):
            return
        while not should_quit():
            try:
                req = urllib.request.Request(make_test_url(url), headers={
                    "User-Agent": USER_AGENT,
                    "Cache-Control": "no-cache",
                    "Accept-Encoding": "identity",
                })
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    # read1: trả về ngay khi có dữ liệu, giống onprogress của XHR.
                    read = getattr(resp, "read1", resp.read)
                    while not should_quit():
                        data = read(CHUNK)
                        if not data:
                            break  # onload -> mở kết nối mới ngay
                        counters[i] += len(data)
            except Exception as exc:  # noqa: BLE001
                if should_quit():
                    return
                errors.append(friendly_error(exc))
                finished.wait(RETRY_DELAY)  # onerror -> thử lại sau 100 ms

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(connections)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()

    def to_mbps(nbytes, seconds):
        return nbytes / seconds * 8 * OVERHEAD / MBIT

    base_t, base_b = t0, 0       # mốc đo (JS: biến r và s)
    measuring = False            # JS: biến n (đã qua giai đoạn grace)
    speed = None
    total = 0
    total_time = warmup + duration
    cancelled = False
    next_tick = t0 + TICK
    while True:
        if stop.wait(max(0.0, next_tick - time.perf_counter())):
            cancelled = True
            break
        next_tick += TICK
        now = time.perf_counter()
        total = sum(counters)
        t = now - base_t
        if t <= 0:
            continue
        if not measuring:
            if t > warmup:
                if total - base_b > 0:      # JS: if (s > 0) { r = now; s = 0 }
                    base_t, base_b = now, total
                measuring = True
            shown = to_mbps(total, now - t0)   # chỉ để hiển thị trong lúc làm nóng
        else:
            speed = to_mbps(total - base_b, t)
            shown = speed
        if on_progress:
            on_progress(shown, min((now - t0) / total_time, 1.0))
        if measuring and speed is not None and t > duration:
            break

    finished.set()
    deadline = time.perf_counter() + 1.0
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.perf_counter()))

    if cancelled:
        return None
    if total == 0:
        raise RuntimeError(errors[-1] if errors else "Không nhận được dữ liệu")
    return speed


# --------------------------------------------------------------------------
# Xuất kết quả -> infographic PNG (cần Pillow: py -m pip install Pillow)
# --------------------------------------------------------------------------
def _find_asset_dir():
    """Tìm thư mục rd_assets: gói trong exe (PyInstaller --add-data), cạnh file exe/script, hoặc thư mục hiện tại."""
    cands = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "rd_assets"))
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "rd_assets"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rd_assets"))
    cands.append(os.path.join(os.getcwd(), "rd_assets"))
    for c in cands:
        if os.path.isfile(os.path.join(c, "layout.json")):
            return c
    return cands[0]


ASSET_DIR = _find_asset_dir()

COUNTRY = {n: c for n, c, _ in SERVERS}
FLAG_CODE = {"AMS": "nl", "RBX": "fr", "HKG1": "hk", "SGP1": "sg", "SGPO1": "sg", "SGPL1": "sg",
             "KUL1": "my", "TYO1": "jp", "LAX1": "us", "TLV1": "il", "MUM1": "in", "Cloudflare": "cf"}


def get_hanoi_time():
    """Lấy giờ hiện tại theo múi giờ Hà Nội từ đồng hồ hệ thống."""
    if ZoneInfo:
        try:
            return datetime.datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
        except Exception:
            pass
    # Fallback: máy đặt múi giờ Việt Nam thì datetime.now() là giờ địa phương.
    return datetime.datetime.now()

def draw_flag(image, draw, cx, cy, radius, code):
    """Vẽ cờ tròn tối giản trực tiếp bằng Pillow, không phụ thuộc emoji/font."""
    x0, y0, x1, y1 = cx-radius, cy-radius, cx+radius, cy+radius
    # Vẽ cờ trong mặt nạ tròn
    size = radius * 2
    flag = Image.new("RGB", (size, size), "white")
    fd = ImageDraw.Draw(flag)
    if code == "nl":
        fd.rectangle((0, 0, size, size//3), fill=(210, 36, 62))
        fd.rectangle((0, size*2//3, size, size), fill=(25, 82, 170))
    elif code == "fr":
        fd.rectangle((0, 0, size//3, size), fill=(25, 78, 166))
        fd.rectangle((size*2//3, 0, size, size), fill=(235, 56, 68))
    elif code == "sg":
        fd.rectangle((0, 0, size, size//2), fill=(230, 34, 65))
        fd.rectangle((0, size//2, size, size), fill=(250, 250, 250))
        fd.ellipse((size*.18, size*.12, size*.52, size*.46), fill="white")
        fd.ellipse((size*.25, size*.12, size*.56, size*.43), fill=(230, 34, 65))
        for sx, sy in [(0.59,0.17),(0.69,0.25),(0.66,0.36),(0.54,0.37),(0.51,0.26)]:
            fd.ellipse((size*sx, size*sy, size*(sx+.055), size*(sy+.055)), fill="white")
    elif code == "jp":
        fd.rectangle((0, 0, size, size), fill=(250, 250, 250))
        fd.ellipse((size*.30, size*.30, size*.70, size*.70), fill=(205, 28, 58))
    elif code == "us":
        stripe_h = max(1, size//13)
        for i in range(13):
            fd.rectangle((0, i*stripe_h, size, min(size, (i+1)*stripe_h)), fill=(190, 35, 55) if i%2 == 0 else "white")
        fd.rectangle((0, 0, size*.48, size*.54), fill=(28, 60, 130))
        for sy in range(4):
            for sx in range(5):
                px = size*(.045 + sx*.085)
                py = size*(.045 + sy*.12)
                fd.ellipse((px, py, px+2, py+2), fill="white")
    elif code == "hk":
        fd.rectangle((0, 0, size, size), fill=(222, 40, 53))
        # Hoa bauhinia tượng trưng
        import math
        for a in range(5):
            ang = a*2*math.pi/5 - math.pi/2
            px, py = size*.5 + math.cos(ang)*size*.18, size*.5 + math.sin(ang)*size*.18
            fd.ellipse((px-size*.10, py-size*.07, px+size*.10, py+size*.07), fill="white")
        fd.ellipse((size*.46, size*.46, size*.54, size*.54), fill=(222,40,53))
    elif code == "my":
        fd.rectangle((0, 0, size, size), fill=(210, 38, 58))
        for i in range(1, 7):
            fd.rectangle((0, i*size//8, size, i*size//8+max(1,size//16)), fill="white")
        fd.rectangle((0, 0, size*.52, size*.53), fill=(20, 65, 145))
        fd.ellipse((size*.12,size*.12,size*.38,size*.40), fill=(250,205,45))
        fd.ellipse((size*.19,size*.10,size*.42,size*.37), fill=(20,65,145))
        fd.ellipse((size*.32,size*.17,size*.37,size*.22), fill=(250,205,45))
    elif code == "il":
        fd.rectangle((0, 0, size, size), fill="white")
        fd.rectangle((0, size*.22, size, size*.34), fill=(36, 89, 190))
        fd.rectangle((0, size*.66, size, size*.78), fill=(36, 89, 190))
        # Ngôi sao David giản lược
        import math
        pts1=[]; pts2=[]
        for i in range(6):
            a=-math.pi/2+i*math.pi/3
            pts1.append((size*.5+math.cos(a)*size*.19,size*.5+math.sin(a)*size*.19))
            a=-math.pi/2+math.pi/3+i*math.pi/3
            pts2.append((size*.5+math.cos(a)*size*.19,size*.5+math.sin(a)*size*.19))
        fd.polygon(pts1, outline=(36,89,190))
        fd.polygon(pts2, outline=(36,89,190))
    elif code == "in":
        fd.rectangle((0, 0, size, size//3), fill=(245, 145, 32))
        fd.rectangle((0, size//3, size, size*2//3), fill="white")
        fd.rectangle((0, size*2//3, size, size), fill=(28, 150, 82))
        fd.ellipse((size*.42,size*.42,size*.58,size*.58), outline=(30,75,160), width=max(1,size//18))
        fd.ellipse((size*.48,size*.48,size*.52,size*.52), fill=(30,75,160))
    else:
        # Cloudflare: nền xanh và đám mây đơn giản
        flag.paste((26, 132, 238), (0, 0, size, size))
        fd = ImageDraw.Draw(flag)
        fd.ellipse((size*.18,size*.42,size*.58,size*.78), fill="white")
        fd.ellipse((size*.36,size*.25,size*.72,size*.75), fill="white")
        fd.ellipse((size*.58,size*.43,size*.84,size*.73), fill="white")
        fd.rectangle((size*.28,size*.54,size*.76,size*.76), fill="white")
    # Mask tròn
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, size-1, size-1), fill=255)
    image.paste(flag, (cx-radius, cy-radius), mask)
    draw.ellipse((x0, y0, x1, y1), outline=(26, 83, 137), width=2)

# --- Infographic dựng từ asset cắt thẳng từ ảnh mẫu (thư mục rd_assets, tạo bởi extract_assets.py) ---
_IG_CACHE = {}


def _ig_asset(name, mode):
    key = (name, mode)
    if key not in _IG_CACHE:
        path = os.path.join(ASSET_DIR, name)
        if not os.path.isfile(path):
            raise RuntimeError("Thiếu asset '%s'. Hãy để thư mục rd_assets cạnh file exe/rd_speedtest.py "
                               "(hoặc build với --add-data)." % path)
        img = Image.open(path)
        img.load()
        _IG_CACHE[key] = img.convert(mode)
    return _IG_CACHE[key]


def _ig_layout():
    if "layout" not in _IG_CACHE:
        path = os.path.join(ASSET_DIR, "layout.json")
        if not os.path.isfile(path):
            raise RuntimeError("Thiếu '%s'. Hãy để thư mục rd_assets cạnh file exe/rd_speedtest.py "
                               "(hoặc build với --add-data)." % path)
        with open(path, encoding="utf-8") as f:
            _IG_CACHE["layout"] = json.load(f)
    return _IG_CACHE["layout"]


def _paste_rgba(dst, src, x, y):
    """Dán ảnh RGBA (alpha thật) lên ảnh RGB."""
    dst.paste(src.convert("RGB"), (int(x), int(y)), src.getchannel("A"))


def _ig_number(strip, text, ty, lay, int_digits=None):
    """Ghép số tốc độ từ glyph đã trích từ ảnh mẫu: chữ số cách nhau 17px, dấu chấm theo mẫu.

    Canh lề phải theo dấu thập phân: mọi dòng có dấu chấm ở cùng một cột. Cột đó được tính sao cho
    số có nhiều chữ số nguyên nhất (`int_digits`) bắt đầu đúng ở x0; số ngắn hơn dịch sang phải.
    """
    gl = lay["glyphs"]
    x0 = 517
    pos = []                       # (ký tự, mép trái glyph)
    int_part, _, frac = text.partition(".")
    if int_digits is None:
        int_digits = len(int_part)
    int_digits = max(int_digits, len(int_part))
    dot_left = x0 + 17 * (int_digits - 1) + 18      # cột dấu chấm cố định cho mọi dòng
    first_left = dot_left - 18 - 17 * (len(int_part) - 1)
    for k, ch in enumerate(int_part):
        pos.append((ch, first_left + 17 * k))
    pos.append((".", dot_left))
    left = dot_left + 8
    for k, ch in enumerate(frac):
        pos.append((ch, left))
        last_left = left
        left += 17
    for ch, lft in pos:
        g = gl[ch]
        img = _ig_asset(g["file"], "RGBA")
        _paste_rgba(strip, img, lft - g["left"], ty + g["dy"])
    last_ch, last_l = pos[-1]
    end_x = last_l + gl[last_ch]["w"]
    mb = lay["mbps"]
    img = _ig_asset(mb["file"], "RGBA")
    _paste_rgba(strip, img, end_x + mb["gap"] - mb["left"], ty + mb["dy"])


def _avg_colour(img, x, y0, y1):
    """Màu trung bình của một cột ảnh trong dải [y0, y1)."""
    return img.crop((x, y0, x + 1, y1)).resize((1, 1), Image.BOX).getpixel((0, 0))


def _recolour(piece, src_col, dst_col):
    """Đổi màu phần tô (chỉ các điểm thuộc phần tô, không đụng nền/rãnh) theo tỉ lệ từng kênh."""
    chans = piece.split()
    mask = chans[1].point(lambda v: 255 if v > 100 else 0)
    out = []
    for c, s, d in zip(chans, src_col, dst_col):
        f = d / float(max(1, s))
        out.append(c.point(lambda v, f=f: min(255, int(round(v * f)))))
    return Image.composite(Image.merge("RGB", out), piece, mask)


def _ig_bar(strip, geo, frac, lay):
    """Rãnh + phần tô (trích từ ảnh mẫu); phần giữa của thanh tô được co giãn, hai đầu bo giữ nguyên.

    Màu gradient của thanh ngắn kết thúc sớm hơn (xanh lam -> cyan), thanh dài kết thúc ở xanh mint, giống mẫu.
    """
    bar = lay["bar"]
    m, cap = bar["margin"], bar["cap"]
    bh = geo["by1"] - geo["by0"] + 1 + 2 * m
    x = bar["x"]
    y = geo["by0"] - m
    track = _ig_asset("track.png", "RGB")
    fill = _ig_asset("fill.png", "RGB")
    if track.height != bh:
        track = track.resize((track.width, bh), Image.LANCZOS)
    if fill.height != bh:
        fill = fill.resize((fill.width, bh), Image.LANCZOS)
    strip.paste(track, (x, y))
    fw = fill.width
    frac = max(0.0, min(1.0, frac))
    wf = max(21, min(fw, int(round(fw * frac ** 0.85))))
    # vị trí cuối của gradient trong ảnh gốc, tuỳ độ dài thanh
    s_end = int(round(110 + 135 * min(1.0, (wf - 21) / 80.0) ** 0.7))
    s_end = max(cap * 2 + 2, min(fw - 2, s_end))
    mid_y0, mid_y1 = bh // 2 - 2, bh // 2 + 2
    right_src = fill.crop((fw - cap, 0, fw, bh))
    if s_end < fw - 8:
        right_src = _recolour(right_src, _avg_colour(fill, fw - cap // 2 - 1, mid_y0, mid_y1),
                              _avg_colour(fill, s_end, mid_y0, mid_y1))
    if wf > 2 * cap:
        mid = fill.crop((cap, 0, max(cap + 1, s_end - cap), bh))
        if wf - 2 * cap != mid.width:
            mid = mid.resize((wf - 2 * cap, bh), Image.LANCZOS)
        piece = Image.new("RGB", (wf, bh))
        piece.paste(fill.crop((0, 0, cap, bh)), (0, 0))
        piece.paste(mid, (cap, 0))
        piece.paste(right_src, (wf - cap, 0))
    else:
        # thanh rất ngắn: nửa trái bo tròn + nửa phải là bản lật của chính nó (liền màu, không có đường nối)
        left = fill.crop((0, 0, cap, bh))
        piece = Image.new("RGB", (wf, bh))
        piece.paste(left, (0, 0))
        piece.paste(ImageOps.mirror(left), (wf - cap, 0))
        if wf > 2 * cap:
            piece.paste(left.crop((cap - 1, 0, cap, bh)).resize((wf - 2 * cap, bh)), (cap, 0))
    if wf < 2 * cap + 6:
        piece = _recolour(piece, (21, 182, 250), (33, 200, 238))
    strip.paste(piece, (x, y))


def _ig_foot_style(lay):
    """Thông số kiểu chữ của dòng chân trang (đo từ chữ gốc): chiều cao chữ hoa, màu, vị trí đáy."""
    ft = lay["footer"]
    text = _ig_asset("foot_text.png", "RGBA")
    tx0 = ft["tx0"]
    c2 = text.crop((256 - tx0, 0, 268 - tx0, text.height))
    bb = c2.getchannel("A").point(lambda a: 255 if a > 90 else 0).getbbox()
    colour = c2.getpixel(((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2))[:3]
    gh = _ig_asset(lay["glyphs"]["8"]["file"], "RGBA").getchannel("A").point(
        lambda a: 255 if a > 90 else 0).getbbox()
    hf = bb[3] - bb[1]
    return dict(text=text, tx0=tx0, top=bb[1], bottom=bb[3], hf=hf, colour=colour,
                scale=hf / float(gh[3] - gh[1]), base_y=ft["ty0"] - (lay["cuts"][-1] + lay["cap_h"]))


def _ig_scaled(ch, st, lay, height=None):
    """Ký tự số hoặc dấu chấm: glyph tốc độ (trích từ ảnh mẫu) thu nhỏ về cỡ chữ chân trang, đổi sang màu chữ chân trang."""
    g = _ig_asset(lay["glyphs"][ch]["file"], "RGBA")
    gb = g.getchannel("A").point(lambda v: 255 if v > 90 else 0).getbbox()
    g = g.crop(gb)
    if ch == ".":
        w = max(2, int(round(g.width * st["scale"])))
        h = max(2, int(round(g.height * st["scale"])))
    else:
        h = st["hf"]
        w = max(1, int(round(g.width * h / float(g.height))))
    g = g.resize((w, h), Image.LANCZOS)
    al = g.getchannel("A").point(lambda v: int(255 * (v / 255.0) ** 1.5))
    solid = Image.new("RGBA", g.size, st["colour"] + (255,))
    solid.putalpha(al)
    return solid


def _ig_foot_piece(ch, st, lay):
    """Một ký tự của dòng chân trang: ảnh RGBA cao bằng dải chữ, kèm bề rộng."""
    th = st["text"].height
    if ch.isdigit() or ch == ".":
        g = _ig_scaled(ch, st, lay)
        out = Image.new("RGBA", (g.width, th), (0, 0, 0, 0))
        y = st["top"] if ch.isdigit() else st["bottom"] - g.height
        out.paste(g, (0, y))
        return out, 0, 0
    if ch == ":":
        dot = _ig_scaled(".", st, lay)
        out = Image.new("RGBA", (dot.width, th), (0, 0, 0, 0))
        out.paste(dot, (0, st["bottom"] - dot.height))
        out.paste(dot, (0, st["bottom"] - dot.height - int(round(st["hf"] * 0.62))))
        return out, 0, 0
    ft = lay["footer"]
    i = ft["chars"].index(ch)            # chữ cái cắt thẳng từ dòng chữ gốc
    a, b = ft["segs"][i]
    return st["text"].crop((a - 1 - st["tx0"], 0, b + 2 - st["tx0"], th)), 1, 2


def _ig_foot_line(items, st, lay, spacing=7, space=15):
    """Ghép chuỗi ký tự thành một ảnh RGBA; ' ' là khoảng cách giữa các từ."""
    pieces = [None if ch == " " else _ig_foot_piece(ch, st, lay) for ch in items]

    def gap(k):
        p, q = pieces[k], pieces[k + 1]
        if p is None or q is None:
            return 0
        return spacing - p[2] - q[1]

    total = 0
    for k, p in enumerate(pieces):
        total += space if p is None else p[0].width
        if k + 1 < len(pieces):
            total += gap(k)
    line = Image.new("RGBA", (total, st["text"].height), (0, 0, 0, 0))
    x = 0
    for k, p in enumerate(pieces):
        if p is None:
            x += space
            continue
        line.alpha_composite(p[0], (x, 0))
        x += p[0].width
        if k + 1 < len(pieces):
            x += gap(k)
    return line


def _ig_footer(n_rows, lay, when):
    """Chân trang: giữ nguyên ảnh mẫu khi 12 server (khác 12 thì thay số bằng glyph trích từ ảnh mẫu),
    rồi thêm dòng "TESTED <ngày> <giờ>" bằng chữ cái và chữ số trích từ ảnh mẫu."""
    footer = _ig_asset("footer.png", "RGB").copy()
    ft = lay["footer"]
    st = _ig_foot_style(lay)
    base_y = st["base_y"]
    if n_rows != 12:
        footer.paste(_ig_asset("foot_clean.png", "RGB"), (ft["tx0"], base_y))
        text, tx0 = st["text"], st["tx0"]
        c1 = text.crop((244 - tx0, 0, 252 - tx0, text.height))
        c2 = text.crop((256 - tx0, 0, 268 - tx0, text.height))
        rest = text.crop((ft["rest_x0"] - 2 - tx0, 0, text.width, text.height))
        if n_rows < 3:                       # chỉ có chữ số 1, 2 trong ảnh mẫu: dùng bản gốc
            digits = [(c1, 4, 2) if ch == "1" else (c2, 8, 2) for ch in str(n_rows)]
        else:
            digits = []
            for ch in str(n_rows):
                p = _ig_foot_piece(ch, st, lay)[0]
                digits.append((p, p.width, 0))
        spacing = 7
        num_w = sum(w for _, w, _ in digits) + spacing * (len(digits) - 1)
        rest_w = rest.width - 2
        center = (ft["num_x0"] + ft["tx1"]) / 2.0
        start = int(round(center - (num_w + ft["gap"] + rest_w) / 2.0))
        x = start
        for img, w, off in digits:
            _paste_rgba(footer, img, x - off, base_y)
            x += w + spacing
        _paste_rgba(footer, rest, start + num_w + ft["gap"] - 2, base_y)
    # dòng thời gian test, căn giữa dưới dòng chữ chân trang
    stamp = "TESTED " + when.strftime("%d.%m.%Y %H:%M")
    line = _ig_foot_line(list(stamp), st, lay)
    cx = (ft["num_x0"] + ft["tx1"]) / 2.0
    footer.paste(line.convert("RGB"), (int(round(cx - line.width / 2.0)), base_y + 34), line.getchannel("A"))
    return footer


def create_infographic(values, output_path, when=None):
    """Xuất ảnh kết quả theo đúng template mẫu (ghép từ asset trích từ ảnh gốc).

    `when`: thời điểm test (datetime); mặc định lấy giờ hệ thống lúc xuất.
    """
    if Image is None:
        raise RuntimeError("Chưa cài Pillow. Hãy chạy: py -m pip install Pillow")
    lay = _ig_layout()
    rows = [n for n in lay["names"] if n in values]
    if not rows:
        raise ValueError("Chưa có kết quả hợp lệ để xuất.")
    vmax = max(values[n] for n in rows) or 1.0
    int_digits = max(len("%.2f" % values[n]) - 3 for n in rows)   # số chữ số nguyên lớn nhất (bỏ ".xx")
    strips = []
    for n in rows:
        info = lay["rows"][n]
        strip = _ig_asset("row_%s.png" % n, "RGB").copy()
        _ig_bar(strip, info, values[n] / vmax, lay)
        _ig_number(strip, "%.2f" % values[n], info["ty"], lay, int_digits)
        strips.append(strip)
    top = _ig_asset("top.png", "RGB")
    cap = _ig_asset("cap.png", "RGB")
    when = when or get_hanoi_time()
    footer = _ig_footer(len(rows), lay, when)
    height = top.height + sum(s.height for s in strips) + cap.height + footer.height
    img = Image.new("RGB", (top.width, height))
    y = 0
    for part in [top] + strips + [cap, footer]:
        img.paste(part, (0, y))
        y += part.height
    img.save(output_path, "PNG", optimize=True)
    return len(rows), when.strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
FONT = "Segoe UI"
PAGE = "#06142a"      # nền cửa sổ
CARD = "#0a2040"      # nền thẻ
CARD2 = "#0f2b52"     # nền ô nhập / nút phụ
TRACK = "#12305a"     # rãnh thanh tốc độ
LINE = "#17406e"      # viền thẻ
SEP = "#0f2d54"       # đường kẻ hàng
TEXT = "#f2f7ff"
MUTED = "#8fb0d6"
DIM = "#5d7ca3"
ACCENT = "#279df5"
MINT = "#53e2ae"
BAD = "#ff6b7a"
WARN = "#f5b83d"

_SC = [1.0]   # hệ số DPI


def px(n):
    return int(round(n * _SC[0]))


def mix(c1, c2, t):
    """Trộn hai màu hex: t=0 -> c1, t=1 -> c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


def rrect(cv, x1, y1, x2, y2, r, **kw):
    r = max(1, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class Card(_CanvasBase):
    """Thẻ bo góc; đặt widget con vào self.inner."""

    def __init__(self, parent, pad=12, radius=14, fill=CARD, border=LINE, parent_bg=PAGE):
        super().__init__(parent, width=1, height=1, bg=parent_bg, highlightthickness=0, bd=0)
        self.pad, self.radius, self.fill, self.border = px(pad), px(radius), fill, border
        self.inner = tk.Frame(self, bg=fill)
        self._win = self.create_window(self.pad, self.pad, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_cfg)
        self.inner.bind("<Configure>", self._on_inner)

    def _on_cfg(self, e):
        self.delete("bg")
        rrect(self, 1, 1, e.width - 1, e.height - 1, self.radius, fill=self.fill, outline=self.border, tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(1, e.width - 2 * self.pad))

    def _on_inner(self, e):
        self.after_idle(self.fit)

    def fit(self):
        """Co thẻ vừa khít nội dung (chiều cao theo widget con)."""
        h = self.inner.winfo_reqheight()
        if int(float(self.cget("height"))) != h + 2 * self.pad:
            self.config(height=h + 2 * self.pad)
        if int(float(self.itemcget(self._win, "height") or 0)) != h:
            self.itemconfigure(self._win, height=h)


class RoundButton(_CanvasBase):
    STYLES = {
        "primary": dict(bg=ACCENT, hover="#52b3ff", press="#1c86d6", fg="#031a33", border=None),
        "secondary": dict(bg=CARD2, hover="#163a6c", press="#0c2447", fg=TEXT, border="#21568c"),
        "danger": dict(bg="#3a1626", hover="#52203a", press="#2c101c", fg="#ff8a97", border="#6b2a3d"),
        "ghost": dict(bg=CARD, hover=CARD2, press=TRACK, fg=MUTED, border=None),
    }

    def __init__(self, parent, text, command, style="secondary", width=120, height=34,
                 parent_bg=CARD, font_size=10):
        super().__init__(parent, width=px(width), height=px(height), bg=parent_bg,
                         highlightthickness=0, bd=0)
        self.text, self.command, self.style = text, command, style
        self.font = (FONT, font_size, "bold")
        self.enabled, self._state = True, "normal"
        self.bind("<Enter>", lambda e: self._set("hover"))
        self.bind("<Leave>", lambda e: self._set("normal"))
        self.bind("<ButtonPress-1>", lambda e: self._set("press"))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda e: self._draw())

    def _set(self, st):
        if self.enabled:
            self._state = st
            self._draw()

    def _release(self, e):
        if not self.enabled:
            return
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self._set("hover" if inside else "normal")
        if inside and self.command:
            self.command()

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._state = "normal"
        self.config(cursor="hand2" if on else "arrow")
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        s = self.STYLES[self.style]
        if not self.enabled:
            fill, fg, border = mix(PAGE, CARD2, 0.6), "#47658f", None
        else:
            fill = {"normal": s["bg"], "hover": s["hover"], "press": s["press"]}[self._state]
            fg, border = s["fg"], s["border"]
        rrect(self, 1, 1, w - 1, h - 1, px(9), fill=fill, outline=border or fill)
        self.create_text(w // 2, h // 2, text=self.text, fill=fg, font=self.font)


class StatCard(_CanvasBase):
    """Ô số liệu: tiêu đề, giá trị lớn + đơn vị, ghi chú."""

    def __init__(self, parent, title, accent):
        super().__init__(parent, width=1, height=px(64), bg=PAGE, highlightthickness=0, bd=0)
        self.title, self.accent = title, accent
        self.value, self.unit, self.sub, self.color = "-", "", "", TEXT
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, value, unit="", sub="", color=TEXT):
        self.value, self.unit, self.sub, self.color = value, unit, sub, color
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20:
            return
        rrect(self, 1, 1, w - 1, h - 1, px(14), fill=CARD, outline=LINE)
        rrect(self, px(12), px(14), px(15), h - px(14), px(2), fill=self.accent, outline=self.accent)
        x = px(24)
        self.create_text(x, px(15), text=self.title, anchor="w", fill=MUTED, font=(FONT, 9))
        vid = self.create_text(x, px(34), text=self.value, anchor="w", fill=self.color, font=(FONT, 15, "bold"))
        if self.unit:
            b = self.bbox(vid)
            self.create_text(b[2] + px(4), b[3] - px(4), text=self.unit, anchor="sw", fill=DIM, font=(FONT, 9))
        self.create_text(x, h - px(11), text=self.sub, anchor="w", fill=DIM, font=(FONT, 9))


class Pill(_CanvasBase):
    """Nhãn trạng thái có chấm màu."""

    def __init__(self, parent):
        super().__init__(parent, width=px(170), height=px(28), bg=PAGE, highlightthickness=0, bd=0)
        self.text, self.color = "Sẵn sàng", MINT
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, text, color):
        self.text, self.color = text, color
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20:
            return
        rrect(self, 1, 1, w - 1, h - 1, h // 2, fill=mix(self.color, PAGE, 0.86), outline=mix(self.color, PAGE, 0.55))
        r = px(4)
        self.create_oval(px(16) - r, h // 2 - r, px(16) + r, h // 2 + r, fill=self.color, outline=self.color)
        self.create_text(px(30), h // 2, text=self.text, anchor="w", fill=TEXT, font=(FONT, 10))


class App:
    def __init__(self, root):
        self.root = root
        _SC[0] = max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.title(APP_TITLE)
        root.configure(bg=PAGE)
        w, h = px(900), px(700)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(w, sw - px(40)), min(h, sh - px(80))
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 3)))
        root.minsize(min(px(860), w), min(px(600), h))

        self.q = queue.Queue()
        self.stop = threading.Event()
        self.running = False
        self.results = {}      # name -> Mbps
        self.rows = {}         # name -> dict widget
        self.test_conns = DEFAULT_CONNECTIONS
        self.test_secs = DEFAULT_DURATION
        self.run_total = self.run_done = 0
        self.run_frac = 0.0
        self.test_time = None  # thời điểm test xong gần nhất (giờ hệ thống)
        self._flags = {}
        self.cards = []

        self._style()
        self._make_flags()
        self._build()
        root.update_idletasks()
        for c in self.cards:
            c.fit()
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(max(px(900), root.winfo_reqwidth()), sw - px(40))
        h = min(root.winfo_reqheight(), sh - px(80))
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 3)))
        root.minsize(min(px(860), w), h)
        self._set_controls(True)
        self._update_stats()
        root.bind("<F5>", lambda e: self.start_all())
        root.bind("<Control-e>", lambda e: self.export_result())
        root.bind("<Escape>", lambda e: self.on_stop())
        root.after(80, self._poll)

    # ---- setup ----
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass

    def _make_flags(self):
        """Cờ tròn dựng bằng Pillow (cùng hàm vẽ với infographic); thiếu Pillow thì dùng huy hiệu mã nước."""
        try:
            from PIL import ImageTk
        except Exception:
            return
        if Image is None:
            return
        r = px(11)
        size = 2 * r + 4
        for name, _, _ in SERVERS:
            try:
                img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                draw_flag(img, ImageDraw.Draw(img), size // 2, size // 2, r, FLAG_CODE[name])
                self._flags[name] = ImageTk.PhotoImage(img)
            except Exception:
                self._flags.clear()
                return

    def _cols(self, f):
        for i, (minw, wt) in enumerate(((44, 0), (114, 0), (140, 1), (112, 0), (120, 2), (116, 0))):
            f.grid_columnconfigure(i, minsize=px(minw), weight=wt)

    def _build(self):
        # --- chân trang ---
        foot = tk.Frame(self.root, bg=PAGE)
        foot.pack(side="bottom", fill="x", padx=px(20), pady=(0, px(8)))
        self.lbl_summary = tk.Label(foot, text="Sẵn sàng. Nhấn F5 để test tất cả.", anchor="w",
                                    font=(FONT, 9), fg=MUTED, bg=PAGE)
        self.lbl_summary.pack(side="left")
        tk.Label(foot, text="F5 test   Esc dừng   Ctrl+E xuất", font=(FONT, 9),
                 fg=DIM, bg=PAGE).pack(side="right")

        outer = tk.Frame(self.root, bg=PAGE)
        outer.pack(fill="both", expand=True, padx=px(18), pady=(px(12), px(6)))

        # --- tiêu đề ---
        head = tk.Frame(outer, bg=PAGE)
        head.pack(fill="x")
        logo = tk.Canvas(head, width=px(38), height=px(38), bg=PAGE, highlightthickness=0)
        logo.create_arc(px(4), px(4), px(34), px(34), start=25, extent=185, style="arc", outline=ACCENT, width=px(5))
        logo.create_arc(px(4), px(4), px(34), px(34), start=205, extent=185, style="arc", outline=MINT, width=px(5))
        logo.pack(side="left")
        titles = tk.Frame(head, bg=PAGE)
        titles.pack(side="left", padx=(px(12), 0))
        tk.Label(titles, text="Real-Debrid Speedtest", font=(FONT, 15, "bold"), fg=TEXT, bg=PAGE).pack(anchor="w")
        tk.Label(titles, text="Đo tốc độ đa luồng tới từng máy chủ CDN", font=(FONT, 9), fg=MUTED,
                 bg=PAGE).pack(anchor="w")
        self.pill = Pill(head)
        self.pill.pack(side="right")

        # --- số liệu ---
        stats = tk.Frame(outer, bg=PAGE)
        stats.pack(fill="x", pady=(px(10), 0))
        for i in range(4):
            stats.grid_columnconfigure(i, weight=1, uniform="stat")
        self.card_best = StatCard(stats, "Nhanh nhất", MINT)
        self.card_avg = StatCard(stats, "Trung bình", ACCENT)
        self.card_done = StatCard(stats, "Đã test", "#7b8cff")
        self.card_live = StatCard(stats, "Đang test", WARN)
        for i, c in enumerate((self.card_best, self.card_avg, self.card_done, self.card_live)):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(4), 0 if i == 3 else px(4)))

        # --- thanh điều khiển ---
        tb = Card(outer, pad=8)
        self.cards.append(tb)
        tb.pack(fill="x", pady=(px(10), 0))
        left = tk.Frame(tb.inner, bg=CARD)
        left.pack(side="left")
        self.btn_all = RoundButton(left, "Test", self.start_all, "primary", width=108, height=30)
        self.btn_stop = RoundButton(left, "Dừng", self.on_stop, "danger", width=66, height=30)
        self.btn_save = RoundButton(left, "Xuất kết quả", self.export_result, "secondary", width=108, height=30)
        for i, b in enumerate((self.btn_all, self.btn_stop, self.btn_save)):
            b.pack(side="left", padx=(0 if i == 0 else px(8), 0))

        # --- tiến độ tổng ---
        self.prog = tk.Canvas(outer, width=1, height=px(4), bg=PAGE, highlightthickness=0)
        self.prog.pack(fill="x", pady=(px(8), 0))
        self.prog.bind("<Configure>", lambda e: self._draw_progress())

        # --- bảng server ---
        tbl = Card(outer, pad=8)
        self.cards.append(tbl)
        tbl.pack(fill="both", expand=True, pady=(px(8), 0))
        inner = tbl.inner
        hdr = tk.Frame(inner, bg=CARD)
        hdr.pack(fill="x", pady=(0, px(3)))
        self._cols(hdr)
        for i, t in enumerate(("", "Server", "Quốc gia", "Tốc độ", "Biểu đồ", "Trạng thái")):
            tk.Label(hdr, text=t, anchor="w", fg=DIM, bg=CARD, font=(FONT, 9)).grid(
                row=0, column=i, sticky="ew", padx=(px(8) if i == 4 else 0, 0))

        for idx, (name, country, url) in enumerate(SERVERS):
            tk.Frame(inner, bg=SEP, height=1).pack(fill="x")
            row = tk.Frame(inner, bg=CARD)
            row.pack(fill="x")
            self._cols(row)
            pady = px(2)
            if name in self._flags:
                flag = tk.Label(row, image=self._flags[name], bg=CARD, bd=0)
            else:
                flag = tk.Canvas(row, width=px(26), height=px(26), bg=CARD, highlightthickness=0)
                flag.create_oval(2, 2, px(26) - 2, px(26) - 2, fill=CARD2, outline=LINE)
                flag.create_text(px(13), px(13), text=FLAG_CODE[name].upper(), fill=MUTED, font=(FONT, 8, "bold"))
            flag.grid(row=0, column=0, sticky="w", pady=pady)
            lbl_name = tk.Label(row, text=name, anchor="w", font=(FONT, 11, "bold"), fg=TEXT, bg=CARD)
            lbl_name.grid(row=0, column=1, sticky="ew")
            tk.Label(row, text=country, anchor="w", font=(FONT, 10), fg=MUTED, bg=CARD).grid(
                row=0, column=2, sticky="ew")
            sp = tk.Frame(row, bg=CARD)
            sp.grid(row=0, column=3, sticky="w")
            lbl_speed = tk.Label(sp, text="-", width=8, anchor="e", font=(FONT, 11, "bold"), fg=DIM, bg=CARD)
            lbl_speed.pack(side="left")
            tk.Label(sp, text="Mbps", font=(FONT, 9), fg=DIM, bg=CARD).pack(side="left", padx=(px(4), 0))
            cv = tk.Canvas(row, width=1, height=px(14), bg=CARD, highlightthickness=0)
            cv.grid(row=0, column=4, sticky="ew", padx=(px(6), px(8)))
            cv.bind("<Configure>", lambda e, n=name: self._draw_bar(n))
            lbl_status = tk.Label(row, text="", anchor="w", font=(FONT, 9), fg=MUTED, bg=CARD)
            lbl_status.grid(row=0, column=5, sticky="ew")
            self.rows[name] = dict(name=lbl_name, speed=lbl_speed, canvas=cv, status=lbl_status, bar=0.0)

    # ---- actions ----
    def start_all(self):
        self.start([s[0] for s in SERVERS])

    def start(self, names):
        if self.running:
            return
        conns, secs = DEFAULT_CONNECTIONS, DEFAULT_DURATION
        self.test_conns, self.test_secs = conns, secs
        self.stop.clear()
        self.running = True
        self.run_total, self.run_done, self.run_frac = len(names), 0, 0.0
        self._draw_progress()
        self._set_controls(False)
        for n in names:
            self.rows[n]["status"].config(text="Đang chờ...", fg=MUTED)
        self.card_live.set("-", "Mbps", "", TEXT)
        self._update_stats()
        threading.Thread(target=self._run, args=(names, conns, secs), daemon=True).start()

    def on_stop(self):
        if self.running and not self.stop.is_set():
            self.stop.set()
            self.btn_stop.set_enabled(False)

    def _run(self, names, conns, secs):
        urls = {n: u for n, _, u in SERVERS}
        for name in names:
            if self.stop.is_set():
                break
            self.q.put(("start", name))
            try:
                res = measure(urls[name], conns, secs, self.stop,
                              lambda s, p, n=name: self.q.put(("prog", n, s, p)))
                if res is None:
                    self.q.put(("cancel", name))
                    break
                self.q.put(("done", name, res))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("error", name, str(exc)))
        self.q.put(("end",))

    def _set_controls(self, idle):
        has = bool(self.results)
        self.btn_all.set_enabled(idle)
        self.btn_save.set_enabled(idle and has)
        self.btn_stop.set_enabled(not idle)

    # ---- UI updates from worker thread ----
    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "start":
            name = msg[1]
            r = self.rows[name]
            r["status"].config(text="Đang test...", fg=ACCENT)
            r["speed"].config(text="...", fg=TEXT)
            self.run_frac = 0.0
            self._set_bar(name, 0.0)
            self.pill.set("Đang đo %s" % name, ACCENT)
            self.card_live.set("-", "Mbps", "", TEXT)
            self._draw_progress()
        elif kind == "prog":
            _, name, mbps, frac = msg
            self.rows[name]["speed"].config(text="%.2f" % mbps, fg=TEXT)
            self.rows[name]["status"].config(text="Đang test %d%%" % int(frac * 100), fg=ACCENT)
            self._set_bar(name, mbps)
            self.run_frac = frac
            self.card_live.set("%.2f" % mbps, "Mbps", "", TEXT)
            self._draw_progress()
        elif kind == "done":
            _, name, mbps = msg
            self.results[name] = mbps
            self.rows[name]["speed"].config(text="%.2f" % mbps, fg=MINT)
            self.rows[name]["status"].config(text="Hoàn tất", fg=MINT)
            self._set_bar(name, mbps)
            self._redraw_all_bars()
            self.run_done += 1
            self.run_frac = 0.0
            self._draw_progress()
            self._update_stats()
        elif kind == "error":
            _, name, err = msg
            self.rows[name]["speed"].config(text="Lỗi", fg=BAD)
            self.rows[name]["status"].config(text=err[:26], fg=BAD)
            self._set_bar(name, 0.0)
            self.run_done += 1
            self.run_frac = 0.0
            self._draw_progress()
        elif kind == "cancel":
            r = self.rows[msg[1]]
            r["speed"].config(text="-", fg=DIM)
            r["status"].config(text="Đã dừng", fg=WARN)
            self._set_bar(msg[1], 0.0)
        elif kind == "end":
            stopped = self.stop.is_set()
            self.running = False
            for r in self.rows.values():
                if r["status"].cget("text") in ("Đang chờ...", "Đang test..."):
                    r["status"].config(text="", fg=MUTED)
            if stopped:
                self.pill.set("Đã dừng", WARN)
                self.run_total = self.run_done = 0
                self.run_frac = 0.0
            else:
                self.pill.set("Hoàn tất", MINT)
            if not stopped or self.results:
                self.test_time = get_hanoi_time()
            self.card_live.set("-", "", "", TEXT)
            self._draw_progress()
            self._set_controls(True)
            self._update_stats()

    # ---- bars ----
    def _scale(self):
        return max(1000.0, max(self.results.values(), default=0.0) * 1.05)

    def _draw_bar(self, name):
        r = self.rows[name]
        cv = r["canvas"]
        w, h = cv.winfo_width(), cv.winfo_height()
        cv.delete("all")
        if w < 20:
            return
        t = px(7)
        x1, x2, y = t // 2 + 1, w - t // 2 - 1, h // 2
        cv.create_line(x1, y, x2, y, width=t, capstyle="round", fill=TRACK)
        frac = min(r["bar"] / self._scale(), 1.0)
        if frac > 0:
            xe = x1 + max(0.5, (x2 - x1) * frac)
            cv.create_line(x1, y, xe, y, width=t, capstyle="round", fill=mix(ACCENT, MINT, frac))

    def _set_bar(self, name, mbps):
        self.rows[name]["bar"] = mbps
        self._draw_bar(name)

    def _redraw_all_bars(self):
        for name in self.rows:
            self._draw_bar(name)

    def _draw_progress(self):
        cv = self.prog
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 20:
            return
        t = h
        x1, x2, y = t // 2, w - t // 2, h // 2
        cv.create_line(x1, y, x2, y, width=t, capstyle="round", fill=CARD)
        if self.run_total:
            frac = min((self.run_done + self.run_frac) / self.run_total, 1.0)
            if frac > 0:
                cv.create_line(x1, y, x1 + max(0.5, (x2 - x1) * frac), y, width=t, capstyle="round",
                               fill=mix(ACCENT, MINT, frac))

    # ---- thống kê ----
    def _update_stats(self):
        n = len(self.results)
        self.card_done.set("%d / %d" % (n, len(SERVERS)), "server", "", TEXT)
        info = {s[0]: s[1] for s in SERVERS}
        best = max(self.results, key=self.results.get) if self.results else None
        for name, r in self.rows.items():
            star = (name == best)
            r["name"].config(text=name + ("  ★" if star else ""), fg=MINT if star else TEXT)
        if not self.results:
            self.card_best.set("-", "", "", TEXT)
            self.card_avg.set("-", "", "", TEXT)
            return
        avg = sum(self.results.values()) / n
        self.card_best.set("%.2f" % self.results[best], "Mbps", "", MINT)
        self.card_avg.set("%.2f" % avg, "Mbps", "", TEXT)
        if not self.running:
            self.lbl_summary.config(text="Đã test %d/%d server. Nhanh nhất là %s với %.2f Mbps." % (
                n, len(SERVERS), best, self.results[best]))

    # ---- export ----
    def export_result(self):
        if self.running:
            return
        if not self.results:
            messagebox.showinfo(APP_TITLE, "Chưa có kết quả để xuất. Hãy chạy test trước.")
            return
        if Image is None:
            messagebox.showerror("Thiếu thư viện Pillow", "Hãy cài Pillow bằng lệnh:\npy -m pip install Pillow")
            return
        path = filedialog.asksaveasfilename(
            title="Lưu infographic",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
            initialfile="realdebrid_speedtest.png")
        if not path:
            return
        try:
            count, time_str = create_infographic(dict(self.results), path, self.test_time)
            messagebox.showinfo("Xuất ảnh thành công",
                                "Đã xuất %d server.\nTest time: %s\n\nFile:\n%s" % (count, time_str, path))
            self.lbl_summary.config(text="Đã xuất: %s" % path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Lỗi xuất ảnh", str(e))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def run_cli(args):
    wanted = {n.upper() for n in args.only} if args.only else None
    stop = threading.Event()
    print("%-11s %-20s %12s" % ("Server", "Quốc gia", "Mbps"))
    for name, country, url in SERVERS:
        if wanted and name.upper() not in wanted:
            continue
        try:
            mbps = measure(url, args.connections, args.duration, stop)
            print("%-11s %-20s %12.2f" % (name, country, mbps))
        except Exception as exc:  # noqa: BLE001
            print("%-11s %-20s %12s  (%s)" % (name, country, "lỗi", exc))


def main():
    ap = argparse.ArgumentParser(description="Real-Debrid speedtest (multi-connection)")
    ap.add_argument("--cli", action="store_true", help="chạy dòng lệnh, không mở GUI")
    ap.add_argument("--only", nargs="*", help="chỉ test các server này (vd: TYO1 SGP1)")
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION,
                    help="số giây đo sau giai đoạn làm nóng 1.5s (mặc định 10, như trang gốc)")
    ap.add_argument("--connections", type=int, default=DEFAULT_CONNECTIONS)
    args = ap.parse_args()

    if args.cli:
        run_cli(args)
        return
    if tk is None:
        raise SystemExit("Máy chưa có Tkinter. Cài python3-tk hoặc dùng: python rd_speedtest.py --cli")
    if sys.platform == "win32":  # chữ và nét vẽ sắc nét trên màn hình DPI cao
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()