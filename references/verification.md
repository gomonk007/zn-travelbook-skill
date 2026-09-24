# 验证：默认不做，只在例外时做一次

> **这份文件管什么**：什么情况下需要**肉眼验证**产出，以及怎么做。
> **怎么用**：默认流程**不需要读这份**。只有在命中下面三种例外时，才照本文档执行。

## 1. 为什么默认不验证

**套用 `../assets/template.html` = 排版零风险 = 不需要验证排版。**

排版是**定死的资产**，不是每次重新创造的东西。实测教训：**每次重写 CSS，就会引出反复截图调排版，把时间全耗在"验证"上。**
先把排版固化一次、之后每次不再碰它，比"生成 → 截图 → 调 → 再截图"省得多。

> **要治的是根因（排版不定），不是症状（少截几张图）。**

## 2. 只有这三种情况才需要看一眼

1. **改动了 `../assets/template.html` 本身** → 验证一次，确认没改坏；
2. 内容里有**超长无空格文本**或**列数很多的表格**（可能撑破容器）；
3. 用户反馈了具体的显示问题。

**要做也只做一次。**

## 3. 怎么做（一次全页截图）

用系统自带的 Chromium 内核浏览器（Chrome / Edge）无头模式截图，**不需要装任何东西**：

```
<浏览器可执行文件> --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=1 --virtual-time-budget=6000 \
  --window-size=1400,4000 --screenshot=out.png "file:///<产物绝对路径>"
```

`<浏览器可执行文件>` 取本机已安装的 Chrome 或 Edge 路径（Windows 常见位置：`Program Files` 下的 `Google/Chrome/Application/chrome.exe` 或 `Microsoft/Edge/Application/msedge.exe`；macOS 为 `/Applications/` 下的同名 `.app/Contents/MacOS/` 可执行文件）。路径含空格时用引号包住。

## 4. 三条实测踩过的坑（保留备查）

1. **`--force-device-scale-factor=1` 不能漏** —— 漏了只会截到页面左侧一截，看起来像横向溢出，其实是假象。
2. **`--window-size` 在小视口下不改变实际宽度** —— 要 430px 时可能恒定在 485–497px。**所以不要用截图判断移动端是否溢出**，必然被裁边，会白白引出一轮长排查。
3. **判断是否溢出要用 JS 注入** —— 把 `clientWidth` / `scrollWidth` 与溢出元素列表塞进 `document.title`，再用 `--dump-dom` 抓出来；`SCROLL == VIEW` 即无真实溢出。

## 5. 不要做的事

- ❌ **不要单独做手机端验证** —— 模板的窄屏断点已固定，套用即生效；
- ❌ **不要为看一个局部反复截图** —— 一次全页截图后按 y 轴切段看，比滚动截图快得多；
- ❌ **不要改一点截一次** —— 把所有问题一次改完，再截一次看完所有问题。
- ❌ **浏览器起不来时不要反复重试** —— 详见 §7，那是环境限制，重试纯属浪费轮次。
- ❌ **用户说了"验证我来做"就别再验证** —— 直接交付。用户的验收比自动截图更权威，也省额度。

## 6. 封面图后处理

生图 → 按 **16:9** 裁剪 → 压到 **≤400KB** → base64 内联。

裁完**放大右下角局部确认水印已裁掉**（比全页截图省事得多）。

## 7. 浏览器不可用时的兜底：结构化自检

**症状**：Chrome / Edge 无头模式**静默失败** —— 连 `--version` 都无输出，`--screenshot` 不产出文件，退出码正常。**加 `--no-sandbox` / 换 Edge / 放开沙箱（`dangerouslyDisableSandbox`）全都无效** —— 这是环境限制，不是权限问题。**重试上限 2 次，超了就转兜底。**

**兜底做法**：用 Python 做**结构化自检**，把真实风险点逐条排掉。截图验证不了的东西，这些能验证：

```python
# 1) 标签闭合（HTMLParser 栈式配对；注意 svg 自闭合标签要进 VOID 白名单）
VOID = {'img','br','meta','link','input','hr','source','use','rect','circle',
        'path','line','ellipse','stop','polygon','polyline'}
# 期望：unclosed == [] 且 errors == []

# 2) 占位符 / 模板残留
for bad in ['%s','None<','{城市}','{N}','__COVER_B64__','>undefined<','href=""']: ...

# 3) 图片内联与零外链
s.count('<img ')  # == 封面 1 + 景点图 + 分类图
bool(re.search(r'<img[^>]+src="https?:', s))   # 必须 False
s.count('data:image/jpeg;base64')              # == img 总数

# 4) 体积（先剥 base64 再数结构，否则正则会被 base64 拖死）
s2 = re.sub(r'data:image/jpeg;base64,[A-Za-z0-9+/=]+', 'IMG', s)

# 5) 结构完整性
re.findall(r'<section id="([a-z]+)"', s2)   # 顶层目录恰好 5 项
re.findall(r'<span class="n">(\d+)</span>([^<]+)</a>', s2)   # TOC 文案
s2.count('<img ')  # 与 inlined 数一致

# 6) 分层折叠 + 严重度点 + 核验行（Q42 新增断言）
s2.count('<details')            # 每组「soft」1 个 + 可跳组 1 个
s2.count('<details open')       # ★ 必须 == 0 —— details 一律默认收起，没有例外
s2.count('class="pfi hard"')    # 与「必读」条目数一致（标题前暗红点）
s2.count('class="pfi soft"')    # 与「提示」条目数一致（标题前暗黄点）
s2.count('class="bd')           # ★ 必须 == 0 —— 文字徽标已废
s2.count('未核实')              # ★ 必须 == 0 —— 不上页面
s2.count('小红书·游玩攻略')      # 与挂了出口的景点卡数一致（单行、不带景点名）
```

**判读要点**
- 景点卡数 = 必去 + 值得 + 可选 + **可跳（折叠内，不给图）**；**图片数 = 景点卡数 − 可跳数**。对不上就是漏图或多图。
- `10/5`、`0/5` 这类日期串会**误命中数字评分正则** —— 查评分前先把 `\d+/\d+` 日期形态排除。
- 零外链是硬指标，`<a href>` 的跳转出口（小红书 / 地图）不算外链依赖。

**兜底覆盖不到的**：真实排版观感、图片裁切是否切歪、字体回退是否难看。**这些交给用户验收**，并在交付时说明"未做视觉验证"。
