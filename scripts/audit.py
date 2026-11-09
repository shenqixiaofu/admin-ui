#!/usr/bin/env python3
"""admin-ui 自检：只看还缺什么，不做审美判断。

用法:
    python3 audit.py                 # 扫当前目录
    python3 audit.py path/to/admin   # 扫指定目录
    python3 audit.py --json          # 机器可读

退出码: 0 正常
"""
import json
import re
import sys
import collections
import pathlib

CODE = ('.vue', '.tsx', '.jsx', '.svelte')
SKIP = ('node_modules', 'dist', 'build', '.git', '.next', '.nuxt', 'coverage')
VIEW_DIRS = ('views', 'pages', 'screens')

# 四态：只看有没有相关迹象。宁可少报，不要误报。
SIG = {
    '加载': r'loading|isLoading|pending|skeleton|骨架|Spin|Loader',
    '空态': r'暂无|暂未|还没有|尚未|没有数据|没有匹配|isEmpty|noData|empty[-_]?state|length\s*===?\s*0|!\w+\.length',
    '出错': r'catch\s*\(|onError|\.error\b|错误|失败|ErrorState|errorState',
    '权限': r'权限|permission|forbidden|403|无权限|unauthorized',
}

# 不需要四态的页面：登录、404、纯跳转
EXEMPT = re.compile(r'login|signin|sign_in|404|not[-_]?found|error[-_]?page|redirect', re.I)

# 布局外壳：只认真正的路由出口。<slot> 太宽泛 —— 任何用了具名插槽的页面都会命中
LAYOUT_SIG = re.compile(r'<[Rr]outer-?[Vv]iew|<Outlet|<[A-Z]\w*Layout[\s/>]', re.I)
LAYOUT_NAME = re.compile(r'^(?:\w*)?(?:layout|shell)$', re.I)


def iter_files(root: pathlib.Path, suffixes):
    for p in root.rglob('*'):
        if p.suffix not in suffixes:
            continue
        if any(s in p.parts for s in SKIP):
            continue
        yield p


def is_view(p: pathlib.Path) -> bool:
    parts = {x.lower() for x in p.parts}
    return any(d in parts for d in VIEW_DIRS)


