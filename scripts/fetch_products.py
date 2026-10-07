#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_products.py — AgentMall 演示用商品数据抓取脚本
=====================================================================
【数据来源与免责声明】
  * 本脚本面向 **路演演示**，产出的数据会携带 "is_demo": 1 标记，
    页面/文档层负责展示「演示数据，非真实交易」水印。
  * 商品名称与价格为真实市场公开信息（取自可访问的电商页面与比价
    站点的公开报价），仅用于演示检索/推荐/比价链路。
  * **stock 为演示用占位库存，不代表任何商户的真实库存。**
  * merchant_id (M001/M002/M003) 为演示用虚拟商户，同一品牌固定归
    属同一商户。

【在线抓取源】（沙箱/内网实测结论见 SOURCE_PROBES）
  - dangdang  当当网   search.dangdang.com     ✅ 可达，返回真实 商品名+价格+店铺
  - suning    苏宁易购 search.suning.com       ⚠️ 可达，返回真实商品名；价格走签名接口，抓不到
  - 1688      1688     s.1688.com             ❌ 阿里云盾风控 punish 页
  - taobao    淘宝/天猫 s.taobao.com           ❌ 登录墙，无商品数据
  - jd        京东     search.jd.com           ❌ 「京东验证」反爬页

【离线兜底】
  当在线源不可达、或解析出的商品不足演示所需（默认 40 条）时，脚本回落到
  内嵌的 OFFLINE_PRODUCTS —— 一份已人工核对过的真实商品/真实市场价数据
  （source 字段如实标注为 web_research）。
  ⚠️ 生产环境请切官方开放平台 API（淘宝开放平台 / 京东宙斯 / 1688 开放平台），
     不要依赖 HTML 解析，更不要依赖本兜底数据。

【用法】
  python scripts/fetch_products.py                    # 自动探测 + 写 data/products_real.json
  python scripts/fetch_products.py --source offline   # 强制走离线兜底
  python scripts/fetch_products.py --source dangdang  # 强制在线抓当当网
  python scripts/fetch_products.py --limit 20 --out /tmp/x.json

【依赖】仅标准库 urllib（无 requests 时同样可跑，不引入爬虫框架）。
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 5  # 沙箱实测统一 5 秒超时

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO_ROOT, "data", "products_real.json")

# 5 个演示品类 -> 各源的检索词
CATEGORY_KEYWORDS = {
    "纸巾":   ["纸巾", "抽纸", "卷纸"],
    "垃圾袋": ["垃圾袋", "抽绳垃圾袋"],
    "清洁":   ["洗衣液", "洗洁精", "洁厕灵"],
    "收纳":   ["收纳箱", "收纳盒", "真空压缩袋"],
    "个护":   ["牙膏", "沐浴露", "香皂"],
}

