"""收录对账端到端测试：本地静态站点 + 可编程 HTTP 服务。

运行方式：
    PYTHONPATH=/tmp/pylibs python3 -I tests/test_reconcile.py
"""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in __import__("sys").path:
        __import__("sys").path.insert(0, candidate)

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

from app import reconcile as rc  # noqa: E402

SITE_DIR = Path(__file__).resolve().parent.parent / "example-site" / "site"


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(f"{message}：期望为真，实际 {value!r}")
    print(f"  ✓ {message}")


def start_static() -> tuple[str, str]:
    """启动静态示例站，返回 (目录第一页URL, 站点根URL)。"""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    return f"{root}/list/page_1.html", root


def main() -> int:
    catalog, root = start_static()
    print(f"[1] 本地示例站点：{root}")

    print("[2] 目录 vs sitemap 索引（一层分片）对账 ...")
    report = rc.reconcile(catalog, f"{root}/sitemap.xml")
    assert_equal(len(report.catalog_links), 11, "目录侧去重后 11 个详情链接")
    assert_equal(len(report.sitemap_links), 11, "sitemap 侧去重后 11 个链接（片段已剥离）")
    assert_equal(len(report.common_links), 9, "两侧共有 9 个链接（notice_001~009）")
    assert_equal(
        sorted(u.rsplit("/", 1)[-1] for u in report.only_in_catalog),
        ["notice_010.html", "notice_011.html"],
        "仅目录有：notice_010、notice_011",
    )
    assert_equal(
        sorted(u.rsplit("/", 1)[-1] for u in report.only_in_sitemap),
        ["notice_012.html", "notice_013.html"],
        "仅 sitemap 有：notice_012、notice_013",
    )
    assert_equal(len(report.visited_list_pages), 2, "遍历并复读 2 个目录页")
    assert_equal(len(report.visited_sitemaps), 3, "读取并复读索引 + 2 个分片，共 3 个 sitemap 文件")
    assert_equal(report.errors, [], "无抓取/解析错误")
    assert_equal(report.recheck_changes, [], "复读后链接集合无变化")
    assert_true(report.reliable, "无异常线索，结论可信（可据独有链接判断收录差异）")

    print("[3] 直接 urlset 形态对账，结果应一致 ...")
    report_urlset = rc.reconcile(catalog, f"{root}/sitemap_all.xml")
    assert_equal(len(report_urlset.visited_sitemaps), 1, "urlset 形态只读取 1 个 sitemap 文件")
    assert_equal(report_urlset.sitemap_links, report.sitemap_links, "urlset 与索引分片结果一致")
    assert_equal(report_urlset.only_in_catalog, report.only_in_catalog, "目录独有链接一致")
    assert_equal(report_urlset.only_in_sitemap, report.only_in_sitemap, "sitemap 独有链接一致")
    assert_true(report_urlset.reliable, "urlset 形态结论同样可信")

    print("[4] URL 规范化：去片段、保留查询参数 ...")
    pages, shards = rc.parse_sitemap(
        '<?xml version="1.0"?><urlset xmlns="x">'
        "<url><loc>/detail/a.html?tab=1&amp;id=2#section</loc></url>"
        "<url><loc>/detail/a.html?tab=1&amp;id=2</loc></url>"
        "</urlset>",
        f"{root}/sitemap_all.xml",
    )
    assert_equal(pages, [f"{root}/detail/a.html?tab=1&id=2"], "片段被剥离、查询参数保留、重复 URL 去重")
    assert_equal(shards, [], "urlset 不产出分片")

    idx_pages, idx_shards = rc.parse_sitemap(
        '<?xml version="1.0"?><sitemapindex xmlns="x">'
        "<sitemap><loc>/sitemap_1.xml</loc></sitemap>"
        "<sitemap><loc>https://other.example.com/s2.xml</loc></sitemap>"
        "</sitemapindex>",
        f"{root}/sitemap.xml",
    )
    assert_equal(idx_pages, [], "索引形态不产出页面链接")
    assert_equal(
        idx_shards,
        [f"{root}/sitemap_1.xml", "https://other.example.com/s2.xml"],
        "相对分片转绝对地址，外部分片原样保留",
    )

    print("[5] 异常场景一律标记待核实，不断言漏登 ...")

    # 5a. sitemap 404 抓取失败
    bad = rc.reconcile(catalog, f"{root}/not-exist.xml")
    assert_true(len(bad.errors) > 0, "不存在的 sitemap 记录抓取错误")
    assert_true(not bad.reliable, "抓取失败时结论不可信")

    # 5b. sitemap 内容无法解析
    broken_dir = SITE_DIR.parent / "broken_site"
    broken_dir.mkdir(exist_ok=True)
    (broken_dir / "junk.xml").write_text("<html><body>不是 sitemap</body></html>", encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(broken_dir))
    broken_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=broken_server.serve_forever, daemon=True).start()
    broken_root = f"http://127.0.0.1:{broken_server.server_address[1]}"
    broken = rc.reconcile(catalog, f"{broken_root}/junk.xml")
    assert_true(any("解析失败" in e for e in broken.errors), "非 sitemap 内容记录解析失败")
    assert_true(not broken.reliable, "解析失败时结论不可信")

    # 5c. 越站：sitemap 与目录不同站点
    cross = rc.reconcile(catalog, f"{broken_root}/junk.xml")
    assert_true(
        any("不同站点" in reason for _url, reason in cross.skipped_urls),
        "跨站 sitemap 记录越站线索",
    )
    assert_true(not cross.reliable, "越站时结论不可信")

    # 5d. 触及采集上限
    original_limit = rc.MAX_LIST_PAGES
    rc.MAX_LIST_PAGES = 1
    try:
        limited = rc.reconcile(catalog, f"{root}/sitemap.xml")
    finally:
        rc.MAX_LIST_PAGES = original_limit
    assert_true(len(limited.limit_hits) > 0, "目录页上限被触发并留痕")
    assert_true(not limited.reliable, "触及采集上限时结论不可信")
    assert_true(len(limited.catalog_links) < 11, "上限内只采集到第一页的链接")

    print("[6] sitemap 索引嵌套超过一层 -> 不跟随并待核实 ...")
    nested_root = start_nested_index_server()
    nested = rc.reconcile(f"{nested_root}/list/p1.html", f"{nested_root}/sitemap.xml")
    assert_equal(
        len(nested.visited_sitemaps), 3,
        "读取根索引与两个第 1 层分片（ok.xml、level1.xml），嵌套的第 2 层不跟随",
    )
    assert_true(
        not any("level2.xml" in u for u in nested.visited_sitemaps),
        "第 2 层 urlset 未被读取",
    )
    assert_true(
        any("嵌套超过一层" in reason for _url, reason in nested.skipped_urls),
        "超层分片记为待核实线索",
    )
    assert_true(not nested.reliable, "嵌套超层时结论不可信")
    assert_equal(
        nested.sitemap_links, [f"{nested_root}/detail/ok.html"],
        "仅收录第 1 层 urlset 分片里的链接",
    )

    print("[7] 复读后集合变化 -> 待核实 ...")
    flip_root = start_flipping_server()
    flip = rc.reconcile(f"{flip_root}/list/page_1.html", f"{flip_root}/sitemap.xml")
    assert_true(len(flip.recheck_changes) > 0, "目录页复读变化被检出")
    assert_true(not flip.reliable, "复读集合变化时结论不可信")
    assert_true(
        all("变化" in c for c in flip.recheck_changes),
        "变化线索包含新增/减少明细",
    )

    print("\n全部对账测试通过 ✅")
    return 0


