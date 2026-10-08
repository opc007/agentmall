"""P0 自检：商品卡片/详情页类目图（issue #2）。

跑法：cd /workspace/agentmall && .venv/bin/python tests/test_category_images.py

分两段：
  A. 纯逻辑 / 静态检查（不起服务）——映射全不对、文件不存在，这里就挂。
  B. 起真实 uvicorn（临时库 AGENTMALL_DB=/tmp/xxx_<pid>.db，跑完删）验真实 HTML。

---- 一处必须说明的接线前提 --------------------------------------------------
用户面 `agentmall/web/app.py` 注册 cat_url 过滤器的活儿**不归本任务**（并行任务负责），
而 app.py 不注册的话 `{{ p.category | cat_url }}` 会直接抛
`No filter named 'cat_url'` → 首页 500（不是破图，是整页挂）。
所以本测试的启动器做一件等价的事：**先看 app.py 有没有注册，没注册就替它注册一次**，
再起 uvicorn。这样：
  * 并行任务的注册还没落地 → 本测试照样能验证模板/CSS/映射本身是否正确；
  * 已经落地            → 走的是 app.py 原生路径，两边结果一致。
打印里会写明本次走的是哪条路径。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentmall.web import catimg  # noqa: E402

IMG_DIR = ROOT / "agentmall" / "web" / "static" / "img" / "category"
STATIC_DIR = ROOT / "agentmall" / "web" / "static"
CATEGORIES = list(catimg.CATEGORY_IMG)  # 15 个

PASS, FAIL = 0, 0


def check(ok: bool, label: str, extra: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {label}" + (f" — {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  ❌ {label}" + (f" — {extra}" if extra else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{'─' * 66}\n{title}\n{'─' * 66}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, timeout: int = 15) -> tuple[int, str, str]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # 跟到终点，自己看落地 URL
            return None

    op = urllib.request.build_opener(NoRedirect)
    try:
        r = op.open(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        r = e
    body = r.read()
    return r.status, body.decode("utf-8", "ignore"), r.geturl()


# =============================================================== A. 纯逻辑
def test_mapping() -> None:
    section("A1 · 15 个类目全部映射到 /static/img/category/cat-*.jpg")
    bad = []
    for c in CATEGORIES:
        url = catimg.img_url(c)
        if not re.fullmatch(r"/static/img/category/cat-[a-z0-9-]+\.jpg", url):
            bad.append((c, url))
    check(not bad and len(CATEGORIES) == 15,
          f"15 个类目 img_url 形状正确（实际 {len(CATEGORIES)} 个）", f"异常 {bad[:3]}")

    section("A2 · 映射到的每个文件在磁盘上真实存在（最关键：错了就是破图）")
    missing = []
    for c in CATEGORIES:
        f = catimg.img_file(c)
        p = IMG_DIR / f
        if not p.is_file() or p.stat().st_size == 0:
            missing.append(f"{c} -> {f}")
    check(not missing, f"{len(CATEGORIES)} 个类目图片文件全部存在且非空",
          f"缺 {len(missing)} 个: {missing}")

    section("A3 · 未知类目 / 空串 / None 全部回落到 cat-shipin.jpg")
    for label, val in (("未知类目", "不存在的类目"), ("空串", ""), ("None", None),
                       ("纯空格", "   ")):
        u = catimg.img_url(val)
        check(u.endswith("/cat-shipin.jpg") and (IMG_DIR / "cat-shipin.jpg").is_file(),
              f"{label} → cat-shipin.jpg", u)
    check((IMG_DIR / catimg.FALLBACK).is_file(), "兜底图 cat-shipin.jpg 在磁盘上存在")

    section("A4 · 模板静态检查：懒加载 / onerror 兜底 / 不写死文件名")
    tpl = ROOT / "agentmall" / "web" / "templates"
    for name in ("index.html", "shop.html", "product.html", "merchant_portal.html"):
        body = (tpl / name).read_text(encoding="utf-8")
        has_filter = "| cat_url" in body
        has_lazy = 'loading="lazy"' in body
        has_err = "onerror=" in body and "cat-shipin.jpg" in body
        check(has_filter, f"{name}: 走 | cat_url 过滤器（不写死文件名）")
        if name in ("index.html", "shop.html", "merchant_portal.html"):
            check(has_lazy, f"{name}: 卡片图有 loading=\"lazy\"")
        check(has_err, f"{name}: 图有 onerror 兜底")

    css = (ROOT / "agentmall" / "web" / "static" / "style.css").read_text(encoding="utf-8")
    for sel in (".g-img", ".pd-img", ".m-img"):
        check(sel in css, f"style.css 有 {sel} 规则")
    check("@media (max-width: 480px)" in css, "style.css 有 480px 移动端断点")
    gimg = re.search(r"\.g-img\s*\{(.*?)\}", css, re.S)
    check(bool(gimg) and "object-fit: cover" in gimg.group(1),
          ".g-img 是正方形裁剪（object-fit: cover）")
    check(bool(gimg) and "aspect-ratio: 1 / 1" in gimg.group(1),
          ".g-img 锁 1:1 比例（两列卡片不变形）")


# =============================================================== B. 真起服务
LAUNCHER = '''
import sys
sys.path.insert(0, {root!r})
from agentmall.web import app as appmod, catimg
if {need_reg}:
    catimg.register(appmod.templates.env)   # app.py 还没注册时，测试替它注册
import uvicorn
uvicorn.run(appmod.app, host="127.0.0.1", port={port}, log_level="warning")
'''


def test_live() -> None:
    section("B0 · 起真实 uvicorn（临时库）")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    db = f"/tmp/am_catimg_{os.getpid()}.db"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)

    # 先在本进程里看一眼 app.py 有没有注册 cat_url（决定启动器要不要代劳），
    # 只读属性检查，不改 app.py 一个字节。
    from agentmall.web import app as appmod
    need_reg = "cat_url" not in appmod.templates.env.filters
    print(f"     ℹ️ app.py 现状：{'未' if need_reg else '已'}注册 cat_url 过滤器"
          f"{'（测试用等价一行代劳，跑的是真实路由+真实静态文件）' if need_reg else ''}")

    launcher = Path(f"/tmp/am_catimg_launch_{os.getpid()}.py")
    launcher.write_text(LAUNCHER.format(root=str(ROOT), port=port,
                                        need_reg=need_reg), encoding="utf-8")
    env = dict(os.environ, AGENTMALL_DB=db, PYTHONPATH=str(ROOT),
               AGENTMALL_PUBLIC_URL=base)
    proc = subprocess.Popen([sys.executable, str(launcher)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                get(f"{base}/healthz", timeout=1)
                up = True
                break
            except Exception:
                time.sleep(0.5)
        if not check(up, f"Web 启动成功 :{port}（库 {os.path.basename(db)}）"):
            return

        section("B1 · 首页商品卡片")
        st, body, _ = get(f"{base}/")
        check(st == 200, f"首页可打开（{st}）")
        srcs = re.findall(r'<img[^>]*?\bsrc="([^"]+)"', body)
        check(len(srcs) >= 10, f"首页至少 10 张 <img>（实际 {len(srcs)}）")
        off = [s for s in srcs if not s.startswith("/static/img/category/")]
        check(not off, f"全部 src 指向 /static/img/category/（共 {len(srcs)} 张）",
              f"异常 {off[:3]}")
        # /static/xxx → static 目录下的 xxx（app.py 把 static 挂在 /static）
        missing = sorted({s for s in srcs
                          if not (STATIC_DIR /
                                  re.sub(r"^/static/", "", s)).is_file()})
        check(not missing, "首页所有 src 对应文件真实存在（无破图风险）",
              f"缺 {missing[:3]}")
        check('loading="lazy"' in body, "首页 HTML 含 loading=\"lazy\"")

        section("B2 · 商品详情页")
        pid = re.search(r"/product/(P\d+)", body)
        if not check(bool(pid), "从首页拿到一个真实商品 id"):
            return
        pid = pid.group(1)
        st, pbody, _ = get(f"{base}/product/{pid}")
        dsrc = re.findall(r'<img[^>]*?\bsrc="([^"]+)"', pbody)
        check(st == 200 and dsrc, f"/product/{pid} 可打开且有图（{st}）", f"{dsrc[:1]}")
        check(all(s.startswith("/static/img/category/") for s in dsrc),
              "详情页图走类目静态目录", f"{dsrc[:1]}")

        section("B3 · 类目筛选后图片全对得上")
        # 线上路由的筛选参数是 cat（app.py: index(q, cat, page)），index.html
        # 里那个 ?category= 是旧路由留下的、模板已不再被渲染的参数名。
        st, fbody, _ = get(f"{base}/?cat=" + urllib.parse.quote("纸巾"))
        fsrcs = re.findall(r'<img[^>]*?\bsrc="([^"]+)"', fbody)
        check(st == 200 and bool(fsrcs), f"/?cat=纸巾 有结果（{len(fsrcs)} 张图）")
        check(bool(fsrcs) and all(s.endswith("/cat-zhijin.jpg") for s in fsrcs),
              "筛选后图片全部是 cat-zhijin.jpg",
              f"共 {len(fsrcs)} 张，异常 {[s for s in fsrcs if not s.endswith('cat-zhijin.jpg')][:3]}")
        st2, body2, _ = get(f"{base}/?category=" + urllib.parse.quote("纸巾"))
        stsrcs = re.findall(r'<img[^>]*?\bsrc="([^"]+)"', body2)
        print(f"     ℹ️ 顺带验一下 ?category=纸巾（index.html 里那个参数名）："
              f"HTTP {st2}，{len(stsrcs)} 张图，"
              f"{'已按类目筛选' if stsrcs and all(s.endswith('cat-zhijin.jpg') for s in stsrcs) else '未筛选（线上路由只认 cat 参数）'}")

        section("B4 · 图片能被 HTTP 取到")
        for fn in ("cat-shipin.jpg", "cat-zhijin.jpg"):
            code = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 f"{base}/static/img/category/{fn}"],
                capture_output=True, text=True).stdout.strip()
            check(code == "200", f"GET /static/img/category/{fn}", f"HTTP {code}")
        missing404 = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{base}/static/img/category/cat-not-exist.jpg"],
            capture_output=True, text=True).stdout.strip()
        check(missing404 == "404", "不存在的图片返回 404（不软 200 骗浏览器）",
              f"HTTP {missing404}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        if launcher.exists():
            launcher.unlink()
        print(f"     🧹 临时库与启动器已清理：{db} / {launcher.name}")


def test_merchant_portal() -> None:
    """商户门户模板：编译得过 + 没传 products 时是空态 + 传了才出图。

    这一段是被逼出来的：第一次写模板时把「{% if products %}」写进了 HTML 注释里，
    Jinja 不认 HTML 注释，照样解析 → 整页模板编译失败（首页/门户直接 500）。
    所以模板**必须**真编译一次，不能只做字符串检查。
    """
    section("C · 商户门户商品列表（merchant_portal.html）")
    try:
        from starlette.requests import Request
        from agentmall import db
        from agentmall.web import merchant_app as m
    except Exception as e:  # noqa: BLE001
        check(False, "import merchant_app", repr(e))
        return
    check("cat_url" in m.templates.env.filters, "merchant_app 已注册 cat_url 过滤器")
    try:
        tpl = m.templates.get_template("merchant_portal.html")
    except Exception as e:  # noqa: BLE001
        check(False, "merchant_portal.html 能编译（Jinja 标签闭合）", repr(e))
        return
    check(True, "merchant_portal.html 能编译（Jinja 标签闭合）")

    db_path = f"/tmp/am_catimg_portal_{os.getpid()}.db"
    os.environ["AGENTMALL_DB"] = db_path
    try:
        db.init_db()
        with db.connect() as conn:
            row = conn.execute("SELECT api_key FROM merchants WHERE id='M001'").fetchone()
        req = Request({"type": "http", "method": "GET", "path": "/", "headers": [
            (b"cookie", b"agentmall_merchant_key=" + (row["api_key"] or "").encode())],
            "query_string": b"", "server": ("127.0.0.1", 8003), "scheme": "http",
            "client": ("127.0.0.1", 1234), "root_path": ""})
        ctx = dict(request=req, base="", mcp_cfg={}, mcp_cfg_text="{}", mcp_endpoint="x",
                   n_products=0, n_orders=0, tools=[], msg="", level="ok",
                   merchant={"merchant_id": "M001", "name": "演示店",
                             "api_key": row["api_key"], "status": "active",
                             "created_at": 0})
        check(tpl.render(**ctx).count('class="m-img"') == 0,
              "门户路由还没传 products 时：空态不报错、不占版面")
        ctx["products"] = [{"name": "厨房纸", "category": "纸巾", "price": 19.9,
                            "id": "P001"},
                           {"name": "洗衣液", "category": "洗护", "price": 29.9,
                            "id": "P002"},
                           {"name": "火星特产", "category": "火星特产", "price": 1.0,
                            "id": "P003"}]
        html = tpl.render(**ctx)
        srcs = re.findall(r'class="m-img"[^>]*?src="([^"]+)"', html)
        check(len(srcs) == 3, f"传 products 时出 3 张图（实际 {len(srcs)}）")
        check(all(s.startswith("/static/img/category/") for s in srcs),
              "门户图全部落类目静态目录", f"{srcs}")
        check(srcs and srcs[-1].endswith("/cat-shipin.jpg"),
              "门户里未知类目也兜底到 cat-shipin.jpg", f"{srcs[-1] if srcs else '-'}")
    except Exception as e:  # noqa: BLE001
        check(False, "商户门户模板渲染", repr(e))
    finally:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)


def main() -> int:
    print("=" * 66)
    print("P0 类目图自检（issue #2）")
    print("=" * 66)
    test_mapping()
    test_live()
    test_merchant_portal()
    # 汇总行跟其他套件保持同款措辞（"N/M 项通过"），start_demo.sh 靠它抓摘要
    print(f"\n{'=' * 66}\n结果：{PASS}/{PASS + FAIL} 项通过（失败 {FAIL}）\n{'=' * 66}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())