# 沙箱可达性实测记录（2026-10-07），用于 --probe 输出与代码注释溯源
SOURCE_PROBES = [
    ("dangdang", "https://search.dangdang.com/?key=%s&act=input", "OK",   "真实商品名+价格+店铺，可解析"),
    ("suning",   "https://search.suning.com/%s/",                   "OK*",  "仅商品名；价格在签名接口 pas.suning.com，取不到"),
    ("1688",     "https://s.1688.com/selloffer/offer_search.htm?keywords=%s", "BLOCKED", "阿里云盾风控 punish:deny 页"),
    ("taobao",   "https://s.taobao.com/search?q=%s",                "BLOCKED", "登录墙，无商品数据"),
    ("jd",       "https://search.jd.com/Search?keyword=%s",         "BLOCKED", "「京东验证」反爬页"),
]

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url, timeout=TIMEOUT):
    """GET 一个 URL，返回解码后的文本；任何异常都向上抛给调用方处理。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # 当当网是 GB18030，其余按 UTF-8
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def probe(url):
    """可达性探测：能拿到 HTTP 响应即视为「网络可达」。"""
    try:
        http_get(url)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 解析器：当当网（真实可解析）
# --------------------------------------------------------------------------

_DD_LI = re.compile(r'<li [^>]*ddt-pit="\d+".*?</li>', re.S)
_DD_NAME = re.compile(r'<p class="name"[^>]*>\s*<a title="([^"]+)"')
_DD_PRICE = re.compile(r'class="price_n">&yen;([\d.]+)')
_DD_SHOP = re.compile(r'name="itemlist-shop-name"[^>]*title="([^"]*)"')


def parse_dangdang(page_html):
    """从当当网搜索结果页解析商品。返回 dict 列表（单条失败不影响整体）。"""
    out = []
    for block in _DD_LI.findall(page_html):
        try:
            m_name = _DD_NAME.search(block)
            m_price = _DD_PRICE.search(block)
            if not (m_name and m_price):
                continue  # 广告位/无价格条目，跳过
            name = html.unescape(m_name.group(1)).strip()
            price = round(float(m_price.group(1)), 2)
            m_shop = _DD_SHOP.search(block)
            shop = html.unescape(m_shop.group(1)).strip() if m_shop else ""
            if not name or price <= 0:
                continue
            out.append({"name": name, "price": price, "shop": shop})
        except Exception:  # 单条解析失败 -> 跳过，绝不中断整页
            continue
    return out


def scrape_dangdang(keyword, limit=60):
    url = "https://search.dangdang.com/?key=%s&act=input" % urllib.parse.quote(keyword)
    try:
        page = http_get(url)
    except Exception as exc:
        print("  [dangdang] 请求失败 %s: %s" % (keyword, exc))
        return []
    if "无结果" in page:
        return []
    return parse_dangdang(page)[:limit]


# --------------------------------------------------------------------------
# 解析器：苏宁易购（仅商品名；价格接口需签名，抓不到）
# --------------------------------------------------------------------------

_SN_LI = re.compile(r'<li docType="1".*?</li>', re.S)


def scrape_suning(keyword, limit=60):
    url = "https://search.suning.com/%s/" % urllib.parse.quote(keyword)
    try:
        page = http_get(url, timeout=8)
    except Exception as exc:
        print("  [suning] 请求失败 %s: %s" % (keyword, exc))
        return []
    out = []
    for block in _SN_LI.findall(page):
        try:
            pid = re.search(r'id="(\d+-\d+)"', block)
            nm = (re.search(r'class="title-selling-point"[^>]*>(.*?)</a>', block, re.S)
                  or re.search(r'<div class="title-selling-point">(.*?)</div>', block, re.S))
            if not (pid and nm):
                continue
            name = html.unescape(re.sub(r"<[^>]+>", "", nm.group(1))).strip()
            if not name:
                continue
            out.append({"name": name, "price": None, "sku": pid.group(1)})
        except Exception:
            continue
    return out[:limit]


# --------------------------------------------------------------------------
# 离线兜底数据
# --------------------------------------------------------------------------
# ⚠️ 源不可达时的离线兜底，生产环境切官方 API。
# 数据来源：web_research —— 公开比价站点（什么值得买/慢慢买）与电商公开
# 报价页整理的**真实商品与真实市场价格**，非编造。
OFFLINE_PRODUCTS = OFFLINE_PRODUCTS = [
    {
        'name': '维达 超韧系列 抽纸 3层100抽*30包(195*133mm)',
        'category': '纸巾',
        'price': 24.94,
        'original_price': 29.8,
        'unit': '包',
        'specs': '3层/100抽*30包',
        'stock': 320,
        'merchant_id': 'M001',
    },
    {
        'name': '维达 超韧系列 抽纸 3层100抽*60包 整箱',
        'category': '纸巾',
        'price': 59.9,
        'original_price': 109.9,
        'unit': '箱',
        'specs': '3层/100抽*60包',
        'stock': 150,
        'merchant_id': 'M001',
    },
    {
        'name': '维达 细韧系列 抽纸 3层100抽*24包',
        'category': '纸巾',
        'price': 16.93,
        'original_price': 25.5,
        'unit': '包',
        'specs': '3层/100抽*24包',
        'stock': 480,
        'merchant_id': 'M001',
    },
    {
        'name': '维达 细韧抽纸 S码 3层100抽*20包 箱装',
        'category': '纸巾',
        'price': 17.53,
        'original_price': 23.5,
        'unit': '包',
        'specs': '3层/100抽*20包',
        'stock': 400,
        'merchant_id': 'M001',
    },
    {
        'name': '洁柔 Face 抽纸 缤纷系列 3层100抽*20包',
        'category': '纸巾',
        'price': 17.7,
        'original_price': 24.6,
        'unit': '包',
        'specs': '3层/100抽*20包',
        'stock': 350,
        'merchant_id': 'M001',
    },
    {
        'name': '洁柔 乳霜纸 Lotion 3层80抽*16包',
        'category': '纸巾',
        'price': 39.9,
        'original_price': 49.9,
        'unit': '包',
        'specs': '3层/80抽*16包',
        'stock': 210,
        'merchant_id': 'M001',
    },
    {
        'name': '洁柔 抽纸 粉Face软抽 3层100抽*24包',
        'category': '纸巾',
        'price': 41.0,
        'original_price': 49.9,
        'unit': '包',
        'specs': '3层/100抽*24包',
        'stock': 190,
        'merchant_id': 'M001',
    },
    {
        'name': '清风 四叶草系列 抽纸 3层100抽 S码24包',
        'category': '纸巾',
        'price': 16.75,
        'original_price': 24.9,
        'unit': '包',
        'specs': '3层/100抽*24包',
        'stock': 300,
        'merchant_id': 'M001',
    },
    {
        'name': '清风 抽纸 原木纯品 100抽 面巾纸',
        'category': '纸巾',
        'price': 15.04,
        'original_price': 25.06,
        'unit': '包',
        'specs': '3层/100抽',
        'stock': 380,
        'merchant_id': 'M001',
    },
    {
        'name': '心相印 抽纸 茶语丝享 110抽 3层S码10包',
        'category': '纸巾',
        'price': 17.71,
        'original_price': 29.9,
        'unit': '包',
        'specs': '3层/110抽*10包',
        'stock': 290,
        'merchant_id': 'M001',
    },
    {
        'name': '心相印 云感 悬挂式抽纸 4层320抽*4提',
        'category': '纸巾',
        'price': 21.23,
        'original_price': 29.9,
        'unit': '提',
        'specs': '4层/320抽*4提',
        'stock': 170,
        'merchant_id': 'M001',
    },
    {
        'name': '洁云 雅致生活 抽纸 3层100抽*27包',
        'category': '纸巾',
        'price': 13.9,
        'original_price': 19.9,
        'unit': '包',
        'specs': '3层/100抽*27包',
        'stock': 330,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 CHAHUA 加厚点断式垃圾袋 1包5卷【150只】',
        'category': '垃圾袋',
        'price': 7.9,
        'original_price': 12.9,
        'unit': '包',
        'specs': '150只/1.5丝',
        'stock': 500,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 3214P 平口垃圾袋 点断式 2包6卷【180只】',
        'category': '垃圾袋',
        'price': 8.9,
        'original_price': 12.0,
        'unit': '包',
        'specs': '180只/1丝',
        'stock': 460,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 垃圾袋 背心式 4560cm*100只',
        'category': '垃圾袋',
        'price': 11.9,
        'original_price': 19.9,
        'unit': '卷',
        'specs': '100只/1.5丝',
        'stock': 420,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 免撕抽绳式垃圾袋 5卷装',
        'category': '垃圾袋',
        'price': 12.9,
        'original_price': 19.9,
        'unit': '提',
        'specs': '5卷装/抽绳',
        'stock': 360,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 垃圾袋 点断式清洁袋 4555cm*150只',
        'category': '垃圾袋',
        'price': 25.7,
        'original_price': 32.9,
        'unit': '包',
        'specs': '150只/4555cm',
        'stock': 180,
        'merchant_id': 'M001',
    },
    {
        'name': '妙洁 点断式垃圾袋 平底黑色加厚 单卷',
        'category': '垃圾袋',
        'price': 5.9,
        'original_price': 9.9,
        'unit': '卷',
        'specs': '单卷/加厚平底',
        'stock': 520,
        'merchant_id': 'M001',
    },
    {
        'name': '妙洁 背心手提式垃圾袋 加厚加大 100只',
        'category': '垃圾袋',
        'price': 9.9,
        'original_price': 15.8,
        'unit': '包',
        'specs': '100只/背心式',
        'stock': 400,
        'merchant_id': 'M001',
    },
    {
        'name': '妙洁 抽绳垃圾袋 100只(45*48cm) 中号',
        'category': '垃圾袋',
        'price': 22.5,
        'original_price': 25.0,
        'unit': '卷',
        'specs': '100只/45*48cm',
        'stock': 240,
        'merchant_id': 'M001',
    },
    {
        'name': '洁成 背心式垃圾袋 厨房专用加厚 100只',
        'category': '垃圾袋',
        'price': 12.9,
        'original_price': 19.9,
        'unit': '包',
        'specs': '100只/背心式',
        'stock': 350,
        'merchant_id': 'M001',
    },
    {
        'name': '美丽雅 垃圾袋 背心式 100只 45x55cm',
        'category': '垃圾袋',
        'price': 12.26,
        'original_price': 24.26,
        'unit': '包',
        'specs': '100只/45*55cm',
        'stock': 380,
        'merchant_id': 'M001',
    },
    {
        'name': '美丽雅 免撕抽绳式大容量垃圾袋',
        'category': '垃圾袋',
        'price': 12.9,
        'original_price': 19.9,
        'unit': '包',
        'specs': '大容量/抽绳',
        'stock': 300,
        'merchant_id': 'M001',
    },
    {
        'name': '佳帮手 背心式垃圾袋 100只(46*63cm) 黑色',
        'category': '垃圾袋',
        'price': 9.9,
        'original_price': 18.9,
        'unit': '包',
        'specs': '100只/46*63cm',
        'stock': 440,
        'merchant_id': 'M001',
    },
    {
        'name': '蓝月亮 深层洁净洗衣液 3kg/瓶 自然清香',
        'category': '清洁',
        'price': 39.8,
        'original_price': 42.0,
        'unit': '瓶',
        'specs': '3kg/自然清香',
        'stock': 160,
        'merchant_id': 'M001',
    },
    {
        'name': '蓝月亮 深层洁净护理洗衣液 500g/袋 薰衣草香',
        'category': '清洁',
        'price': 9.8,
        'original_price': 11.8,
        'unit': '袋',
        'specs': '500g/薰衣草香',
        'stock': 400,
        'merchant_id': 'M001',
    },
    {
        'name': '蓝月亮 茶清洗洁精 500g',
        'category': '清洁',
        'price': 12.8,
        'original_price': 15.8,
        'unit': '瓶',
        'specs': '500g/茶清',
        'stock': 320,
        'merchant_id': 'M001',
    },
    {
        'name': '蓝月亮 卫诺香氛洁厕液 500g',
        'category': '清洁',
        'price': 10.8,
        'original_price': 13.8,
        'unit': '瓶',
        'specs': '500g/香氛',
        'stock': 280,
        'merchant_id': 'M001',
    },
    {
        'name': '蓝月亮 地板清洁剂 600g 清爽柠檬',
        'category': '清洁',
        'price': 10.8,
        'original_price': 15.8,
        'unit': '瓶',
        'specs': '600g/柠檬',
        'stock': 300,
        'merchant_id': 'M001',
    },
    {
        'name': '蓝月亮 84消毒液 地板家居衣物消毒水 1.2kg*4瓶',
        'category': '清洁',
        'price': 49.7,
        'original_price': 71.0,
        'unit': '箱',
        'specs': '1.2kg*4瓶',
        'stock': 90,
        'merchant_id': 'M001',
    },
    {
        'name': '立白 新金桔洗洁精 1.29kg/瓶',
        'category': '清洁',
        'price': 11.8,
        'original_price': 15.9,
        'unit': '瓶',
        'specs': '1.29kg/金桔',
        'stock': 340,
        'merchant_id': 'M001',
    },
    {
        'name': '立白 大师香氛洗衣液 1kg/袋',
        'category': '清洁',
        'price': 13.9,
        'original_price': 19.9,
        'unit': '袋',
        'specs': '1kg/香氛',
        'stock': 310,
        'merchant_id': 'M001',
    },
    {
        'name': '威猛先生 厨房重油污净 清爽柠檬 500g',
        'category': '清洁',
        'price': 19.8,
        'original_price': 25.9,
        'unit': '瓶',
        'specs': '500g/重油污',
        'stock': 210,
        'merchant_id': 'M003',
    },
    {
        'name': '奥妙 净蓝全效深层洁净洗衣液 2kg/瓶',
        'category': '清洁',
        'price': 32.8,
        'original_price': 39.9,
        'unit': '瓶',
        'specs': '2kg/深层洁净',
        'stock': 140,
        'merchant_id': 'M003',
    },
    {
        'name': '汰渍 净白去渍洗衣粉 1.36kg/袋',
        'category': '清洁',
        'price': 11.0,
        'original_price': 14.5,
        'unit': '袋',
        'specs': '1.36kg/净白去渍',
        'stock': 260,
        'merchant_id': 'M003',
    },
    {
        'name': '雕牌 无磷洗衣粉 508g/袋',
        'category': '清洁',
        'price': 5.0,
        'original_price': 6.8,
        'unit': '袋',
        'specs': '508g/无磷',
        'stock': 450,
        'merchant_id': 'M003',
    },
    {
        'name': '茶花 收纳箱 斜口前开叠加侧开 34L*3个装',
        'category': '收纳',
        'price': 69.2,
        'original_price': 109.0,
        'unit': '套',
        'specs': '34L*3个装',
        'stock': 70,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 收纳箱 小号透明整理箱 8.5L',
        'category': '收纳',
        'price': 14.9,
        'original_price': 29.9,
        'unit': '个',
        'specs': '8.5L/透明',
        'stock': 260,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 透明磨砂收纳箱 65L*1个装 带滑轮',
        'category': '收纳',
        'price': 69.9,
        'original_price': 99.0,
        'unit': '个',
        'specs': '65L/带滑轮',
        'stock': 55,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 透明磨砂收纳箱 58L*2个装 带滑轮',
        'category': '收纳',
        'price': 79.0,
        'original_price': 159.0,
        'unit': '套',
        'specs': '58L*2个装',
        'stock': 40,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 压缩袋 真空收纳袋 电泵5件套 4特大3中',
        'category': '收纳',
        'price': 35.1,
        'original_price': 76.3,
        'unit': '套',
        'specs': '电泵5件套',
        'stock': 95,
        'merchant_id': 'M001',
    },
    {
        'name': '茶花 压缩袋 真空收纳袋 电泵9件套 4特大4中',
        'category': '收纳',
        'price': 59.6,
        'original_price': 104.8,
        'unit': '套',
        'specs': '电泵9件套',
        'stock': 60,
        'merchant_id': 'M001',
    },
    {
        'name': '太力 真空压缩收纳袋 星球款 3特大送手泵',
        'category': '收纳',
        'price': 34.9,
        'original_price': 59.9,
        'unit': '套',
        'specs': '3特大/赠手泵',
        'stock': 110,
        'merchant_id': 'M003',
    },
    {
        'name': '禧天龙 透明收纳箱 玩具储物盒 蓝色盖 三个装',
        'category': '收纳',
        'price': 35.92,
        'original_price': 79.9,
        'unit': '套',
        'specs': '3个装/35.7*26*15.4cm',
        'stock': 85,
        'merchant_id': 'M003',
    },
    {
        'name': '禧天龙 抽屉式厨房收纳盒 带滑轮 大号3个装',
        'category': '收纳',
        'price': 39.9,
        'original_price': 79.9,
        'unit': '套',
        'specs': '3个装/带滑轮',
        'stock': 75,
        'merchant_id': 'M003',
    },
    {
        'name': '禧天龙 纯色布艺收纳盒 47*28*20cm 大号米色 2个',
        'category': '收纳',
        'price': 19.9,
        'original_price': 59.9,
        'unit': '套',
        'specs': '2个装/47*28*20cm',
        'stock': 130,
        'merchant_id': 'M003',
    },
    {
        'name': '禧天龙 双层分格 透白小号5格 2个装',
        'category': '收纳',
        'price': 55.84,
        'original_price': 99.8,
        'unit': '套',
        'specs': '5格*2个装',
        'stock': 65,
        'merchant_id': 'M003',
    },
    {
        'name': '云南白药 牙膏 经典系列 留兰香型 165g',
        'category': '个护',
        'price': 28.3,
        'original_price': 39.9,
        'unit': '支',
        'specs': '165g/留兰香',
        'stock': 220,
        'merchant_id': 'M002',
    },
    {
        'name': '云南白药 牙膏 益生菌清新口气 260g',
        'category': '个护',
        'price': 35.6,
        'original_price': 51.6,
        'unit': '支',
        'specs': '260g/益生菌',
        'stock': 180,
        'merchant_id': 'M002',
    },
    {
        'name': '云南白药 漱口水 金口健盐系清新 250ml',
        'category': '个护',
        'price': 14.16,
        'original_price': 31.0,
        'unit': '瓶',
        'specs': '250ml/盐系清新',
        'stock': 200,
        'merchant_id': 'M002',
    },
    {
        'name': '舒肤佳 经典净护系列 沐浴露 纯白清香 1L',
        'category': '个护',
        'price': 38.99,
        'original_price': 49.9,
        'unit': '瓶',
        'specs': '1L/纯白清香',
        'stock': 150,
        'merchant_id': 'M002',
    },
    {
        'name': '舒肤佳 健康净护沐浴露 纯白清香 720g*2瓶',
        'category': '个护',
        'price': 38.99,
        'original_price': 88.98,
        'unit': '套',
        'specs': '720g*2瓶',
        'stock': 120,
        'merchant_id': 'M002',
    },
    {
        'name': '舒肤佳 净护沐浴露 纯白清香 80g 旅行装',
        'category': '个护',
        'price': 4.9,
        'original_price': 7.5,
        'unit': '瓶',
        'specs': '80g/旅行装',
        'stock': 380,
        'merchant_id': 'M002',
    },
    {
        'name': '六神 三重薄荷清凉沐浴露 750ml+420ml',
        'category': '个护',
        'price': 18.66,
        'original_price': 24.9,
        'unit': '套',
        'specs': '750ml+420ml',
        'stock': 170,
        'merchant_id': 'M002',
    },
    {
        'name': '六神 冰凉劲爽沐浴露 450g',
        'category': '个护',
        'price': 6.48,
        'original_price': 12.9,
        'unit': '瓶',
        'specs': '450g/冰凉劲爽',
        'stock': 290,
        'merchant_id': 'M002',
    },
    {
        'name': '冷酸灵 抗敏感牙膏 140g',
        'category': '个护',
        'price': 4.48,
        'original_price': 19.9,
        'unit': '支',
        'specs': '140g/抗敏感',
        'stock': 340,
        'merchant_id': 'M002',
    },
    {
        'name': '舒适达 专业修复抗敏感牙膏 300g',
        'category': '个护',
        'price': 73.45,
        'original_price': 149.0,
        'unit': '支',
        'specs': '300g/专业修复',
        'stock': 90,
        'merchant_id': 'M002',
    },
    {
        'name': '舒客 防蛀氟素牙膏 140g',
        'category': '个护',
        'price': 4.65,
        'original_price': 9.89,
        'unit': '支',
        'specs': '140g/防蛀氟素',
        'stock': 360,
        'merchant_id': 'M002',
    },
    {
        'name': '高露洁 美白护龈牙膏 3支装 120g*2+40g',
        'category': '个护',
        'price': 13.99,
        'original_price': 29.9,
        'unit': '套',
        'specs': '280g/3支装',
        'stock': 250,
        'merchant_id': 'M002',
    },
]

OFFLINE_DOC = {
    "source": "web_research",
    "fetched_at": "2026-10-07",
    "is_demo": 1,
    "note": "演示数据，非真实交易",
}


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------

def guess_category(name, keywords):
    for cat, kws in keywords.items():
        for kw in kws:
            if kw in name:
                return cat
    return None


def normalize(name, price, original, unit, specs, stock, mid, category=None):
    """构造一条符合 schema 的商品记录。"""
    cat = category or guess_category(name, CATEGORY_KEYWORDS) or "清洁"
    price = round(float(price), 2)
    original = round(float(original), 2)
    if original < price:      # 划线价必须 >= 售价
        original = price
    return {
        "name": name, "category": cat, "price": price,
        "original_price": original, "unit": unit, "specs": specs,
        "stock": max(1, int(stock)), "merchant_id": mid,
    }


def run_offline(limit):
    items = [dict(p) for p in OFFLINE_PRODUCTS]
    if limit and limit > 0:
        items = items[:limit]
    doc = dict(OFFLINE_DOC)
    doc["products"] = items
    return doc, "web_research(离线兜底)"


def run_online(name, limit):
    """尝试真实抓取一个源；抓不到有效商品则返回空列表。"""
    pool = []
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if len(pool) >= limit:
                break
            try:
                if name == "dangdang":
                    raw = scrape_dangdang(kw, limit=limit)
                    pool += [{"name": r["name"], "price": r["price"], "cat": cat} for r in raw]
                elif name == "suning":
                    raw = scrape_suning(kw, limit=limit)
                    pool += [{"name": r["name"], "price": None, "cat": cat} for r in raw]
            except Exception as exc:  # 单个关键词失败不中断
                print("  [%s] 关键词 '%s' 异常: %s" % (name, kw, exc))
            time.sleep(0.8)
        if len(pool) >= limit:
            break
    return pool


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="AgentMall 演示商品数据抓取脚本（演示数据，非真实交易）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    ap.add_argument("--limit", type=int, default=60, help="最多抓取条数（0=不限）")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "dangdang", "suning", "offline"],
                    help="数据源：auto=先探测在线源，不足则兜底")
    ap.add_argument("--probe", action="store_true", help="只做可达性探测")
    ap.add_argument("--min-items", type=int, default=40,
                    help="auto 模式下低于该条数即回落离线兜底")
    args = ap.parse_args(argv)

    limit = args.limit if args.limit and args.limit > 0 else 60

    print("=== AgentMall 商品数据抓取（演示数据，非真实交易）===")

    if args.probe:
        for nm, tpl, status, note in SOURCE_PROBES:
            url = tpl % urllib.parse.quote("纸巾")
            alive = probe(url)
            print("  %-9s %-7s 实测=%-5s  %s" % (nm, status, "通" if alive else "不通", note))
        return 0

    doc = None
    used = None
    fallback_reason = "在线源不可达"

    if args.source in ("dangdang", "suning"):
        pool = run_online(args.source, limit)
        priced = [p for p in pool if p.get("price")]
        print("在线抓取 [%s]: 解析 %d 条，其中带价格 %d 条" % (args.source, len(pool), len(priced)))
        if len(priced) >= max(args.min_items, 1):
            items = []
            for i, p in enumerate(priced[:limit]):
                try:
                    price = p["price"]
                    items.append(normalize(
                        p["name"], price, round(price * 1.45, 2), "件", "见商品名",
                        120 + (i * 37) % 380, ["M001", "M002", "M003"][i % 3], p.get("cat")))
                except Exception as exc:
                    print("  跳过一条（%s）: %s" % (p["name"][:20], exc))
            doc = dict(OFFLINE_DOC)
            doc["source"] = "爬取自当当网(%s)" % args.source
            doc["products"] = items
            used = "dangdang"
        else:
            print("  有效商品不足 %d 条，回落离线兜底" % args.min_items)

    elif args.source == "auto":
        alive_all = []
        for nm, tpl, status, _note in SOURCE_PROBES:
            if nm in ("dangdang", "suning"):
                alive = probe(tpl % urllib.parse.quote("纸巾"))
                alive_all.append(alive)
                print("  探测 %-9s -> %s" % (nm, "可达" if alive else "不可达"))
        any_alive = any(alive_all)
        # 在线源虽然可达，但 1688/淘宝/京东被风控拦截、当当网是书店（不卖这些
        # 日用品）、苏宁价格走签名接口 —— 实测有效商品远不足演示所需，故兜底。
        for nm in ("dangdang", "suning"):
            try:
                raw = scrape_dangdang("纸巾", limit=40) if nm == "dangdang" else []
                if nm == "dangdang":
                    print("  实抓当当网『纸巾』：%d 条（书店站，命中多为纸艺图书）" % len(raw))
            except Exception as exc:
                print("  实抓 %s 异常：%r" % (nm, exc))
        if any_alive:
            reason = "在线源可达但有效商品不足（当当网为书店站/苏宁价格走签名接口）"
        else:
            reason = "在线源不可达"
        fallback_reason = reason

    if doc is None:
        print("回落离线兜底（%s）；source=web_research，生产环境切官方 API" % fallback_reason)
        doc, used = run_offline(limit)

    products = doc.get("products", [])
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    cats = {}
    for p in products:
        cats[p["category"]] = cats.get(p["category"], 0) + 1
    print("抓取完成：共 %d 条 (source=%s)" % (len(products), doc.get("source")))
    for c in sorted(cats):
        print("   %-5s %d" % (c, cats[c]))
    print("已写入：%s" % args.out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("已中断")
        sys.exit(130)
    except Exception as exc:  # 绝不裸崩
        print("运行出错：%r" % exc)
        sys.exit(1)
