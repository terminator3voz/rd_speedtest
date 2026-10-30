#!/usr/bin/env python3
"""Trích xuất asset từ ảnh mẫu infographic -> thư mục rd_assets/.

Chạy:  python extract_assets.py [ảnh_mẫu.png] [thư_mục_đầu_ra]
Tất cả asset là pixel cắt thẳng từ ảnh mẫu (không vẽ lại bằng thuật toán).
Chỉ phần số liệu (chữ số tốc độ, thanh tốc độ, số server ở chân trang) được ghép lại khi xuất.
"""
import json
import os
import sys

import numpy as np
from PIL import Image

SRC = sys.argv[1] if len(sys.argv) > 1 else "a_crisp_dark_blue_futuristic_infographic_infograp.png"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "rd_assets")
os.makedirs(OUT, exist_ok=True)

NAMES = "AMS RBX HKG1 SGP1 SGPO1 SGPL1 KUL1 TYO1 LAX1 TLV1 MUM1 Cloudflare".split()
SEPS = [313, 401, 482, 567, 653, 739, 825, 913, 1001, 1090, 1181, 1270]   # vạch kẻ trên đầu mỗi dòng
TEXTS = ["84.19", "72.39", "807.98", "775.44", "960.45", "914.77",
         "819.01", "213.24", "79.36", "64.54", "250.96", "630.75"]
CUT_ABOVE = 5          # dòng bắt đầu cách vạch kẻ 5px để giữ nguyên cả vạch
TABLE_END = 1350       # hết dòng cuối; từ đây là phần đáy bảng
FOOT_START = 1380      # bắt đầu chân trang
CLEAN_X0, CLEAN_X1 = 500, 985     # vùng số liệu cần xoá trong mỗi dòng
PATCH = (440, 490)                 # mảnh nền sạch để lát lại
BAR_X = 699                        # x bắt đầu thanh
BAR_MARGIN = 3

T = Image.open(SRC).convert("RGB")
A = np.array(T).astype(np.float64)
H, W = A.shape[:2]


def save(arr, name):
    Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8)).save(os.path.join(OUT, name), optimize=True)


def rgba_from(region, bg):
    """Tách lớp chữ khỏi nền: alpha theo hình chiếu lên hướng (màu chữ - nền); RGB giữ màu thật."""
    p = region.astype(np.float64)
    b = bg.astype(np.float64)
    d = p - b
    lum = d.sum(-1)
    # màu chữ = trung bình các điểm sáng nhất
    idx = np.argsort(lum.ravel())[-max(8, lum.size // 30):]
    c = p.reshape(-1, 3)[idx].mean(0)
    dc = c - b.reshape(-1, 3).mean(0)
    a = np.clip((d * dc).sum(-1) / max(1e-6, (dc * dc).sum()), 0, 1)
    a[a < 0.03] = 0
    rgb = np.where(a[..., None] > 0.05, (p - (1 - a[..., None]) * b) / np.maximum(a[..., None], 0.05), c)
    out = np.dstack([np.clip(rgb, 0, 255), a * 255])
    return out


def save_rgba(arr, name):
    Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8), "RGBA").save(os.path.join(OUT, name), optimize=True)


cuts = [s - CUT_ABOVE for s in SEPS] + [TABLE_END]
layout = {"cuts": cuts, "rows": {}, "names": NAMES}

# --- phần trên (tiêu đề + đầu bảng), đáy bảng, chân trang ---
save(A[0:cuts[0]], "top.png")
save(A[TABLE_END:FOOT_START], "cap.png")
save(A[FOOT_START:], "footer.png")
layout.update(top_h=cuts[0], cap_h=FOOT_START - TABLE_END, foot_h=H - FOOT_START, width=W)

