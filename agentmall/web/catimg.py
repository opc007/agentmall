"""类目 → 缩略图映射（Phase A P0：商品卡片加类目图）。

图片由指挥官提交在 `agentmall/web/static/img/category/`，命名是**拼音**
（cat-zhijin.jpg 而不是 cat-纸巾.jpg）——中文文件名在 URL、反代、
部分 Windows 环境里是坑，所以统一用拼音，映射写死在这里。

**兜底很重要**：映射表里没有的类目一律回落到 `cat-shipin.jpg`，
绝不能出现破图——路演现场一张裂图比没有图更伤。
"""
from __future__ import annotations

STATIC_PREFIX = "/static/img/category"
FALLBACK = "cat-shipin.jpg"

#: category 字段（小驼拼音）→ 图片文件名
CATEGORY_IMG: dict[str, str] = {
    "食品": "cat-shipin.jpg",
    "洗护": "cat-xihu.jpg",
    "纸巾": "cat-zhijin.jpg",
    "母婴": "cat-muying.jpg",
    "家居": "cat-jiaju.jpg",
    "个护": "cat-gehu.jpg",
    "厨房": "cat-chufang.jpg",
    "办公": "cat-bangong.jpg",
    "收纳": "cat-shouna.jpg",
    "衣物清洁": "cat-yiwu-qingjie.jpg",
    "清洁": "cat-qingjie.jpg",
    "家庭清洁": "cat-jiating-qingjie.jpg",
    "宠物": "cat-chongwu.jpg",
    "垃圾袋": "cat-lajidai.jpg",
    "卫生护理": "cat-weisheng-huli.jpg",
}


def img_file(category: str | None) -> str:
    """类目 → 图片文件名。未知/空类目一律兜底，绝不返回空串。"""
    return CATEGORY_IMG.get((category or "").strip(), FALLBACK)


def img_url(category: str | None) -> str:
    """类目 → 可直接写进 <img src> 的静态路径。"""
    return f"{STATIC_PREFIX}/{img_file(category)}"


def register(env) -> None:
    """把 cat_url 注册成 Jinja 过滤器：{{ product.category | cat_url }}。

    app.py 和 merchant_app.py 都要调一次，保证两边的模板写法一致。
    """
    env.filters["cat_url"] = img_url
    env.filters["cat_img"] = img_file
