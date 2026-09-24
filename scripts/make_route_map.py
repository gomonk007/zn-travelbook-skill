#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_route_map.py —— 为旅行攻略 HTML 生成"内嵌路线图"（L3 档）

做什么
    把行程里的景点坐标，变成 base64 内联进 HTML 的路线图：
      景点坐标 --direction/v1/driving--> 真实道路路径(polyline)
               --RDP 抽稀--> 每天一段有序点串（★ 每天一个独立 path 参数）
               --staticmap/v2--> PNG 底图 + 标记点 + 白底彩线
               --PIL 转 JPEG--> base64 字符串

为什么
    交付物是"单个可分享 HTML"，图片必须内联、不能外链。
    base64 图随文件转发，**产物里不含 Key**，转发是安全的。

需要什么
    一把腾讯位置服务 WebServiceAPI Key（申请 4 步见 references/map-capability.md §2）
    ⚠️ Key 是私密凭据：不要写进 SKILL.md / 不要提交到仓库 / 不要贴在公开对话里

三种模式
    --probe              诊断：各接口各打 1 次，报告可用状态（消耗配额，慎用）
    --demo               离线自检：解码 / 抽稀 / 预算 / 视野，全部不联网
    (默认) --input x.json --out y.json    正式出图
    ★ --split-days       **按天各出一张图**（地图放在「每日行程」里，每天一块）

用法
    python make_route_map.py --probe
    python make_route_map.py --demo
    python make_route_map.py --input trip.json --out route.json --split-days

Key 的三种给法（按优先级）
    1) --key XXXX
    2) 环境变量 TMAP_WEBSERVICE_KEY
    3) --key-file <路径>   （默认找 ./.workbuddy/tmap_key.txt）

输入 trip.json 格式
    {
      "size": "800*500",          // 可选，默认 800*500（×scale=2 → 输出 1600*1000，恰在像素上限内）
      "scale": 2,                 // 可选，默认 2；size×scale 的输出像素须 ≤160 万，超了自动降
      "zoom": 12,                 // 可选；不给则按坐标范围自动推（Mercator 反解，实测偏差<0.4%）
      "margin": 1.12,             // 可选，留边系数（几何口径，不是估出来的经验值）
      "max_path_points": 0,       // 可选，描线点数上限；0/不写 = 由 URL 预算自动反推（推荐）
      "max_jpeg_kb": 160,         // 可选，体积上限
      "path_color": "0xFF0000",   // 可选，动线主色（纯红实测最跳；青绿会隐身）
      "path_halo": true,          // 可选，白描边（强烈建议开：否则彩线会被路网吃掉）
      "days": [
        {"day": 1, "name": "抵达·市区",
         "stops": [[25.2638, 110.2948, "象鼻山"], [25.2715, 110.2965, "日月双塔"]]}
      ]
    }

输出 route.json
    {
      "ok": true,
      "base64": "data:image/jpeg;base64,...",
      "suggest_width": 1000, "suggest_height": 620,
      "caption": "真实道路走向（点位与顺序示意，非导航级）",
      "days": [{"day":1,"distance_m":9104,"duration_min":28,"traffic_lights":11,
                "path_points":87,"max_dev_m":21.3}],
      "max_dev_m": 21.3,            // 全图最大偏离（米）—— 判断"贴不贴路"的硬指标
      "url_len": 6120,
      "warnings": []
    }
    失败时 ok=false + warnings，调用方据此**整块删除 routewrap**（不要留空占位）。
