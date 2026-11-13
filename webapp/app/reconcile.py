"""收录对账：比对"分页目录"与"公告 sitemap"两侧收录的详情链接。

口径（需求）：
- 从目录入口逐页跟随"下一页"，提取每页的详情链接；
- sitemap 支持 urlset，或一层 sitemap 索引（索引中的分片再读一次，不继续嵌套）；
- 按去掉片段（fragment）的完整 URL 去重，保留查询参数；
- 采集结束后"复读"已访问的目录页与已读取的 sitemap 文件，重新抽取链接集合；
- 抓取失败、解析失败、越站链接、触及采集上限、复读后集合变化等任何异常，
  都会使结果标记为"待核实"：页面只展示两侧独有链接线索，不断言某侧漏登。
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.builder import XMLParsedAsHTMLWarning

from .crawler import (
    MAX_DETAIL_PAGES,
    MAX_LIST_PAGES,
    REQUEST_TIMEOUT,
    USER_AGENT,
    _extract_list_links,
)
from .utils import same_origin

# sitemap 自身的采集上限：最多读取的 sitemap 文件数（含根与分片）、单文件最多收录的链接数
MAX_SITEMAP_FILES = 20
MAX_SITEMAP_URLS = 1000


@dataclass
class ReconcileReport:
    """一次收录对账的完整结果。"""

    catalog_url: str = ""
    sitemap_url: str = ""
    # 两侧各自去重后的链接集合
    catalog_links: list[str] = field(default_factory=list)
    sitemap_links: list[str] = field(default_factory=list)
    # 集合差集：只在一侧出现的链接
    only_in_catalog: list[str] = field(default_factory=list)
    only_in_sitemap: list[str] = field(default_factory=list)
    common_links: list[str] = field(default_factory=list)
    # 过程留痕
    visited_list_pages: list[str] = field(default_factory=list)
    visited_sitemaps: list[str] = field(default_factory=list)
    skipped_urls: list[tuple[str, str]] = field(default_factory=list)  # (url, 原因)
    errors: list[str] = field(default_factory=list)
    recheck_changes: list[str] = field(default_factory=list)          # 复读发现的集合变化
    limit_hits: list[str] = field(default_factory=list)                # 触及的采集上限
    input_valid: bool = True

    @property
    def reliable(self) -> bool:
        """无任何异常线索时才允许据此判断漏登；否则只能标记待核实。"""
        return (
            self.input_valid
            and not self.errors
            and not self.skipped_urls
            and not self.recheck_changes
            and not self.limit_hits
        )


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def normalize_link(base: str, href: str) -> str | None:
    """相对地址转绝对地址、去掉片段、保留查询参数；非 http(s) 返回 None。"""
    if not href:
        return None
    url = urldefrag(urljoin(base, href))[0].strip()
    if not _is_http_url(url):
        return None
    return url


def parse_sitemap(xml_text: str, base_url: str) -> tuple[list[str], list[str]]:
    """解析 sitemap 文本。

    返回 ``(页面链接, 分片链接)``：
    - 根/分片是 ``<sitemapindex>`` 时，页面链接为空、分片链接为各 ``<sitemap><loc>``；
    - 是 ``<urlset>`` 时，页面链接为各 ``<url><loc>``、分片链接为空。

    两者都识别不出来时抛出 ValueError，交由调用方记为解析失败。
    """
    with warnings.catch_warnings():
        # 未引入 lxml，统一用内置 html.parser 解析；无命名空间标签名可正常匹配
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(xml_text, "html.parser")
    # 命名空间下标签名可能带前缀（如 sitemap:sitemap），用结尾匹配兼容
    is_index = soup.find(lambda tag: tag.name and tag.name.endswith("sitemapindex")) is not None
    is_urlset = soup.find(lambda tag: tag.name and tag.name.endswith("urlset")) is not None

    if is_index:
        locs = [
            loc.get_text("", strip=True)
            for sm in soup.find_all(lambda t: t.name and t.name.endswith("sitemap"))
            for loc in sm.find_all(lambda t: t.name and t.name.endswith("loc"))
        ]
        shards: list[str] = []
        for raw in locs:
            url = normalize_link(base_url, raw)
            if url:
                shards.append(url)
        # 去重保序
        return [], _dedupe(shards)

    if is_urlset:
        locs = [
            loc.get_text("", strip=True)
            for u in soup.find_all(lambda t: t.name and t.name.endswith("url"))
            for loc in u.find_all(lambda t: t.name and t.name.endswith("loc"))
        ]
        pages: list[str] = []
        for raw in locs:
            url = normalize_link(base_url, raw)
            if url:
                pages.append(url)
        return _dedupe(pages), []

    raise ValueError(f"既不是 sitemapindex 也不是 urlset：{base_url}")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _crawl_catalog(client: httpx.Client, entry_url: str, report: ReconcileReport) -> set[str]:
    """沿同站"下一页"遍历目录，提取去重后的详情链接，并把异常记入 report。"""
    queue: list[str] = [entry_url]
    seen_lists: set[str] = set()
    detail_links: set[str] = set()
    # 记录每个目录页首访抽取结果，供复读比对：最终URL -> 链接集合
    first_pass: dict[str, set[str]] = {}

    while queue:
        if len(seen_lists) >= MAX_LIST_PAGES:
            report.limit_hits.append(f"目录页数达到上限 {MAX_LIST_PAGES} 页，剩余分页未采集")
            break
        list_url = queue.pop(0)
        if list_url in seen_lists:
            continue
        seen_lists.add(list_url)

        try:
            resp = client.get(list_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            report.errors.append(f"目录页抓取失败 {list_url}：{exc}")
            continue

        final_url = str(resp.url)
        report.visited_list_pages.append(final_url)
        page_links, next_page = _extract_list_links(resp.text, final_url)

        kept: set[str] = set()
        for link in page_links:
            if not same_origin(entry_url, link):
                report.skipped_urls.append((link, "目录中的越站详情链接，已跳过"))
                continue
            if len(detail_links) >= MAX_DETAIL_PAGES and link not in detail_links:
                report.skipped_urls.append((link, f"详情链接达到上限 {MAX_DETAIL_PAGES} 个，未收录"))
                report.limit_hits.append(f"目录详情链接达到上限 {MAX_DETAIL_PAGES} 个")
                continue
            detail_links.add(link)
            kept.add(link)
        first_pass[final_url] = kept

        if next_page:
            if not same_origin(entry_url, next_page):
                report.skipped_urls.append((next_page, "下一页指向不同站点，已停止跟随"))
            elif next_page not in seen_lists and next_page not in queue:
                queue.append(next_page)

    # 复读：重新抓取已访问目录页，比对链接集合
    for page_url, first_links in first_pass.items():
        try:
            resp = client.get(page_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            report.errors.append(f"目录页复读抓取失败 {page_url}：{exc}")
            continue
        re_links, _ = _extract_list_links(resp.text, str(resp.url))
        re_set = {link for link in re_links if same_origin(entry_url, link)}
        if re_set != first_links:
            report.recheck_changes.append(
                f"目录页 {page_url} 复读后链接集合变化"
                f"（新增 {sorted(re_set - first_links)}，减少 {sorted(first_links - re_set)}）"
            )

    return detail_links


def _crawl_sitemap(client: httpx.Client, root_url: str, report: ReconcileReport) -> set[str]:
    """读取 sitemap（urlset 或一层索引分片），返回去重后的页面链接。

    深度口径：根文件为第 0 层；根是 sitemapindex 时只下钻到第 1 层分片。
    第 1 层分片若仍是索引（嵌套超过一层），其分片不跟随，只记待核实线索。
    """
    page_links: set[str] = set()
    # 每个 sitemap 文件首访抽取结果：最终URL -> (页面集合, 分片集合)
    first_pass: dict[str, tuple[set[str], set[str]]] = {}
    fetched: set[str] = set()
    queue: list[tuple[str, int]] = [(root_url, 0)]

    while queue:
        sitemap_url, depth = queue.pop(0)
        if sitemap_url in fetched:
            continue
        if len(fetched) >= MAX_SITEMAP_FILES:
            report.limit_hits.append(f"sitemap 文件数达到上限 {MAX_SITEMAP_FILES} 个，剩余分片未读取")
            break
        fetched.add(sitemap_url)

        try:
            resp = client.get(sitemap_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            report.errors.append(f"sitemap 抓取失败 {sitemap_url}：{exc}")
            continue

        final_url = str(resp.url)
        report.visited_sitemaps.append(final_url)
        try:
            pages, shards = parse_sitemap(resp.text, final_url)
        except ValueError as exc:
            report.errors.append(f"sitemap 解析失败 {final_url}：{exc}")
            continue

        page_set, shard_set = set(pages), set(shards)
        first_pass[final_url] = (page_set, shard_set)

        for link in page_set:
            if len(page_links) >= MAX_SITEMAP_URLS and link not in page_links:
                report.skipped_urls.append((link, f"sitemap 链接达到上限 {MAX_SITEMAP_URLS} 个，未收录"))
                report.limit_hits.append(f"sitemap 链接达到上限 {MAX_SITEMAP_URLS} 个")
                continue
            page_links.add(link)

        if shard_set and depth >= 1:
            # 一层索引分片仍是索引 -> 嵌套超层，不继续下钻，仅留待核实线索
            for shard in sorted(shard_set):
                report.skipped_urls.append((shard, "sitemap 索引嵌套超过一层，未跟随该分片"))
            continue

        for shard in shard_set:  # depth == 0 的根索引，分片进入第 1 层
            if shard not in fetched and shard not in {u for u, _d in queue}:
                queue.append((shard, depth + 1))

    # 复读：重新读取每个 sitemap 文件并比对
    for sitemap_url, (first_pages, first_shards) in first_pass.items():
        try:
            resp = client.get(sitemap_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            report.errors.append(f"sitemap 复读抓取失败 {sitemap_url}：{exc}")
            continue
        try:
            re_pages, re_shards = parse_sitemap(resp.text, str(resp.url))
        except ValueError as exc:
            report.errors.append(f"sitemap 复读解析失败 {sitemap_url}：{exc}")
            continue
        re_page_set, re_shard_set = set(re_pages), set(re_shards)
        if re_page_set != first_pages:
            report.recheck_changes.append(
                f"sitemap {sitemap_url} 复读后页面链接集合变化"
                f"（新增 {sorted(re_page_set - first_pages)}，减少 {sorted(first_pages - re_page_set)}）"
            )
        if re_shard_set != first_shards:
            report.recheck_changes.append(
                f"sitemap {sitemap_url} 复读后分片集合变化"
                f"（新增 {sorted(re_shard_set - first_shards)}，减少 {sorted(first_shards - re_shard_set)}）"
            )

    return page_links


def reconcile(catalog_url: str, sitemap_url: str) -> ReconcileReport:
    """对账入口：输入同站目录入口与 sitemap 地址，返回 ReconcileReport。"""
    report = ReconcileReport(
        catalog_url=(catalog_url or "").strip(),
        sitemap_url=(sitemap_url or "").strip(),
    )

    if not _is_http_url(report.catalog_url) or not _is_http_url(report.sitemap_url):
        report.errors.append("目录地址与 sitemap 地址都必须是 http(s) 开头的完整 URL。")
        report.input_valid = False
        return report
    if not same_origin(report.catalog_url, report.sitemap_url):
        report.skipped_urls.append(
            (report.sitemap_url, "sitemap 与目录入口不同站点（协议/主机/端口不一致），结果仅作线索")
        )

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        catalog_set = _crawl_catalog(client, report.catalog_url, report)
        sitemap_set = _crawl_sitemap(client, report.sitemap_url, report)

    # sitemap 中的越站链接同样只作线索
    catalog_origin = urlparse(report.catalog_url)
    for link in sorted(sitemap_set):
        parsed = urlparse(link)
        if (parsed.scheme, parsed.netloc) != (catalog_origin.scheme, catalog_origin.netloc):
            report.skipped_urls.append((link, "sitemap 中的越站链接，已标记待核实"))

    report.catalog_links = sorted(catalog_set)
    report.sitemap_links = sorted(sitemap_set)
    report.common_links = sorted(catalog_set & sitemap_set)
    report.only_in_catalog = sorted(catalog_set - sitemap_set)
    report.only_in_sitemap = sorted(sitemap_set - catalog_set)
    return report