def read(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return ''


def strip_noise(t: str) -> str:
    """去掉注释，避免把注释里的说明当成用户可见文案。"""
    t = re.sub(r'<!--.*?-->', '', t, flags=re.S)          # HTML
    t = re.sub(r'/\*.*?\*/', '', t, flags=re.S)            # 块注释
    t = re.sub(r'(?m)^\s*//.*$', '', t)                    # 行注释
    return t


def main():
    as_json = '--json' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    root = pathlib.Path(args[0] if args else '.').resolve()
    if not root.exists():
        print('目录不存在:', root, file=sys.stderr)
        sys.exit(2)

    views = sorted(p for p in iter_files(root, CODE) if is_view(p))
    if not views:
        print('没有找到页面级文件（路径需包含 views / pages / screens）')
        print('扫描根目录:', root)
        sys.exit(0)

    # 业务组件池：状态经常写在 view 的子组件里，只看 view 会假阴性
    comp_files = [p for p in iter_files(root, CODE) if 'components' in p.parts]
    # 逻辑池：错误处理通常在 composables / hooks / logic 里，store 是全局的不并入。
    # logic 是 nfc-stamp 的约定（views/admin/logic/useXxx.js），漏掉会把整页的 try/catch 判成缺失。
    hook_files = [p for p in iter_files(root, ('.js', '.ts', '.mjs'))
                  if any(x in p.parts for x in ('composables', 'hooks', 'logic'))]

    result = {'root': str(root), 'pages': [], 'global': {}}
    miss = collections.Counter()

    for f in views:
        own = read(f)
        stem = f.stem.lower()
        # 找同名子组件目录（components/**/logs/ 对应 Logs.vue）
        kws = {f.stem.lower(), f.stem.lower() + 's', f.stem.lower().rstrip('s')}
        extra = [p for p in comp_files if any(k in {x.lower() for x in p.parts} for k in kws)]
        # 找页面用的 composable，两条路都要走：
        # 1) 按名字猜（Home.vue → useHome.js），覆盖常见约定
        # 2) 按页面里实际 import 的路径找 —— Settings.vue 引的是 useAdminSettings.js，
        #    名字猜不出来，可 try/catch 全在里面
        hook_set = {p for p in hook_files if p.stem.lower() in {f'use{stem}', stem, f'use{stem}s'}}
        for spec in re.findall(r'''from\s+['"]([^'"]+)['"]''', own):
            if not any(x in spec for x in ('composables', 'hooks', 'logic')):
                continue
            tail = spec.rsplit('/', 1)[-1]
            hook_set |= {p for p in hook_files if p.stem == tail}
        hooks = sorted(hook_set)
        merged = own + '\n' + '\n'.join(read(p) for p in extra + hooks)

        row = {'file': str(f.relative_to(root)), 'lines': len(own.splitlines()),
               'subs': len(extra), 'hooks': len(hooks)}
        # 布局外壳与登录页不需要四态
        is_layout = bool(LAYOUT_SIG.search(own)) or bool(LAYOUT_NAME.search(f.stem))
        exempt = bool(EXEMPT.search(f.stem)) or is_layout
        row['exempt'] = exempt
        row['layout'] = is_layout
        # 不渲染集合的页面谈不上空态：纯表单/配置页永远不会有「没有数据」这一态
        row['has_list'] = bool(re.search(r'v-for|\w+\.map\(', merged))
        for k, pat in SIG.items():
            if k == '空态' and not row['has_list']:
                row[k] = None
                continue
            ok = bool(re.search(pat, merged, re.I))
            row[k] = ok
            if not ok and not exempt:
                miss[k] += 1
        result['pages'].append(row)

    all_code = '\n'.join(read(p) for p in iter_files(root, CODE))
    all_style = '\n'.join(read(p) for p in iter_files(root, ('.css', '.scss', '.less')))
    clean = strip_noise(all_code)

    # 1. 危险操作：只在确认类上下文里找。不匹配 title=，那是 tooltip 不是确认文案
    ctx = re.findall(r'(?:confirm|确认|Modal|dialog|Dialog|message|description)\s*[=:(][^\n]{0,120}', clean)
    ctx += re.findall(r'>\s*(?:确认)?(?:删除|清空|移除|注销)[^\n<]{0,60}<', clean)
    risky = [c for c in ctx if re.search(r'删除|清空|移除|注销', c)]
    # 说清单条对象的（这张/这个/该）不算问题；批量才必须给数量
    no_count = [c.strip() for c in risky
                if not re.search(r'\d|条|个|项|全部|所有|这张|这个|该条|此条', c)]

    # 2. 筛选状态是否进 URL
    url_sync = len(re.findall(r'route\.query|\.query\b|searchParams|location\.search', clean))

    # 3. hover 才显示的操作
    hover_only = len(re.findall(r'opacity-0[^"\']{0,80}group-hover:opacity-100', all_code + all_style))

    # 4. 可达性
    aria = len(re.findall(r'aria-label|aria-labelledby', all_code))
    disabled = len(re.findall(r'\bdisabled\b|:disabled=', all_code))

    # 5. 表格列宽
    tables = len(re.findall(r'<table|<el-table|<a-table|<Table\b', all_code))
    fixed = len(re.findall(r'table-layout:\s*fixed|table-fixed', all_code + all_style))

    # 6. 分页是否给「第 x/y 页」
    pager = len(re.findall(r'[Pp]agination|el-pagination|分页', all_code))
    pager_pos = len(re.findall(r'第\s*[\{`]?\w*[\}`]?\s*[/／]\s*[\{`]?\w*[\}`]?\s*页', all_code))

    result['global'] = dict(
        pages=len(views), missing=dict(miss),
        risky=len(risky), risky_no_count=len(no_count), samples=no_count[:5],
        url_sync=url_sync, hover_only=hover_only, aria=aria, disabled=disabled,
        tables=tables, table_fixed=fixed, pager=pager, pager_with_position=pager_pos,
    )

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f'扫描根目录: {root}')
    print(f'页面数: {len(views)}\n')

    w = min(max(max(len(p['file']) for p in result['pages']), 20), 44)
    print(f"{'页面':<{w}}  {'加载':^5}{'空态':^5}{'出错':^5}{'权限':^5} {'行数':>5} 子组件")
    print('-' * (w + 36))
    for p in result['pages']:
        # None = 该态对这个页面不适用（没列表就没有空态）
        marks = ''.join(('  —  ' if (p[k] is None or p['exempt']) else ('  ✓  ' if p[k] else '  ✗  '))
                        for k in SIG)
        name = p['file']
        if len(name) > w:
            name = '…' + name[-(w - 1):]
        print(f"{name:<{w}} {marks} {p['lines']:>5} {p['subs']:>6}")

    print('\n缺失统计:', ', '.join(f'{k} {v} 页' for k, v in miss.items()) or '四态齐全')
    layouts = [p['file'] for p in result['pages'] if p['layout']]
    if layouts:
        print('布局外壳（不需要四态）:', ', '.join(layouts))
        print('  ⚠ 布局外壳放在 views/ 下容易和真实页面混淆，建议移到 layouts/ 并改名')
    print('（— 表示该页面不适用四态；子组件数 > 0 说明状态写在子组件里，已合并判断）')

    g = result['global']
    print('\n— 全局 —')
    print(f"危险操作文案 {g['risky']} 处，没提数量 {g['risky_no_count']} 处"
          + ('  ← 删除/清空类要说清几条' if g['risky_no_count'] else ''))
    for s in g['samples']:
        print('    ·', s[:75])
    print(f"URL 状态同步 {g['url_sync']} 处" + ('  ← 筛选大概在本地状态，刷新即丢' if g['url_sync'] == 0 else ''))
    print(f"hover 才显示的操作 {g['hover_only']} 处" + ('  ← 触屏与键盘不可用' if g['hover_only'] else ''))
    print(f"aria-label {g['aria']} 处 | disabled {g['disabled']} 处"
          + ('  ← 提交按钮可能没有防重复' if g['disabled'] == 0 else ''))
    print(f"表格 {g['tables']} 个，固定列宽 {g['table_fixed']} 处"
          + ('  ← 未固定，翻页时列宽会抖' if g['tables'] and g['table_fixed'] == 0 else ''))
    print(f"分页相关 {g['pager']} 处，带「第 x/y 页」{g['pager_with_position']} 处"
          + ('  ← 只显示总数，用户不知道自己在哪' if g['pager'] and g['pager_with_position'] == 0 else ''))

    bad = sum(miss.values())
    print('\n判定：')
    if bad == 0 and g['hover_only'] == 0 and g['url_sync'] > 0:
        print('  基本达标，剩下的是文案与细节。')
    else:
        print(f'  {bad} 处状态缺口、{g["hover_only"]} 处 hover-only 操作、{g["url_sync"]} 处 URL 同步。')
        print('  顺序：先补状态 → 再调结构 → 最后套视觉。')


if __name__ == '__main__':
    main()
