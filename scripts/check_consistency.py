# -*- coding: utf-8 -*-
u"""check_consistency.py —— 交付前自检（本 skill 的"规则够不够死"验收器）

跑法（在 skill 根目录）：
    python scripts/check_consistency.py

它检查四件事，全部是**机器可判**的（不需要人眼看图）：
    ① 过时令牌：改了口径之后，旧词有没有漏改 → 漏一处，下一个 agent 就照旧值生成
    ② 表格列数：Markdown 表格同一张表内 `|` 数量必须一致
    ③ 顶层目录链：① 行程总览 → ② 景点 → ③ 每日行程 → ④ 必读指南 → ⑤ 行程全貌
    ④ 模板自检：零裸字号 / 无过时 class / 新结构件齐备 / TOC 与 section id 对齐

★ 一个教训（写这个脚本时踩到的）：**扫描器自己会误报。** 四类假阳性必须排除，否则
  真问题会淹没在噪音里、反而没人看：
    A) **留档区与"黑名单/术语表"区**里的旧词是**有意保留的对照**（"不得写成：会毁行程"），
       不是残留 → 按**章节名**排除（`BAN_SECTIONS`）
    B) Markdown 单元格里**转义的 `\\|`** 会被误当成列分隔符 → 先剥掉再数
    C) **新旧目录链同形**（都含 ③ 和 ④），只按**旧链完整片段**判定，不能按"同时出现"判定
    D) **★ 第 4 类（Q40 新踩）：禁令句自己携带旧词** —— "**不出现「小白出片」等站内黑话**"
       是一句**必须写**的禁令，它当然含 `小白出片`。**别为了躲扫描器而把禁令写模糊** ——
       正确做法是给这一类加**行内标记**（`SOFT` 里的"站内黑话"等）。
       ★ 判据：**这句话是"在讲旧值"还是"在用旧值"？** 讲旧值 → 加标记，让扫描器闭嘴。

★ **扫描范围**（改口径时别漏）：
    在范围内 → `SKILL.md` + `references/*.md` + `assets/template.html`
    ★ **不在范围内** → 工作区里的 `攻略内容Schema-草案.md`（契约层）、`旅游攻略Skill-需求与进度监督文档.md`（SSOT）。
      改动口径后必须**另行对这两个文件 grep 一遍旧令牌** —— 否则会出现"reference 层改了、契约层没改"，
      下一个 agent 读契约层照样按旧值生成（= 本次改动作废）。
返回码：0 = 通过；1 = 有问题（问题行已打印）。
"""
import io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep
FILES = ["SKILL.md"] + sorted(
    "references/" + f for f in os.listdir(ROOT + "references") if f.endswith(".md"))

ORDER = ["行程总览", "景点", "每日行程", "必读指南", "行程全貌"]