# --- từng dòng ---
geo = {}
for i, n in enumerate(NAMES):
    y0, y1 = cuts[i], cuts[i + 1]
    strip = A[y0:y1].copy()
    # vị trí chữ số tốc độ
    reg = A[SEPS[i] + 4:y1 - 3, 510:600]
    rows = np.where((reg[:, :, 1] > 110).any(1))[0]
    ty = rows.min() + SEPS[i] + 4
    # thanh: dùng cột x=960 (luôn là rãnh)
    col = A[SEPS[i] + 4:y1 - 3, 960, 1]
    br = np.where(col > 38)[0]
    by0, by1 = br.min() + SEPS[i] + 4, br.max() + SEPS[i] + 4
    geo[n] = dict(ty=int(ty - y0), by0=int(by0 - y0), by1=int(by1 - y0))
    patch = strip[:, PATCH[0]:PATCH[1]]
    pw = patch.shape[1]
    g = geo[n]
    # chỉ xoá đúng hai ô chứa số liệu (chữ số + "Mbps", thanh tốc độ); mọi pixel khác giữ nguyên từ ảnh mẫu
    boxes = [(g["ty"] - 6, g["ty"] + 32, 505, 696), (g["by0"] - 5, g["by1"] + 6, 693, 978)]
    for (ya, yb, xa, xb) in boxes:
        for x in range(xa, xb, pw):
            w = min(pw, xb - x)
            strip[ya:yb, x:x + w] = patch[ya:yb, :w]
    save(strip, "row_%s.png" % n)
    layout["rows"][n] = dict(geo[n], h=int(y1 - y0), bg=[float(v) for v in np.median(patch.reshape(-1, 3), 0)])

# --- chữ số / dấu chấm / "Mbps" ---
digit_src = {}   # ký tự -> (tên dòng, thứ tự phân đoạn)
segs_by_row = {}
for i, n in enumerate(NAMES):
    ty = geo[n]["ty"] + cuts[i]
    reg = A[ty - 2:ty + 28, 505:700]
    mask = reg[:, :, 1] > 110
    cols = np.where(mask.any(0))[0]
    segs = []
    s = p = cols[0]
    for c in cols[1:]:
        if c != p + 1:
            segs.append((s + 505, p + 505))
            s = c
        p = c
    segs.append((s + 505, p + 505))
    segs = [sg for sg in segs if sg[0] < 600 + 20 and sg[1] < 697]
    segs_by_row[n] = segs
glyphs = {}
best = {}      # ký tự -> (độ thoáng, dòng, vị trí)
for i, (n, text) in enumerate(zip(NAMES, TEXTS)):
    segs = segs_by_row[n][:len(text)]
    for k, ch in enumerate(text):
        a0, a1 = segs[k]
        gl = a0 - segs[k - 1][1] if k else 12
        gr = segs[k + 1][0] - a1 if k + 1 < len(text) else 12
        clr = min(gl, gr)
        if ch not in best or clr > best[ch][0]:
            best[ch] = (clr, i, k)
