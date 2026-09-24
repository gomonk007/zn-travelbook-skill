# -*- coding: utf-8 -*-
"""strip_node_ids.py —— 交付前剥离 HTML 中的宿主编辑痕迹属性。

用法：
    python scripts/strip_node_ids.py <输出文件.html> [更多文件...]

为什么需要它：
    部分 agent 宿主环境会往 skills 目录下的 HTML 资产里注入
    data-page-node-id="..." 节点 ID（注入发生在资产被读取/变更后，
    资产源文件里无法根除）。这些属性对浏览器无意义、对读者是噪音，
    且会让交付物带上"编辑器半成品"痕迹。模板 assets/template.html
    的注释里已声明：交付物里不得出现任何此类属性。

    因此规则是：**生成产物后、交付前，必须跑一次本脚本。**

行为：
    · 原地删除所有 ` data-page-node-id="..."` 属性；
    · 打印每个文件剥离的数量与残留数（残留应为 0）；
    · 不改任何其他内容。
"""
import io
import re
import sys

ATTR = re.compile(r'\s+data-page-node-id="[^"]*"')


def strip_file(path):
    text = io.open(path, encoding='utf-8').read()
    cleaned, n = ATTR.subn('', text)
    if n:
        io.open(path, 'w', encoding='utf-8', newline='\n').write(cleaned)
    left = len(ATTR.findall(io.open(path, encoding='utf-8').read()))
    status = 'OK' if left == 0 else 'FAIL'
    print('%-4s %-60s 剥离 %4d 处，残留 %d' % (status, path, n, left))
    return left == 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    ok = all(strip_file(p) for p in argv[1:])
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