# ── ① 过时令牌 ────────────────────────────────────────────────
# ★ 改口径时**要把旧词加进这张表** —— 这张表就是"令牌清单"，不写进来就等于没有自检。
TOKENS = [
    "appeal", "cost", "tac=",              # 已废字段（`cost`→`tip`，`appeal`→`why`；`tactics` 整体删除）
    "妙计", "值得的理由", "避坑提示",        # 已废字段名
    "会毁行程", "体验提示",                  # 已废徽标文案
    "mapbar", "maplink", "mapembed",        # 已废地图 class（L1 / L2）
    "travel.html?spots", "nearby-search.html?center",
    "默认档 L1", "L3 与 L1 并存",
    "两栏网格排布", "第 3 项名为**必读指南", "v0.14", "v0.15",
    # ── Q40 新增 ──
    "小白出片", "拍照攻略", "景点攻略",        # 已废小红书入口文案（属"制作思路"，不上页面）
    "三块地，两换店", "陡的不给爹妈",           # 已废口诀（自造暗语）
    "贴水坐筏", "先抢漓江，再抢遇龙",
    "气温",                                   # 天气卡的旧标题
    # ── Q42 新增 ──
    "安排依据",                                # 旧小节名 → 「安排思路」（Q42）
    'class="bd',                              # 旧严重度药丸 class（.bd read / .bd note）→ 整体废弃
    "已核实",                                  # 旧核验文案 → 「✅ 已核验（基于X）」；旧口径对照行须带标记
]
# 有意保留旧词的章节（对照 / 留档），这些章节内不报
BAN_SECTIONS = ("黑名单", "术语表", "留档", "取证记录", "取证", "反例", "缩写对照表")
# 行内出现这些标记 = 该行是在"讲旧值"，不是在"用旧值"
SOFT = ("原为", "旧名", "废止", "~~", "不得写成", "不得再", "曾是", "此前", "上一版", "推翻",
        "证伪", "误读", "移除", "改名", "改文", "已删", "去掉", "整体删除", "不做字段块",
        "留档", "取证", "Q39 用户", "Q40 用户", "用户指示", "职能**并入", "没有「", "不进产物", "病症",
        "同时站了", "标题写「气温」", "别写成气温", "旧标题", "改成", "反例", "左侧",
        # ★ 第 4 类假阳性：**「不许出现 X」这句禁令本身含 X** —— 禁令是必须写的，不是残留
        "站内黑话", "不上卡面", "典型天气",
        # ★ 第 4 类（Q41 再补）：**「X 已从契约层删除」这句是在"讲旧值"** ——
        #    为了让扫描器闭嘴而把这句话写模糊，等于删掉一条口径 → 加标记，不改句子。
        "删除", "作废", "废弃",
        # ★ Q42 补：新令牌对应的"讲旧值"标记
        "已废", "旧口径", "留痕")

OLD_CHAIN = ["必读指南 ★ → ④ 每日行程", "③ 必读指南 → ④ 每日行程", "③ 必读指南 ★"]

fail = []


def read(p):
    return io.open(ROOT + p, encoding="utf-8").read()