"""

import argparse
import base64
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://apis.map.qq.com/ws"
UA = "travel-guide-once/0.12 (+staticmap checks)"

# ── 状态码（实测）──────────────────────────────────────────────
ST_OK = 0
ST_QUOTA = 121      # 此key每日调用量已达到上限  ← 按接口分项计，打满即全局失败
ST_NO_WS = 199      # 此key未开启WebserviceAPI功能
ST_BADKEY = 311     # key格式错误
ST_BAD_PARAM = 348  # 参数错误 ← 实测最常见的触发原因：输出像素数超上限
ST_BAD_SIZE = 368   # 图片大小格式错误
ST_URL_LONG = 414   # 服务端 URL 过长（网关直接返回 HTML，不是 JSON）

# ★ staticmap/v2 的真正约束（全部实测，非官方文档）：
#   ① 输出像素总数 = (size宽×scale) × (size高×scale) ≤ 约 160 万
#      官方文档写"size 上限 1680*1200"是错的 —— 1680*1200 = 202 万像素，实测直接 348。
#      实测边界：scale=2 size 800*500（160 万）✓ / scale=2 size 840*600（202 万）✗
#   ② URL 总长（编码后）≈ 8192 上限，超出网关返回 414 Request-URI Too Large。
#      实测：7759 字符 ✓ / 9659 字符 ✗（400 点 vs 500 点，紧凑编码）
#      ⚠️ 这条是【硬约束】，它决定了一张图最多能画几个点。取 7800 留余量。
MAX_OUTPUT_PX = 1_600_000
MAX_URL = 7800

# ★ 坐标小数位：5 位 ≈ 1.1 m 精度，画线足够（实测服务端接受）。
#   每点比 6 位省 2 字符；再配合"不编码 , | :"共省约 30% URL —— 直接换成更多点数。
COORD_DEC = 5
COORD_FMT = "%%.%df,%%.%df" % (COORD_DEC, COORD_DEC)

STATUS_HINT = {
    ST_QUOTA: "该接口当日额度已用尽 —— 明天自动重置；若刚申请就打满，去配额页点「一键分配」",
    ST_NO_WS: "Key 未勾选 WebServiceAPI 能力（去应用设置里勾上）",
    ST_BADKEY: "Key 格式错误（如已开启签名校验，必须另带 SK 签名）",
    ST_BAD_PARAM: "参数错误 —— 90% 是 size×scale 的输出像素超上限（见 MAX_OUTPUT_PX）",
    ST_BAD_SIZE: "size 写错或比例异常，不返回图片（size 与 scale 必须一起算）",
    ST_URL_LONG: "URL 过长（网关 414）—— 需减少描线点数，本脚本应已自动降点",
}


# ── Key ───────────────────────────────────────────────────────
def load_key(args):
    if args.key:
        return args.key.strip()
    env = os.environ.get("TMAP_WEBSERVICE_KEY", "").strip()
    if env:
        return env
    for p in ([args.key_file] if args.key_file else []) + [".workbuddy/tmap_key.txt", ".tmap_key"]:
        if p and os.path.exists(p):
            with io.open(p, encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
    return ""


# ── polyline 解码（格式为实测所得，非文档所载）─────────────────
# 腾讯 direction 的 polyline 是一个**扁平数字数组**：
#   [0] 首点纬度(度)  [1] 首点经度(度)
#   [2],[3] 第2点的 Δlat, Δlng（单位 1e-6 度，累加）
#   [4],[5] 第3点 …… 以此类推
#   解出点数 = (len + 2) / 2
# 实测核验：解出的点串长度与接口返回的 distance 字段吻合到 0.3%
#   （象鼻山→日月双塔：解出 1517 m vs 字段 1513 m）
def decode_polyline(arr):
    if not arr or len(arr) < 2:
        return []
    pts = [(float(arr[0]), float(arr[1]))]
    lat, lng = float(arr[0]), float(arr[1])
    for i in range(2, len(arr) - 1, 2):
        lat += float(arr[i]) / 1e6
        lng += float(arr[i + 1]) / 1e6
        pts.append((lat, lng))
    return pts


# ── 几何：距离 / Douglas-Peucker 抽稀 ─────────────────────────
# 为什么不用"等弧长抽稀"：等弧长在长直路上照样布点，在急弯处反而点不够，
#   结果是"点数花光了、弯还是被切掉"。RDP 按【最大偏离】分配点：
#   直路几乎不占点，弯道自动加密 —— 同样点数下贴合度约好一倍。
def path_length_m(pts):
    tot = 0.0
    for a, b in zip(pts, pts[1:]):
        tot += math.hypot((b[0] - a[0]) * 111320.0,
                          (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])))
    return tot


def _proj(pts, lat0):
    """经纬度 → 以 lat0 为基准的局部米制平面（画线误差计算用，非制图）。"""
    kx = 111320.0 * math.cos(math.radians(lat0))
    return [(p[1] * kx, p[0] * 111320.0) for p in pts]


def _seg_d(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    if t <= 0.0:
        return math.hypot(px - ax, py - ay)
    if t >= 1.0:
        return math.hypot(px - bx, py - by)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def rdp(pts, eps_m):
    """Douglas-Peucker：返回的点串对原路径的最大偏离 <= eps_m。"""
    n = len(pts)
    if n < 3:
        return list(pts)
    xy = _proj(pts, pts[0][0])
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = xy[i]
        bx, by = xy[j]
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            d = _seg_d(xy[k][0], xy[k][1], ax, ay, bx, by)
            if d > best:
                best, bi = d, k
        if best > eps_m:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [pts[i] for i in range(n) if keep[i]]


def max_dev_m(dense, thin):
    """密点路径对抽稀折线的最大偏离（米）—— "贴不贴路"的硬指标。"""
    if len(thin) < 2 or len(dense) < 2:
        return 0.0
    x0 = dense[0][0]
    D = _proj(dense, x0)
    T = _proj(thin, x0)
    mx = 0.0
    for px, py in D:
        d = min(_seg_d(px, py, T[j][0], T[j][1], T[j + 1][0], T[j + 1][1])
                for j in range(len(T) - 1))
        if d > mx:
            mx = d
    return mx


def fit_rdp(pts, budget):
    """在点数 <= budget 的前提下，把最大偏离压到最小。返回 (点串, 实际最大偏离)。"""
    if budget < 2:
        return list(pts[:budget]), 0.0
    if len(pts) <= budget:
        return list(pts), 0.0
    lo, hi = 0.5, 5000.0
    for _ in range(26):
        mid = (lo + hi) / 2.0
        if len(rdp(pts, mid)) > budget:
            lo = mid
        else:
            hi = mid
    out = rdp(pts, hi)
    return out, max_dev_m(pts, out)


def _merc(lat):
    """Web Mercator 的 y 坐标（无量纲）。"""
    return math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))


def pick_zoom(lat_span, lng_span, clat, w1, h1, margin=1.12):
    """按 Web Mercator 反解 zoom：保证所有点入画，取「装得下的最大整数档」。

    实测校准：两个相隔 1005.7 m 的标记点，zoom 13/14/15 的像素距与 Mercator
    理论偏差均在 0.4% 以内 → 公式可直接用。zoom **只接受整数**（小数报
    status=348），所以只能取档，步进是 2 倍视野。

    ⚠️⚠️ Q38 实测（桂林图）—— 两处单位错误会让 zoom 偏大 1 档、边界点被裁：
      ① **`w1`/`h1` 必须是 scale=1 语义的像素数**。本函数里 res = k/2^zoom 是
         「scale=1 时每像素多少米」。若把 `w*scale` 传进来，覆盖被高估 2 倍 →
         zoom 多走 1 档。实测后果：zoom=14 时 **37/186 路径点 + 3/11 景点出画**
         （芦笛岩、象鼻山、七星公园被切在画面外）。
      ② **need 必须用「投影米」**：经度 = lng_span×π/180×R（**不乘 cos**）；
         纬度 = (merc(lat_max) − merc(lat_min))×R。旧式 `lng×111320×cos` 与
         `lat×111320` 都低估约 10%（两个方向都多乘/漏乘了 cos）。
    ⚠️ 方向：zoom 越大 → 视野越小。要装下更大范围得用【更小】的 zoom，
       所以约束是"分辨率不能比 need/视口 更精细"，即 res >= res_min。
    """
    R = 6378137.0
    need_w = max(math.radians(lng_span) * R * margin, 1.0)
    half = lat_span / 2.0
    need_h = max((_merc(clat + half) - _merc(clat - half)) * R * margin, 1.0)
    res_min = max(need_w / float(w1), need_h / float(h1))
    k = 156543.03392 * math.cos(math.radians(clat))
    zi = int(math.floor(math.log(k / res_min) / math.log(2.0)))
    # 初始 zi 已满足 res >= res_min；再尝试往上走，只要仍装得下就取更大的档
    while zi < 17:
        res = k / (2.0 ** (zi + 1))
        if res * w1 >= need_w and res * h1 >= need_h:
            zi += 1
        else:
            break
    return max(4, min(17, zi))


# ── HTTP ──────────────────────────────────────────────────────
def _get_json(url, params=None, timeout=20):
    import requests
    r = requests.get(url, params=params, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"status": -1, "message": "非 JSON 响应: %s" % r.text[:120]}


def enc(v):
    """只保留 , | : 三个字符不编码 —— 实测服务端接受，URL 可省约 30%。"""
    return urllib.parse.quote(str(v), safe=",|:")


def _pairs(params):
    """参数统一成 (k, v) 序列。

    ⚠️ 兼容 dict 与 list-of-pairs 两种入参：**必须用 .items()**。
    直接 `for k, v in params` 迭代 dict 拿到的是 key 字符串，
    多字符 key 会被解包失败 → "too many values to unpack (expected 2)"。
    这是 v0.17 之前出图链路整体跑不通的根因（probe/出图/URL 预算三处全踩）。
    """
    return params.items() if isinstance(params, dict) else params


def build_url(params):
    return API + "/staticmap/v2?" + "&".join("%s=%s" % (k, enc(v)) for k, v in _pairs(params))


def fetch_image(params, timeout=45):
    """直发 GET 拿图片字节。**不用 requests**：它会把 , | : 重新编码，白省 30%。"""
    url = build_url(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.code == 414:
            return json.dumps({"status": ST_URL_LONG, "message": "URL 过长"}).encode("utf-8")
        return body
    except Exception as e:
        return json.dumps({"status": -1, "message": str(e)[:120]}).encode("utf-8")


def probe(key):
    """各接口各打 1 次，报告真实可用状态。会消耗配额。"""
    cases = [
        ("staticmap/v2       出图", f"{API}/staticmap/v2", {"center": "25.27,110.295", "zoom": "13", "size": "300*200", "key": key}, True),
        ("direction/v1/driving 算路", f"{API}/direction/v1/driving", {"from": "25.2638,110.2948", "to": "25.3040,110.2555", "key": key}, False),
        ("distance/v1/matrix 矩阵", f"{API}/distance/v1/matrix", {"mode": "driving", "from": "25.2638,110.2948", "to": "25.2715,110.2965", "key": key}, False),
        ("place/v1/search    搜点", f"{API}/place/v1/search", {"keyword": "象鼻山", "boundary": "region(桂林,0)", "page_size": 1, "key": key}, False),
        ("geocoder/v1        逆解析", f"{API}/geocoder/v1", {"location": "25.2638,110.2948", "key": key}, False),
    ]
    rows = []
    for name, url, params, binary in cases:
        try:
            if binary:
                res = fetch_image(params)
            else:
                res = _get_json(url, params)
        except Exception as e:
            rows.append((name, "ERR", str(e)[:60]))
            continue
        if binary:
            ok = isinstance(res, (bytes, bytearray)) and res[:1] == b"\x89"
            rows.append((name, "✓ 可用" if ok else "✗ 失败", "%d B" % len(res) if ok else _brief(res)))
        else:
            st = res.get("status")
            ok = st == ST_OK
            rows.append((name, "✓ 可用" if ok else "✗ %s" % st, "" if ok else _brief(res)))
        time.sleep(1.0)
    return rows


def _brief(res):
    if isinstance(res, (bytes, bytearray)):
        return res[:100].decode("utf-8", "ignore")
    return "%s %s" % (res.get("status"), res.get("message", ""))[:80]


# ── 出图参数守卫（约束来自实测，非官方文档）─────────────────────
def fit_size(size, scale, max_px=MAX_OUTPUT_PX):
    """把 size/scale 夹进 staticmap/v2 的真实像素上限内。

    返回 (size, scale, note)。note=None 表示无需调整。
    实测：真正卡的是"输出像素总数" = (宽×scale) × (高×scale) ≤ 约 160 万。
    """
    try:
        w, h = [int(x) for x in str(size).split("*")]
    except Exception:
        return "800*500", 1, "size 无法解析，已回退默认值"
    if w <= 0 or h <= 0:
        return "800*500", 1, "size 非正数，已回退默认值"
    if w * scale * h * scale <= max_px:
        return size, scale, None
    if w * h <= max_px:
        return size, 1, ("scale 由 %d 降为 1：%s 的 %d 倍图会超出 %d 万像素上限"
                         % (scale, size, scale, max_px // 10000))
    k = math.sqrt(max_px / float(w * h))
    nw, nh = max(50, int(w * k)), max(50, int(h * k))
    ns = "%d*%d" % (nw, nh)
    return ns, 1, "size 与 scale 双双超限，已缩为 %s（%d 万像素）" % (ns, nw * nh // 10000)


# ── 描线配色（实测选定）────────────────────────────────────────
# ★ 纯红 0xFF0000（Q36 由用户裁定）：在腾讯浅色底图上"最跳"的颜色。
#   实测对比（同参数只换色，统计线的像素彩色度）：
#     纯红 +56 ｜ 亮橙红 +31 ｜ 朱磦 +20 ｜ 深墨 -4（等于隐形）
#   ⚠️ 底图上本来就有红色 POI（实测一张 800×500 图里有 10 个红色团块），
#      纯红描线在个别位置会与 POI 混 —— 已知且可接受（用户已确认不纠结）。
ROUTE_COLOR = "0xFF0000"
ROUTE_WEIGHT = 5          # 基准线宽（scale=1）；实际 = 基准 × scale，白底 = 2 × 主色
ROUTE_HALO = True         # ★ 白描边。实测：没有它，彩线会被路网/POI 吃掉
MARKER_SIZE = "large"     # ⚠️ markers 的 color 参数实测【无效】：一旦传了就固定渲染成青蓝，
                          #    传什么值都一样；不传才是腾讯默认红。所以这里干脆不传。


# ── 出图 ──────────────────────────────────────────────────────
def build(trip, key, inject=None, dry=False):
    warnings = []
    days = trip.get("days", [])
    if not days:
        return {"ok": False, "warnings": ["输入里没有 days"]}

    # ── 1. 逐天：景点 + 逐段算路（每天独立成串）──────────────
    # ★ 关键：绝不能把各天的点串首尾相连 —— 实测那样会在"上一天终点 → 下一天起点"
    #   之间凭空画一条直线（桂林样例：day1→day2 跳 718 m，day2→day3 跳 3024 m，
    #   后者在图上是一条横穿市区、极其刺眼的长斜线）。
    #   🔧 Q37 二次修正：**也不能用 ";" 把多天串在同一个 path 里** —— 实测服务端会把整条
    #   path 当成**闭合填充多边形**来画（灰块占 32.7% 画面、被画面裁切，用户看到的就是
    #   "灰色半透明三角形"）。正解 = **每天一个独立的 `path` 参数**（见下方 path_strs）。
    day_paths, day_meta, stop_pts = [], [], []
    for d in days:
        stops = [s for s in d.get("stops", []) if len(s) >= 2]
        for j, s in enumerate(stops):
            stop_pts.append((float(s[0]), float(s[1]), d.get("day"),
                             str(s[2]) if len(s) > 2 and s[2] else "#%d" % (j + 1)))
        if len(stops) < 2:
            day_meta.append({"day": d.get("day"), "skipped": "少于 2 个点，不画线"})
            continue
        rp, dist, dur, lights = [], 0, 0, 0
        for i in range(len(stops) - 1):
            a, b = stops[i], stops[i + 1]
            res = inject if inject is not None else _get_json(
                f"{API}/direction/v1/driving",
                {"from": "%s,%s" % (a[0], a[1]), "to": "%s,%s" % (b[0], b[1]), "key": key})
            st = res.get("status")
            if st != ST_OK:
                warnings.append("第%s天 第%d段算路失败：%s %s · %s"
                                % (d.get("day"), i + 1, st, res.get("message", ""),
                                   STATUS_HINT.get(st, "")))
                continue
            r = res["result"]["routes"][0]
            seg = decode_polyline(r["polyline"])
            if not seg:
                continue
            rp += seg if not rp else seg[1:]
            dist += r.get("distance", 0)
            dur += r.get("duration", 0)
            lights += r.get("traffic_light_count", 0)
            time.sleep(0.3)
        day_paths.append(rp)
        day_meta.append({"day": d.get("day"), "name": d.get("name", ""),
                         "distance_m": dist, "duration_min": dur, "traffic_lights": lights,
                         "real_path": bool(rp)})

    usable = [rp for rp in day_paths if len(rp) >= 2]
    if not stop_pts and not usable:
        return {"ok": False, "warnings": warnings + ["没有任何可用坐标"]}

    # ── 2. 视野：所有点必须入画，且尽量占满 ─────────────────
    allp = [p for rp in usable for p in rp] + [(p[0], p[1]) for p in stop_pts]
    lats = [p[0] for p in allp]
    lngs = [p[1] for p in allp]
    clat, clng = (min(lats) + max(lats)) / 2.0, (min(lngs) + max(lngs)) / 2.0

    size, scale, note = fit_size(trip.get("size", "800*500"), int(trip.get("scale", 2)))
    if note:
        warnings.append(note)
    w, h = [int(x) for x in size.split("*")]
    if trip.get("zoom"):
        zoom = int(trip["zoom"])
    else:
        # ⚠️ 传 scale=1 语义的 w/h（不是 w*scale）—— 见 pick_zoom 文档字符串
        zoom = pick_zoom(max(lats) - min(lats), max(lngs) - min(lngs), clat,
                         w, h, float(trip.get("margin", 1.12)))
        # 内容宽高比与画面不匹配 → 整数 zoom 步进（2 倍）下会一侧留白偏多，提示（不强制改）
        _R = 6378137.0
        _nw = math.radians(max(lngs) - min(lngs)) * _R
        _nh = (_merc(max(lats)) - _merc(min(lats))) * _R
        if _nh > 1 and _nw > 1 and not trip.get("quiet_aspect"):
            _cratio = _nw / _nh
            _ratio = _cratio / (w / float(h))
            if _ratio > 1.25 or _ratio < 0.8:
                _tot = w * scale * h * scale          # 输出总像素（保持不变）
                _rw = math.sqrt(_tot * _cratio)
                sug_w = max(64, int(_rw / scale) // 4 * 4)
                sug_h = max(64, int(_tot / _rw / scale) // 4 * 4)
                warnings.append(
                    "内容宽高比 %.2f 与画面 %.2f 不匹配（差 %.1f 倍）：整数 zoom 只能整档取"
                    "（每档差 2 倍视野），会有一侧留白明显偏多。把 size 调成约 %d*%d 可让两侧"
                    "留白均匀（总留白量不变，观感更平衡）。"
                    % (_cratio, w / float(h), max(_ratio, 1.0 / _ratio), sug_w, sug_h))

    # ── 3. 标记点：只标景点，不标路径中间点 ─────────────────
    # ⚠️ 实测：markers 的 color 参数无效（传了就固定青蓝），故不传 —— 默认红图钉，
    #    与纯红动线同色系，观感更整。
    mk_size = trip.get("marker_size", MARKER_SIZE)
    markers = ""
    if stop_pts:
        markers = "size:%s|" % mk_size + "|".join(
            COORD_FMT % (la, ln) for la, ln, _, _ in stop_pts)

    # ── 4. 描线：白底彩线两层，每层内部按天用 ; 分段 ────────
    p_color = trip.get("path_color", ROUTE_COLOR)
    halo = bool(trip.get("path_halo", ROUTE_HALO))
    layers = 2 if halo else 1
    p_weight = max(1, min(20, int(trip.get("path_weight", ROUTE_WEIGHT * scale))))
    halo_w = min(20, p_weight * 2)
    bases = [("center", "%.6f,%.6f" % (clat, clng)), ("zoom", str(zoom)),
             ("size", size), ("scale", str(scale)), ("key", key)]
    if markers:
        bases.append(("markers", markers))
    prefixes = (["weight:%d|color:0xFFFFFF|" % halo_w, "weight:%d|color:%s|" % (p_weight, p_color)]
                if halo else ["weight:%d|color:%s|" % (p_weight, p_color)])

    # 固定开销 = 除点串之外的一切。
    # ★★ 每「层 × 天」都是一个独立 path 参数（见下方 path_strs 的构造理由）：
    #     把多天塞进同一个 path 里用 ";" 连接，服务端会把它画成一个巨大的**填充多边形**
    #     （实测：32.7% 的画面被灰块填满，且被裁切 = 用户看到的「灰色三角形」）。
    fixed = len(API) + len("/staticmap/v2?") + sum(len(k) + len(enc(v)) + 2 for k, v in bases)
    fixed += sum(len("&path=") + len(p) for p in prefixes) * len(usable) + 8

    # 点数预算：先估；再用【实测 URL 长度】闭环修正
    # ⚠️ 每点的实际字符数 = "25.26380,110.29480"(18) + "|"(1) = 19 —— 曾写成 14，低估 26%。
    # ⚠️ 总点数 = 每天点数之和 × 层数，而每天之和本身又 = budget × 层数
    #    → 分母必须是 layers²，不是 layers（写错会让初始 URL 直接超限一倍，白跑 2 轮重试）。
    per_char = 9 + 2 * COORD_DEC      # "25.26380,110.29480"(18) + "|"(1)，COORD_DEC=5 → 19
    budget = int(trip.get("max_path_points") or 0)
    if budget <= 0:
        budget = max(30, int((MAX_URL - fixed) / (layers * layers * per_char)))
    total_budget = budget * layers

    path_strs, thin_all, devs, url_len, per_day = [], [], [], 0, []
    for attempt in range(4):
        # 按每天路径长度加权分配点数（长的一天多分点）
        lens = [path_length_m(rp) for rp in usable]
        tot = sum(lens) or 1.0
        alloc = [max(2, int(round(total_budget * L / tot))) for L in lens]
        if alloc:
            alloc[-1] = max(2, total_budget - sum(alloc[:-1]))

        segs, thin_all, devs, per_day = [], [], [], []
        for k, rp in enumerate(usable):
            thin, dev = fit_rdp(rp, alloc[k])
            thin_all += thin
            devs.append(dev)
            per_day.append({"day": day_meta[k].get("day"), "path_points": len(thin),
                            "max_dev_m": round(dev, 1), "raw_points": len(rp)})
            segs.append("|".join(COORD_FMT % p for p in thin))
        # ★★ 每天一个**独立 path 参数**（点与点之间仍用 "|"）：
        #    实测三种写法的渲染结果 ——
        #      点用 "|"             → 正常细线 ✓（正确写法）
        #      点用 ";"             → 什么都不画（0 像素）
        #      多天用 ";" 串在一个 path 里 → 画成**巨大填充多边形**（32.7% 画面，被裁切）
        #    所以"跨天不要连成一条假直线"只能靠**重复 path 参数**实现，不能靠 ";"。
        #    展开顺序 = 先所有白描边层、后所有主色层 → 主色压在描边上，视觉正确。
        path_strs = [pre + seg for pre in prefixes for seg in segs]
        params = bases + [("path", s) for s in path_strs]
        url_len = len(build_url(params))
        if url_len <= MAX_URL:
            break

        # 闭环降级：拿实测长度按比例缩点数，而不是靠"猜一个安全值"
        over = url_len / float(MAX_URL)
        total_budget = max(10, int(total_budget / over / 1.02))
        warnings.append("URL %d 超上限 %d，已自动把描线点数压到 %d 重试"
                        % (url_len, MAX_URL, total_budget // layers))

    if dry:
        return {"ok": True, "dry_run": True, "url": build_url(params), "url_len": url_len,
                "markers": [markers], "zoom": zoom, "size": size, "scale": scale,
                "path_points": len(thin_all), "max_dev_m": round(max(devs) if devs else 0.0, 1),
                "days": day_meta, "per_day": per_day, "warnings": warnings}

    png = fetch_image(params)
    if png[:1] != b"\x89":
        try:
            j = json.loads(png.decode("utf-8"))
        except Exception:
            j = {"status": -1, "message": png[:120].decode("utf-8", "ignore")}
        st = j.get("status")
        return {"ok": False, "days": day_meta, "url_len": url_len, "per_day": per_day,
                "warnings": warnings + ["出图失败：%s %s · %s"
                                        % (st, j.get("message", ""), STATUS_HINT.get(st, ""))]}

    # ── 5. PNG → JPEG，压进体积预算 ─────────────────────────
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.size != (w * scale, h * scale):
        im = im.resize((w * scale, h * scale), Image.LANCZOS)
    max_kb = int(trip.get("max_jpeg_kb", 160))
    q, buf = 78, None
    while q >= 40:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        if buf.tell() / 1024.0 <= max_kb:
            break
        q -= 8
    kb = buf.tell() / 1024.0
    if kb > max_kb:
        warnings.append("压缩到 q40 仍有 %.0f KB（超预算 %d KB），建议缩小 size 或减少天数"
                        % (kb, max_kb))

    dev_map = {d.get("day"): d for d in per_day}
    for dm in day_meta:
        dm.update(dev_map.get(dm.get("day"), {}))
    worst = max(devs) if devs else 0.0
    if worst > 60:
        warnings.append("最大偏离 %.0f m 偏大（>60 m）—— 描线会明显不贴路，建议缩小范围或减天数"
                        % worst)
    real = any(d.get("real_path") for d in day_meta)
    return {
        "ok": True,
        "base64": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
        "suggest_width": w, "suggest_height": h,
        "out_px": w * scale * h * scale,
        "bytes_kb": round(kb, 1), "jpeg_quality": q,
        "url_len": url_len, "path_points": len(thin_all),
        # 视野三件套（下游可据此复现/校验：content bbox 是否真的落在画面内）
        "center": "%.6f,%.6f" % (clat, clng), "zoom": zoom,
        "size": size, "scale": scale,
        "url": build_url(params),
        "max_dev_m": round(worst, 1), "per_day": per_day,
        "caption": "真实道路走向（点位与顺序示意，非导航级）" if real else "点位与顺序示意",
        "days": day_meta, "warnings": warnings,
    }


# ── 按天出图（★ Q39：地图在「每日行程」里"每天一块"）───────────
def build_split(trip, key, dry=False):
    """每天各出一张图。

    为什么用"一天一个 trip 调 build()"，而不是"一次 build 出 N 张"：
      ① ★★ 「每天一个独立 path 参数」这条硬约束**天然成立** —— 一次请求里只有一条 path，
         不可能再犯"多天用 `;` 串成一个 path → 被渲染成闭合填充多边形"的错（Q38 真 bug，
         用户肉眼看到的是「灰色半透明三角形」）。
      ② 每天各自算自己的 center / zoom → 内容占画面更满，也不再有"某天点少被拉远"的问题。
      ③ 某天失败**不影响其他天** —— 降级是"只删那天那块"，与 Q39 定的规则一致。

    代价：请求次数 = 天数（不是 1 次），但**总算路次数不变**（每天都只算一次路）。
    """
    days = trip.get("days", [])
    if not days:
        return {"ok": False, "warnings": ["输入里没有 days"], "images": []}
    base = {k: v for k, v in trip.items() if k != "days"}
    # 单天范围小 → 画布可以小一档；每天一张 → 单张体积预算收紧（整页 ≤3 MB）
    base.setdefault("size", "720*450")
    base.setdefault("max_jpeg_kb", 100)
    # 单天的包围盒常常近似一条线（景点顺路排开）→ 宽高比提示在这档没有意义，静音
    base["quiet_aspect"] = True

    images, allw, ok_any = [], [], False
    for d in days:
        sub = dict(base)
        sub["days"] = [d]
        r = build(sub, key, dry=dry)
        item = {"day": d.get("day"), "name": d.get("name", "")}
        dm = (r.get("days") or [{}])[0]
        item.update({"distance_m": dm.get("distance_m"), "duration_min": dm.get("duration_min"),
                     "traffic_lights": dm.get("traffic_lights")})
        if r.get("ok"):
            ok_any = True
            item.update({"img": r.get("base64"), "dry_run": bool(r.get("dry_run")),
                         "url": r.get("url") if r.get("dry_run") else None,
                         "bytes_kb": r.get("bytes_kb"), "jpeg_quality": r.get("jpeg_quality"),
                         "url_len": r.get("url_len"), "path_points": r.get("path_points"),
                         "max_dev_m": r.get("max_dev_m"), "zoom": r.get("zoom"),
                         "size": r.get("size"), "scale": r.get("scale"),
                         "center": r.get("center"), "caption": r.get("caption"),
                         "suggest_width": r.get("suggest_width"),
                         "suggest_height": r.get("suggest_height")})
        else:
            item["skipped"] = (" / ".join(str(x) for x in r.get("warnings", []))[:160]
                               or "未出图（少于 2 个点 / 算路失败）")
        if r.get("warnings"):
            item["warnings"] = r["warnings"]
            allw += [(w if str(w).startswith("第") else "第%s天：%s" % (d.get("day"), w))
                     for w in r["warnings"]]
        images.append(item)

    tot = sum((i.get("bytes_kb") or 0) for i in images)
    return {"ok": ok_any, "images": images, "total_kb": round(tot, 1),
            "count": len([i for i in images if i.get("img")]), "warnings": allw}


# ── demo：离线自检 ────────────────────────────────────────────
def demo():
    print("=== ① polyline 解码自检（真实样本：桂林 象鼻山→日月双塔）===")
    sample = [25.263805, 110.2948, 13, 395, 0, 0, 534, -7]
    pts = decode_polyline(sample)
    print("  输入 %d 个数字 → 解出 %d 点（期望 %d）" % (len(sample), len(pts), len(sample) // 2))
    assert len(pts) == len(sample) // 2, "点数公式不符"
    assert abs(pts[0][0] - 25.263805) < 1e-9 and abs(pts[0][1] - 110.2948) < 1e-9, "首点不符"
    assert len(decode_polyline([0.0] * 402)) == 201, "402 元素未解出 201 点"
    print("  ✓ 首点与请求 from 一致；402 元素 → 201 点")
    print()

    print("=== ② RDP 抽稀自检（按最大偏离，而非等弧长）===")
    dense = []
    la, ln = 25.2638, 110.2948
    for i in range(400):
        la += 0.00006
        ln += 0.00010 * math.sin(i / 25.0)     # 平滑弯道 → 点数越松偏离越大，可验梯度
        dense.append((la, ln))
    devs = []
    for n in (30, 60, 120):
        thin, dev = fit_rdp(dense, n)
        devs.append(dev)
        print("  预算 %3d 点 → 实得 %3d 点，对原路径最大偏离 %.1f m" % (n, len(thin), dev))
        assert len(thin) <= n, "超出点数预算"
        assert thin[0] == dense[0] and thin[-1] == dense[-1], "首末点未保留"
    print("  ✓ 预算越松偏离越小：30 点 %.1f m → 60 点 %.1f m → 120 点 %.1f m"
          % tuple(devs))
    assert devs[0] >= devs[1] >= devs[2], "RDP 单调性不符"
    print()

    print("=== ③ 视野自检（Mercator 反解，应保证内容入画）===")
    # ⚠️ 尺寸传 scale=1 语义（800×500），与 pick_zoom 内 res 的定义一致
    R_ = 6378137.0
    cases = [
        (0.0402, 0.0515, 25.28, 800, 500, "桂林三天（宽图）"),
        (0.0402, 0.0515, 25.28, 500, 500, "同内容（方图）"),
        (0.5, 0.5, 25.28, 800, 500, "跨度大 10 倍"),
    ]
    for la_s, ln_s, cl, w1, h1, why in cases:
        z = pick_zoom(la_s, ln_s, cl, w1, h1, 1.12)
        res = 156543.03392 * math.cos(math.radians(cl)) / (2.0 ** z)
        need_w = math.radians(ln_s) * R_ * 1.12
        half = la_s / 2.0
        need_h = (_merc(cl + half) - _merc(cl - half)) * R_ * 1.12
        cov_w, cov_h = res * w1, res * h1
        print("  %-20s span %.4f×%.4f @%d×%d → zoom=%d，覆盖余量 宽%.2f× 高%.2f×（%s）"
              % (why, la_s, ln_s, w1, h1, z, cov_w / need_w, cov_h / need_h,
                 "✓ 装得下且有余量" if cov_w >= need_w and cov_h >= need_h else "✗ 切边！"))
        assert cov_w >= need_w and cov_h >= need_h, "视野不足，会切边"
    # ★ 回归哨兵（Q38 实测值）：桂林样例必须给 13 —— zoom=14 时 37/186 点 + 3/11 景点出画
    assert pick_zoom(0.0402, 0.0515, 25.28, 800, 500, 1.12) == 13, "zoom 档位回归（应为 13）"
    print("  ✓ 回归哨兵：桂林样例 zoom=13（实测 14 会切掉边界景点）")
    print()

    print("=== ④ 尺寸守卫自检（边界值来自实测）===")
    cases = [
        ("800*500", 2, "800*500", 2, "160 万像素 → 原样通过（默认值）"),
        ("1400*868", 1, "1400*868", 1, "122 万像素 → 原样通过"),
        ("840*600", 2, "840*600", 1, "202 万 → 降 scale 到 1"),
        ("1000*620", 2, "1000*620", 1, "200 万 → 降 scale 到 1"),
        ("1680*1200", 1, None, 1, "202 万且 scale 已为 1 → 必须缩 size"),
    ]
    for sz_in, sc_in, sz_exp, sc_exp, why in cases:
        sz_out, sc_out, note = fit_size(sz_in, sc_in)
        px = int(sz_out.split("*")[0]) * sc_out * int(sz_out.split("*")[1]) * sc_out
        assert px <= MAX_OUTPUT_PX, "%s 仍超上限" % sz_in
        if sz_exp:
            assert (sz_out, sc_out) == (sz_exp, sc_exp), "%s 期望 %s@%d" % (sz_in, sz_exp, sc_exp)
            mark = "原样"
        else:
            mark = "缩为 %s" % sz_out
        print("  %-10s @%d → %-10s @%d  %s ｜ %s" % (sz_in, sc_in, sz_out, sc_out, mark, why))
    print()

    print("=== ⑤ URL 组装 + 预算闭环自检（离线，不联网）===")
    r = selftest()
    print("  URL = %d 字符（上限 %d）｜ 描线 %d 点 ｜ 最大偏离 %.1f m"
          % (r["url_len"], MAX_URL, r["path_points"], r["max_dev_m"]))
    assert r["url_len"] <= MAX_URL, "URL 超上限"
    for d in r["per_day"]:
        print("    第%s天：%d 点（原始 %d），最大偏离 %.1f m"
              % (d["day"], d["path_points"], d["raw_points"], d["max_dev_m"]))
    print("  ✓ 参数组装通过")
    print()
    print("全部自检通过 ✓")


def selftest():
    """离线核对 URL 组装与点数预算：合成 direction 返回跑完整流程，不发任何请求。"""
    def mkpoly(n):
        """弯曲的假 polyline（差分整数，单位 1e-6 度）。
           纯直线两点就能表达，测不出抽稀预算；必须带弯道才有代表性。"""
        pl = [25.2638, 110.2948]
        for i in range(n):
            pl += [int(180 + 140 * math.sin(i / 9.0)), int(-120 + 90 * math.cos(i / 7.0))]
        return pl

    fake = {"status": 0, "message": "Success",
            "result": {"routes": [{"mode": "DRIVING", "distance": 9104, "duration": 28,
                                   "traffic_light_count": 11, "polyline": mkpoly(199)}]}}
    trip = {"days": [
        {"day": 1, "name": "市区一日",
         "stops": [[25.2638, 110.2948, "象鼻山"], [25.2750, 110.2900, "日月双塔"]]},
        {"day": 2, "name": "城里一日",
         "stops": [[25.2806, 110.3013, "东西巷"], [25.3040, 110.2555, "芦笛岩"]]},
    ]}
    return build(trip, "FAKE_KEY_FOR_OFFLINE_CHECK", inject=fake, dry=True)


# ── main ─────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="生成内嵌路线图（L3）")
    ap.add_argument("--key"), ap.add_argument("--key-file")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--input"), ap.add_argument("--out")
    ap.add_argument("--split-days", action="store_true",
                    help="★ 按天各出一张图（地图放在「每日行程」里，每天一块；推荐）")
    a = ap.parse_args()

    if a.demo:
        demo()
        return 0
    if a.selftest:
        r = selftest()
        print("URL = %d 字符（上限 %d）｜ 描线 %d 点 ｜ 最大偏离 %.1f m"
              % (r["url_len"], MAX_URL, r["path_points"], r["max_dev_m"]))
        return 0

    key = load_key(a)
    if a.probe:
        if not key:
            print("✗ 没找到 Key。三种给法见文件头注释。")
            return 2
        print("=== Key 诊断（每个接口各 1 次，会消耗配额）===")
        for name, st, extra in probe(key):
            print("  %-28s %-8s %s" % (name, st, extra))
        return 0

    if not (a.input and a.out):
        ap.print_help()
        return 2
    if not key:
        print("✗ 没找到 Key（--key / TMAP_WEBSERVICE_KEY / --key-file）")
        return 2

    trip = json.loads(io.open(a.input, encoding="utf-8").read())

    # ── ★ 按天出图（Q39：地图在每日行程里"每天一块"）──────────
    if a.split_days:
        res = build_split(trip, key)
        io.open(a.out, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=2))
        if res.get("ok"):
            print("✓ 按天出图：%d/%d 天成功，合计 %.0f KB（整页图片预算 ≤3 MB）"
                  % (res["count"], len(res["images"]), res["total_kb"]))
            for i in res["images"]:
                if i.get("img"):
                    print("  第%s天 %s：%.1f km / %d 分钟 / 描线 %s 点 / 偏离 %.1f m / %d KB / zoom %s"
                          % (i.get("day"), i.get("name", ""),
                             (i.get("distance_m") or 0) / 1000.0, i.get("duration_min") or 0,
                             i.get("path_points", "-"), i.get("max_dev_m", 0.0),
                             i.get("bytes_kb", 0), i.get("zoom", "-")))
                else:
                    print("  第%s天：未出图 → **整块删掉那天的 routewrap**（不留空占位）—— %s"
                          % (i.get("day"), i.get("skipped", "")))
        else:
            print("✗ 全部未出图（所有天都删掉 routewrap，不要留空占位）：")
        for w in res.get("warnings", []):
            print("  ! %s" % w)
        print("  已写入 %s" % a.out)
        return 0 if res.get("ok") else 1

    res = build(trip, key)
    payload = dict(res)
    payload.pop("base64", None)
    if res.get("base64"):
        payload["base64_head"] = res["base64"][:60] + "...(%d KB)" % int(len(res["base64"]) / 1024)
    io.open(a.out, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("ok"):
        print("✓ 出图成功：%d KB / q%d / %dx%d"
              % (res["bytes_kb"], res["jpeg_quality"], res["suggest_width"], res["suggest_height"]))
        print("  描线 %d 点（URL %d/%d 字符）｜ 最大偏离 %.1f m"
              % (res["path_points"], res["url_len"], MAX_URL, res["max_dev_m"]))
        print("  图注：%s" % res["caption"])
        for d in res.get("days", []):
            if d.get("distance_m"):
                print("  第%s天 %s：%.1f km / %d 分钟 / %d 个红绿灯 / 描线 %s 点 / 偏离 %.1f m"
                      % (d.get("day"), d.get("name", ""), d["distance_m"] / 1000.0,
                         d["duration_min"], d["traffic_lights"],
                         d.get("path_points", "-"), d.get("max_dev_m", 0.0)))
    else:
        print("✗ 未出图（调用方应整块删除 routewrap，不要留空占位）：")
    for w in res.get("warnings", []):
        print("  ! %s" % w)
    print("  已写入 %s" % a.out)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