def start_flipping_server() -> str:
    """目录页首访与复读返回不同链接集合的服务，用于验证复读差异检测。"""

    class FlipHandler(http.server.BaseHTTPRequestHandler):
        call_count = 0

        def log_message(self, *args):  # 静音
            pass

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/list/"):
                FlipHandler.call_count += 1
                # 第 1 次（首访）只有 notice_001；第 2 次（复读）多出 notice_002
                extra = (
                    '<li><a href="/detail/notice_002.html">某项目更正公告内容足够长</a></li>'
                    if FlipHandler.call_count >= 2
                    else ""
                )
                body = (
                    "<html><body><ul class='notice-list'>"
                    "<li><a href='/detail/notice_001.html'>某项目招标公告内容足够长</a></li>"
                    f"{extra}</ul></body></html>"
                ).encode("utf-8")
            elif self.path.startswith("/sitemap.xml"):
                body = (
                    '<?xml version="1.0"?><urlset xmlns="x">'
                    "<url><loc>/detail/notice_001.html</loc></url></urlset>"
                ).encode("utf-8")
            elif self.path.startswith("/detail/"):
                body = b"<html><body>detail</body></html>"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlipHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def start_nested_index_server() -> str:
    """根索引 -> 第1层索引 -> 第2层 urlset 的三层结构，验证只下钻一层。"""

    pages = {
        "/list/p1.html": (
            "<html><body><ul>"
            "<li><a href='/detail/ok.html'>某项目招标公告内容足够长的标题文字</a></li>"
            "</ul></body></html>"
        ),
        # 第 0 层：索引，指向第 1 层（仍是索引）
        "/sitemap.xml": (
            '<?xml version="1.0"?><sitemapindex xmlns="x">'
            "<sitemap><loc>/shards/level1.xml</loc></sitemap></sitemapindex>"
        ),
        # 第 1 层：又是索引（嵌套超层），同时夹带一个正常 urlset 不会发生；
        # 这里它是索引，其指向的第 2 层不应被跟随
        "/shards/level1.xml": (
            '<?xml version="1.0"?><sitemapindex xmlns="x">'
            "<sitemap><loc>/shards/level2.xml</loc></sitemap></sitemapindex>"
        ),
        # 第 2 层：urlset，即使存在也不应被读取
        "/shards/level2.xml": (
            '<?xml version="1.0"?><urlset xmlns="x">'
            "<url><loc>/detail/should_not_follow.html</loc></url></urlset>"
        ),
        # 与目录共有的正常链接，经另一个合法的第 1 层 urlset 分片收录
        "/shards/ok.xml": (
            '<?xml version="1.0"?><urlset xmlns="x">'
            "<url><loc>/detail/ok.html</loc></url></urlset>"
        ),
    }

    class NestedHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path in ("/sitemap.xml", "/shards/level1.xml", "/shards/ok.xml"):
                # 根索引同时挂一个合法 urlset 分片和一个嵌套索引分片
                if self.path == "/sitemap.xml":
                    body = (
                        '<?xml version="1.0"?><sitemapindex xmlns="x">'
                        "<sitemap><loc>/shards/ok.xml</loc></sitemap>"
                        "<sitemap><loc>/shards/level1.xml</loc></sitemap>"
                        "</sitemapindex>"
                    ).encode()
                else:
                    body = pages[self.path].encode()
            else:
                body = pages.get(self.path, "<html><body>x</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NestedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    raise SystemExit(main())