def ban_zones(lines):
    """禁写章节的行区间。★ 区间要**跨子标题**延伸 —— 只到下一个同级或更高级标题为止
    （踩过：`## 1. …留档` 的下一行就是 `### 1.1 …`，按"遇到任意标题即结束"会把区间切成一空段，
      结果整个留档区都没被排除，扫描器继续误报）。"""
    zones, cur, lvl = [], None, 9
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^(#{2,4})\s+(.*)$", ln)
        if m:
            n = len(m.group(1))
            if cur and n <= lvl:
                zones.append((cur, i - 1)); cur = None
            if cur is None and any(s in m.group(2) for s in BAN_SECTIONS):
                cur, lvl = i, n
    if cur:
        zones.append((cur, len(lines)))
    return zones


def npipe(s):
    return s.replace("\\|", "\u0001").count("|")


print("=" * 88); print(u"① 过时令牌（硬残留）"); print("=" * 88)
for f in FILES:
    lines = read(f).split("\n")
    zones = ban_zones(lines)
    for tok in TOKENS:
        for i, ln in enumerate(lines, 1):
            if tok not in ln or any(a <= i <= b for a, b in zones):
                continue
            if any(s in ln for s in SOFT):
                continue
            print(u"  !! %-26s L%-4d [%s] %s" % (f, i, tok, ln.strip()[:78]))
            fail.append("token")
print(u"  → 硬残留 %d 处" % len(fail))

print(); print("=" * 88); print(u"② 表格列数一致性"); print("=" * 88)
tb = 0
for f in FILES:
    lines = read(f).split("\n")
    i, n = 0, len(lines)
    while i < n:
        if lines[i].lstrip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
            h, j = npipe(lines[i]), i + 2
            while j < n and lines[j].lstrip().startswith("|"):
                if npipe(lines[j]) != h:
                    print(u"  !! %-24s L%-4d 列数 %d ≠ 表头 %d | %s"
                          % (f, j + 1, npipe(lines[j]), h, lines[j].strip()[:56]))
                    tb += 1
                j += 1
            i = j
        else:
            i += 1
print(u"  → 表格列数问题 %d 处" % tb); fail += ["table"] * tb

print(); print("=" * 88); print(u"③ 顶层目录链顺序"); print("=" * 88)
for f in FILES:
    t = read(f)
    m = re.search(u"①[^\\n]*行程总览[^\\n]*", t)
    if m:
        pos = [m.group(0).find(x) for x in ORDER]
        good = all(p >= 0 for p in pos) and pos == sorted(pos)
        print(u"  %-26s %s  %s" % (f, u"✓" if good else u"!!", m.group(0).strip()[:74]))
        if not good:
            fail.append("order")
    for frag in OLD_CHAIN:
        for i, ln in enumerate(t.split("\n"), 1):
            if frag in ln and not any(s in ln for s in SOFT):
                print(u"  !! %-24s L%-4d 旧链残留 [%s]" % (f, i, frag))
                fail.append("oldchain")

print(); print("=" * 88); print(u"④ 模板自检 assets/template.html"); print("=" * 88)
tpl = read("assets/template.html")
bare = len(re.findall(r"font-size:\s*(?!var\()", tpl))
print(u"  裸字号（font-size 必须跟 var(--fs-*)）  %d %s" % (bare, u"✓" if not bare else u"!!"))
fail += ["barefs"] * bare
for cls in ["mapbar", "maplink", "mapembed", 'class="qa"', 'class="tac"', "值得的理由", "妙计",
            'card-h">气温', "景点攻略", "拍照攻略"]:
    c = tpl.count(cls)
    if c:
        print(u"  过时 class/字段 %-16s %d  !!" % (cls, c)); fail.append("cls")
NEED = ["routewrap", "spotgrid", "spotcell", "xhsrow", "pfwrap", "pfgroup",
        "bring", "say", "jingle", "rfoot"]
miss = [c for c in NEED if tpl.count(c) == 0]
print(u"  结构件齐备（%d 项）%s %s" % (len(NEED), u"缺 " + str(miss) if miss else u"✓", u"" if not miss else u"!!"))
fail += ["miss"] * len(miss)
toc = re.findall(r'href="#([^"]+)"', tpl)
sec = re.findall(r'<section id="([^"]+)"', tpl)
bad_id = [x for x in toc if x not in sec]
print(u"  TOC ↔ section id：%s %s" % (u"对齐 ✓" if not bad_id else u"缺失 " + str(bad_id), u"" if not bad_id else u"!!"))
fail += ["toc"] * len(bad_id)
seq = [b for _, b in re.findall(r'<span class="n">(\d+)</span>([^<]+)', tpl)]
print(u"  TOC 文案顺序：%s %s" % (u"正确 ✓" if seq == ORDER else str(seq), u"" if seq == ORDER else u"!!"))
if seq != ORDER:
    fail.append("tocorder")
# ★ Q42：严重度药丸（`.bd`）与「某组无 hard 则 details 展开」例外**均已废止** ——
#    这两项从"必须有"**反转为"必须没有"**（否则下一个 agent 又把药丸/展开抄回来）。
for bad in ['class="bd', '<details open', '安排依据']:
    c = tpl.count(bad)
    if c:
        print(u"  模板仍有已废结构 %-14s %d  !!" % (bad, c)); fail.append("q42bad")
for part in ['class="pfi hard"', 'class="pfi soft"', 'class="reasons"', 'class="xhsrow"',
             'class="jingle"', 'class="say"', '小红书·游玩攻略', '小红书·拍照参考', '✅ 已核验（基于']:
    if tpl.count(part) == 0:
        print(u"  缺构件 %s  !!" % part); fail.append("part")

print(); print("=" * 88)
print(u"★ 结论：%s" % (u"全部通过 ✓✓" if not fail else u"仍有 %d 项问题，见上" % len(fail)))
print("=" * 88)
sys.exit(0 if not fail else 1)