for ch, (clr, i, k) in best.items():
    n, text = NAMES[i], TEXTS[i]
    ty = geo[n]["ty"] + cuts[i]
    segs = segs_by_row[n][:len(text)]
    a0, a1 = segs[k]
    x0 = a0 - 2 if k == 0 else max(a0 - 2, (segs[k - 1][1] + a0) // 2 + 1)
    x1 = a1 + 3 if k + 1 == len(text) else min(a1 + 3, (a1 + segs[k + 1][0]) // 2)
    y0g = ty - 3
    region = A[y0g:y0g + 32, x0:x1 + 1]
    bg = np.array(layout["rows"][n]["bg"])
    g = rgba_from(region, np.broadcast_to(bg, region.shape))
    name = "dot" if ch == "." else "d" + ch
    save_rgba(g, "glyph_%s.png" % name)
    glyphs[ch] = dict(file="glyph_%s.png" % name, left=int(a0 - x0), w=int(a1 - a0 + 1), dy=-3)
layout["glyphs"] = glyphs

# Mbps (lấy từ dòng AMS)
n = "AMS"
ty = geo[n]["ty"] + cuts[0]
reg = A[ty - 3:ty + 32, 605:662]
m = reg[:, :, 1] > 110
cols = np.where(m.any(0))[0]
rws = np.where(m.any(1))[0]
mx0, mx1 = cols.min() + 605 - 2, cols.max() + 605 + 2
my0 = rws.min() + ty - 3 - 2
region = A[my0:my0 + 36, mx0:mx1 + 1]
bg = np.broadcast_to(np.array(layout["rows"][n]["bg"]), region.shape)
save_rgba(rgba_from(region, bg), "mbps.png")
layout["mbps"] = dict(file="mbps.png", dy=int(my0 - ty), left=int(cols.min() + 605 - mx0), gap=18)

# --- thanh tốc độ ---
# Rãnh: dải của AMS, phần phải x>=722; phần trái lát từ đoạn giữa
n = "AMS"
g = geo[n]
ya, yb = g["by0"] + cuts[0] - BAR_MARGIN, g["by1"] + cuts[0] + BAR_MARGIN + 1
trk_r = A[ya:yb, 722:973]
trk_l = np.tile(A[ya:yb, 740:750], (1, 3, 1))[:, :722 - BAR_X]
save(np.concatenate([trk_l, trk_r], 1), "track.png")
# Phần tô: dải của SGPO1 (thanh dài nhất)
i = NAMES.index("SGPO1")
g = geo["SGPO1"]
ya, yb = g["by0"] + cuts[i] - BAR_MARGIN, g["by1"] + cuts[i] + BAR_MARGIN + 1
yc = (g["by0"] + g["by1"]) // 2 + cuts[i]
line = A[yc, BAR_X:973]
fx1 = np.where(line[:, 1] > 100)[0].max() + BAR_X
save(A[ya:yb, BAR_X:fx1 + 3], "fill.png")
layout["bar"] = dict(x=BAR_X, margin=BAR_MARGIN, cap=11, track_w=973 - BAR_X, fill_w=int(fx1 + 3 - BAR_X),
                     band_h=int(g["by1"] - g["by0"] + 1))
layout["bar"]["band_h_track"] = int(geo["AMS"]["by1"] - geo["AMS"]["by0"] + 1)

# --- chân trang: chữ "12 SERVERS DETECTED • TEST RESULTS • Mbps" ---
fy0, fy1 = 1420, 1456
reg = A[fy0:fy1, 200:850]
bgc = reg[:, :, :]
# nền sạch: nội suy theo chiều dọc giữa hàng trên và dưới dải chữ
band = A[fy0:fy1, 235:800]
txt_mask = (band[:, :, 2] - band[:, :, 0] > 150) & (band[:, :, 1] > 90)
rows = np.where(txt_mask.any(1))[0]
cols = np.where(txt_mask.any(0))[0]
ty0, ty1 = rows.min() + fy0, rows.max() + fy0
tx0, tx1 = cols.min() + 235, cols.max() + 235
pad = 3
top_row = A[ty0 - pad - 4, tx0 - 6:tx1 + 8]
bot_row = A[ty1 + pad + 4, tx0 - 6:tx1 + 8]
h = ty1 - ty0 + 1 + 2 * pad
clean = np.empty((h, tx1 - tx0 + 14, 3))
for k in range(h):
    t = (k + 1) / (h + 1)
    clean[k] = top_row * (1 - t) + bot_row * t
ft_region = A[ty0 - pad:ty0 - pad + h, tx0 - 6:tx1 + 8]
save_rgba(rgba_from(ft_region, clean[:, :ft_region.shape[1]]), "foot_text.png")
# phần sau số "12": từ chữ S đầu tiên
# tìm khoảng trống giữa "12" và "SERVERS"
colmask = txt_mask.any(0)
gaps = []
c = 0
xs = np.where(colmask)[0]
for a, b in zip(xs[:-1], xs[1:]):
    if b - a > 12:
        gaps.append((a + 235, b + 235))
first_gap = gaps[0]
layout["footer"] = dict(ty0=int(ty0 - pad), h=int(h), tx0=int(tx0 - 6), tx1=int(tx1 + 8),
                        num_x0=int(tx0), num_x1=int(first_gap[0]), rest_x0=int(first_gap[1]),
                        text_y0=int(ty0), text_y1=int(ty1), gap=int(first_gap[1] - first_gap[0]))
segs = []
s0 = p0 = xs[0]
for x in xs[1:]:
    if x != p0 + 1:
        segs.append([int(s0 + 235), int(p0 + 235)])
        s0 = x
    p0 = x
segs.append([int(s0 + 235), int(p0 + 235)])
layout["footer"]["segs"] = segs
layout["footer"]["chars"] = "12SERVERSDETECTED\u2022TESTRESULTS\u2022Mbps"
assert len(segs) == len(layout["footer"]["chars"]), (len(segs), len(layout["footer"]["chars"]))
save(clean, "foot_clean.png")
with open(os.path.join(OUT, "layout.json"), "w", encoding="utf-8") as f:
    json.dump(layout, f, ensure_ascii=False, indent=1)
print("OK ->", OUT)
print(json.dumps(layout["footer"]), layout["bar"])
