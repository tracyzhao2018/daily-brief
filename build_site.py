# -*- coding: utf-8 -*-
"""
每日资讯简报 · 静态站点构建脚本
兼容 Python 3.6+ / 3.11，仅使用标准库。
用法: python build_site.py
输出: site/index.html 、 data/cache.json
"""

from __future__ import print_function

import calendar
import datetime
import email.utils
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import traceback
import xml.etree.ElementTree as ET

from concurrent import futures

try:
    from urllib.parse import urljoin, urlsplit
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    from urlparse import urljoin, urlsplit
    from urllib2 import Request, urlopen

import html as html_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "site")
OUT_FILE = os.path.join(OUT_DIR, "index.html")
CACHE_FILE = os.path.join(BASE_DIR, "data", "cache.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 10
MAX_ITEMS = 6
MIN_ITEMS = 4
RECENT_DAYS = 10

# 事件聚类（今日焦点）
FOCUS_HOURS = 24
FOCUS_MIN_COUNT = 3
FOCUS_TOP_N = 3
FOCUS_LEAD_COUNT = 5
STOPWORDS = set(u"""
中国 美国 英国 法国 德国 日本 韩国 俄罗斯 乌克兰 以色列 伊朗 印度 欧盟 联合国
记者 报道 消息 新闻 快讯 突发 最新 独家 关注 来源 编辑 声明 官方 专家 网友 视频 直播 图片
表示 宣布 发布 今天 今年 今日 昨日 昨天 近日 上午 下午 晚上 目前 现在 已经 正在 将于 预计 计划
公司 集团 企业 亿元 万元 同比 环比 增长 下降 上涨 下跌 超过 突破 达到 实现 完成 开始 结束 继续
进行 相关 方面 问题 情况 工作 发展 建设 重要 主要 首次 全面 持续 推动 提升 加强 支持 促进 优化
我们 他们 你们 一个 这个 那个 这些 那些 其中 以及 但是 因为 如果 所以 此外 同时 可以 没有 不是
地区 国家 部门 市场 价格 服务 系统 平台 技术 产品 用户 数据 全球 国际 经济 社会 文化 教育 健康
医院 医生 学生 学校 法院 警方 调查 影响 应对 措施 通知 要求 介绍 指出 认为 回应 通报 公布 启动
举行 召开 参加 政府 国务院 委员会 中心 研究院 大学 大学 主席 书记 总统 总理 部长 委员会 银行 基金
股价 指数 收盘 开盘 成交 板块 概念 上市 投资 消费 产业 行业 领域 项目 工程 活动 会议 论坛 峰会
""".split())

_BAD_EDGE = set(u"的了在是和与为将已被对从到有无不就都还等及或并而其之中上下内外着过把让使")
STOPWORDS_LONG = [w for w in STOPWORDS if len(w) >= 3]

# 图片本地化
IMG_DIRNAME = "img"
IMG_WORKERS = 8
IMG_BUDGET = 25.0
IMG_EXT_WHITELIST = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# 原文配图（og:image）抓取与持久缓存
IMG_CACHE_FILE = os.path.join(BASE_DIR, "data", "img_cache.json")
IMG_CACHE_TTL = 7 * 86400
OG_WORKERS = 8
OG_TIMEOUT = 8
OG_BUDGET = 90.0
OG_SKIP_WORDS = (u"logo", u"icon", u"avatar", u"qr", u"二维码", u"erweima", u"sprite", u"placeholder")
# 正文容器特征（按可靠度排序），用于在正文区找首个配图
OG_CONTENT_MARKERS = (
    u"left_zw", u'class="article-content"', u'class="article_content"',
    u'class="article"', u'id="article"', u'class="content"', u'id="content"',
    u'class="detail"', u"entry-content", u"post-body", u"TRS_Editor",
)

# ---------------------------------------------------------------------------
# 数据源（均已实测；失败时自动回落缓存）
# 说明：人民网 RSS 更新滞后，仅作关键词兜底；36氪存在 WAF 拦截，仅作可选源。
# ---------------------------------------------------------------------------
FEEDS = {
    "news": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/china.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/world.xml"),
        ("人民网", "http://www.people.com.cn/rss/politics.xml"),
    ],
    "military": [
        ("人民网", "http://www.people.com.cn/rss/military.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/china.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/world.xml"),
    ],
    "finance": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/finance.xml"),
        ("人民网", "http://www.people.com.cn/rss/finance.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    ],
    "stock": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/finance.xml"),
        ("人民网", "http://www.people.com.cn/rss/finance.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    ],
    "tech": [
        ("IT之家", "https://www.ithome.com/rss/"),
        ("36氪", "https://36kr.com/feed"),
        ("人民网", "http://www.people.com.cn/rss/it.xml"),
    ],
    "law": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/china.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/society.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
        ("人民网", "http://www.people.com.cn/rss/politics.xml"),
    ],
    "agri": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/china.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/finance.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/society.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    ],
    "edu": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/edu.xml"),
        ("人民网", "http://www.people.com.cn/rss/edu.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    ],
    "local": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/china.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/society.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/finance.xml"),
    ],
    "culture": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/culture.xml"),
        ("人民网", "http://www.people.com.cn/rss/culture.xml"),
    ],
    "health": [
        ("中国新闻网", "https://www.chinanews.com.cn/rss/health.xml"),
        ("人民网", "http://www.people.com.cn/rss/health.xml"),
        ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    ],
}

# ---------------------------------------------------------------------------
# 栏目配置：默认配图取自原版页面（每个栏目固定一张）
# ---------------------------------------------------------------------------
TRAE = "https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt="
IMG_SIZE = "&image_size=landscape_4_3"

COLUMNS = [
    {
        "key": "news",
        "num": "01",
        "name": "要闻",
        "kicker": "TODAY",
        "title": "全球 · 国内要闻",
        "tag": "要闻",
        "filter": False,
        "img": TRAE + "a%20rocket%20launching%20at%20dawn%20from%20a%20coastal%20launch%20pad"
                      "%2C%20billowing%20smoke%2C%20photorealistic%20news%20photography" + IMG_SIZE,
        "groups": [
            ("航天", [u"发射", u"卫星", u"长征", u"航天"]),
            ("宏观", [u"国务院", u"政策", u"会议", u"部署"]),
            ("国际", [u"美国", u"伊朗", u"俄罗斯", u"乌克兰", u"欧洲", u"日本", u"韩国"]),
            ("民生", [u"气象", u"台风", u"寒潮", u"交通", u"铁路", u"防汛"]),
        ],
    },
    {
        "key": "military",
        "num": "02",
        "name": "军事·台海",
        "kicker": "DEFENSE",
        "title": "军事 · 台海动态",
        "tag": "军事",
        "filter": True,
        "img": TRAE + "a%20modern%20naval%20warship%20with%20a%20stealth%20drone%20on%20deck%20in%20open%20sea"
                      "%2C%20dramatic%20clouds%2C%20military%20photography" + IMG_SIZE,
        "keywords": [u"军事", u"国防", u"军队", u"解放军", u"海军", u"空军", u"陆军", u"火箭军",
                     u"武警", u"演习", u"演训", u"军演", u"航母", u"舰", u"战机", u"军机",
                     u"导弹", u"无人机", u"台海", u"台湾", u"两岸", u"台独", u"军售",
                     u"巡航", u"维和", u"征兵", u"阅兵", u"国防部", u"海警", u"美军",
                     u"以军", u"俄军", u"乌军", u"北约", u"中东", u"伊朗", u"以色列",
                     u"空袭", u"袭击", u"冲突", u"战争", u"军工", u"武器", u"部队",
                     u"军舰", u"边防", u"台军", u"防空"],
        "groups": [
            ("台海", [u"台海", u"台湾", u"两岸", u"台独"]),
            ("演训", [u"演习", u"演训", u"军演", u"训练", u"巡航"]),
            ("装备", [u"航母", u"战机", u"导弹", u"舰", u"无人机", u"雷达"]),
            ("国防", [u"国防", u"军队", u"解放军", u"武警", u"军事"]),
            ("国际", [u"俄乌", u"北约", u"以色列", u"美军"]),
        ],
    },
    {
        "key": "finance",
        "num": "03",
        "name": "财经",
        "kicker": "FINANCE",
        "title": "财经 · 宏观政策",
        "tag": "财经",
        "filter": False,
        "img": TRAE + "shanghai%20financial%20district%20skyline%20at%20dusk%2C%20glass%20towers"
                      "%2C%20market%20data%20screens%2C%20cinematic" + IMG_SIZE,
        "groups": [
            ("货币政策", [u"央行", u"逆回购", u"利率", u"降准", u"MLF", u"LPR"]),
            ("宏观", [u"GDP", u"CPI", u"PMI", u"财政", u"税收", u"统计"]),
            ("产业", [u"制造", u"工业", u"产业", u"企业"]),
            ("外贸", [u"出口", u"进口", u"关税", u"外贸"]),
            ("金融", [u"银行", u"保险", u"证券", u"基金"]),
        ],
    },
    {
        "key": "stock",
        "num": "04",
        "name": "股票",
        "kicker": "MARKET",
        "title": "股票 · 市场速览",
        "tag": "市场",
        "filter": True,
        "img": TRAE + "close%20up%20of%20stock%20market%20trading%20screens%20with%20candlestick%20charts"
                      "%20in%20red%20and%20green%2C%20dark%20room" + IMG_SIZE,
        "keywords": [u"A股", u"沪指", u"上证", u"深证", u"创业板", u"科创", u"涨停", u"跌停",
                     u"股价", u"指数", u"板块", u"成交", u"开盘", u"收盘", u"券商", u"基金",
                     u"北向", u"融资", u"IPO", u"港股", u"美股", u"证券", u"大盘", u"涨幅",
                     u"跌幅", u"两市", u"沪市", u"深市", u"主力资金"],
        "groups": [
            ("A股", [u"A股", u"沪指", u"上证", u"深证", u"创业板", u"科创"]),
            ("个股", [u"涨停", u"跌停", u"股价", u"个股"]),
            ("机构", [u"券商", u"基金", u"北向", u"融资"]),
            ("市场", [u"指数", u"板块", u"成交", u"开盘", u"收盘"]),
        ],
    },
    {
        "key": "tech",
        "num": "05",
        "name": "科技",
        "kicker": "TECH",
        "title": "科技 · 前沿进展",
        "tag": "科技",
        "filter": False,
        "img": TRAE + "humanoid%20robot%20arm%20in%20a%20modern%20robotics%20laboratory%2C%20blue%20rim%20light"
                      "%2C%20macro%20detail" + IMG_SIZE,
        "groups": [
            ("大模型", [u"AI", u"大模型", u"智能体", u"GPT", u"模型"]),
            ("硬件", [u"芯片", u"手机", u"处理器", u"显卡", u"英伟达", u"华为"]),
            ("互联网", [u"阿里", u"腾讯", u"字节", u"平台"]),
            ("前沿", [u"机器人", u"量子", u"卫星", u"自动驾驶"]),
            ("新品", [u"发布", u"上市", u"亮相"]),
        ],
    },
    {
        "key": "law",
        "num": "06",
        "name": "法律",
        "kicker": "LAW",
        "title": "法律 · 立法与司法",
        "tag": "法治",
        "filter": True,
        "img": TRAE + "solemn%20courtroom%20interior%20with%20wooden%20desk%2C%20law%20books"
                      "%2C%20soft%20window%20light%2C%20photorealistic" + IMG_SIZE,
        "keywords": [u"立法", u"法典", u"草案", u"条例", u"办法", u"法院", u"检察",
                     u"司法", u"判决", u"审理", u"起诉", u"逮捕", u"反腐", u"受贿", u"违纪",
                     u"执法", u"查处", u"违法", u"律师", u"仲裁", u"未成年人", u"知识产权",
                     u"治安", u"刑法", u"民法典", u"法治", u"公安部", u"最高法", u"最高检",
                     u"司法解释", u"行政处罚", u"刑事", u"民事", u"裁定", u"立案", u"公诉",
                     u"审查调查"],
        "groups": [
            ("立法", [u"立法", u"草案", u"修订", u"条例", u"表决", u"施行"]),
            ("司法", [u"法院", u"检察", u"判决", u"审理", u"司法"]),
            ("执法", [u"公安", u"执法", u"查处", u"打击", u"违法"]),
            ("反腐", [u"反腐", u"受贿", u"违纪", u"审查"]),
            ("权益", [u"未成年人", u"劳动", u"消费者", u"赔偿"]),
        ],
    },
    {
        "key": "agri",
        "num": "07",
        "name": "三农",
        "kicker": "RURAL",
        "title": "三农 · 乡村振兴",
        "tag": "三农",
        "filter": True,
        "img": TRAE + "golden%20rice%20harvest%20in%20autumn%2C%20farmer%20working%20in%20field"
                      "%2C%20aerial%20view%2C%20warm%20sunset" + IMG_SIZE,
        "keywords": [u"三农", u"农业", u"农村", u"农民", u"乡村", u"粮食", u"秋粮", u"夏粮",
                     u"收储", u"耕地", u"农田", u"种植", u"养殖", u"土地承包", u"宅基地",
                     u"脱贫", u"增收", u"农产品", u"供销社", u"农机", u"水利", u"秋收",
                     u"夏收", u"丰收", u"合作社", u"种业", u"生猪", u"蔬菜", u"县域"],
        "groups": [
            ("粮食", [u"粮食", u"秋粮", u"夏粮", u"收储", u"收购"]),
            ("乡村振兴", [u"乡村振兴", u"乡村", u"农村", u"农民"]),
            ("农业", [u"农业", u"种植", u"养殖", u"耕地", u"农田"]),
            ("土地", [u"土地承包", u"宅基地", u"确权"]),
            ("增收", [u"增收", u"收入", u"脱贫"]),
        ],
    },
    {
        "key": "edu",
        "num": "08",
        "name": "教育",
        "kicker": "EDUCATION",
        "title": "教育 · 招考政策",
        "tag": "教育",
        "filter": True,
        "img": TRAE + "chinese%20students%20in%20a%20bright%20classroom%2C%20teacher%20at%20blackboard"
                      "%2C%20warm%20natural%20light%2C%20documentary%20photo" + IMG_SIZE,
        "keywords": [u"教育", u"高校", u"学校", u"学生", u"教师", u"高考", u"招生", u"录取",
                     u"志愿", u"学位", u"学科", u"考试", u"考研", u"就业", u"毕业生", u"校园",
                     u"教育部", u"留学", u"职教", u"双减", u"课程"],
        "groups": [
            ("高考", [u"高考", u"招生", u"志愿", u"录取"]),
            ("政策", [u"教育部", u"政策", u"学位", u"学科"]),
            ("校园", [u"学校", u"学生", u"教师", u"校园"]),
            ("就业", [u"就业", u"毕业生", u"招聘"]),
            ("考试", [u"考试", u"考研", u"竞赛"]),
        ],
    },
    {
        "key": "local",
        "num": "09",
        "name": "本地·江苏",
        "kicker": "LOCAL",
        "title": "本地 · 江苏 / 南京",
        "tag": "江苏",
        "filter": True,
        "img": TRAE + "nanjing%20ancient%20city%20wall%20with%20golden%20ginkgo%20trees%20in%20autumn"
                      "%2C%20soft%20morning%20light" + IMG_SIZE,
        "keywords": [u"江苏", u"南京", u"苏州", u"无锡", u"常州", u"镇江", u"南通", u"扬州",
                     u"泰州", u"徐州", u"盐城", u"淮安", u"连云港", u"宿迁", u"金陵",
                     u"苏南", u"苏北", u"长三角"],
        "groups": [
            ("南京", [u"南京", u"金陵"]),
            ("苏南", [u"苏州", u"无锡", u"常州", u"镇江"]),
            ("苏中", [u"南通", u"扬州", u"泰州"]),
            ("苏北", [u"徐州", u"盐城", u"淮安", u"连云港", u"宿迁"]),
            ("江苏", [u"江苏", u"全省", u"省级"]),
        ],
    },
    {
        "key": "culture",
        "num": "10",
        "name": "文化",
        "kicker": "CULTURE",
        "title": "文化 · 展演与展览",
        "tag": "文化",
        "filter": False,
        "img": TRAE + "traditional%20chinese%20dance%20drama%20on%20stage%2C%20flowing%20silk%20costume"
                      "%2C%20theatrical%20lighting" + IMG_SIZE,
        "groups": [
            ("舞台", [u"演出", u"舞剧", u"话剧", u"音乐会", u"戏曲"]),
            ("展览", [u"展览", u"博物馆", u"美术馆", u"文物"]),
            ("影视", [u"电影", u"电视剧", u"影视", u"票房"]),
            ("非遗", [u"非遗", u"民俗", u"传承"]),
            ("文学", [u"文学", u"图书", u"阅读", u"出版"]),
        ],
    },
    {
        "key": "health",
        "num": "11",
        "name": "健康",
        "kicker": "HEALTH",
        "title": "健康 · 疾控提醒",
        "tag": "健康",
        "filter": True,
        "img": TRAE + "doctor%20in%20white%20coat%20reviewing%20medical%20data%20on%20tablet"
                      "%2C%20bright%20hospital%2C%20shallow%20depth%20of%20field" + IMG_SIZE,
        "keywords": [u"健康", u"医疗", u"医院", u"医生", u"疾控", u"流感", u"疫苗", u"药物",
                     u"用药", u"疾病", u"患者", u"卫生健康", u"医保", u"传染病", u"病毒",
                     u"营养", u"心理", u"急救"],
        "groups": [
            ("流感", [u"流感", u"甲流", u"乙流", u"H3N2", u"H1N1"]),
            ("疾控", [u"疾控", u"疫情", u"监测", u"预警"]),
            ("医疗", [u"医院", u"医生", u"诊疗", u"医保"]),
            ("用药", [u"药物", u"疫苗", u"用药", u"抗病毒"]),
            ("健康", [u"健康", u"养生", u"睡眠", u"饮食", u"运动"]),
        ],
    },
]

# 各栏目前 N 个源视为「主源」，其余为补位源（避免通用滚动源挤占分类稿）
PRIMARY_COUNT = {
    "news": 3, "military": 2, "finance": 2, "stock": 2, "tech": 2,
    "law": 2, "agri": 3, "edu": 2, "local": 2, "culture": 2, "health": 2,
}

# 选稿顺序：要闻优先占据头条，其余按专稿程度递减排布，避免同一稿件跨栏目重复
PROCESS_ORDER = ["news", "stock", "military", "finance", "tech",
                 "law", "agri", "local", "health", "edu", "culture"]

# 这些栏目的关键词命中稿优先留给本栏目，要闻不再重复选用
RESERVE_KEYS = ["stock", "military", "law", "agri", "local"]
RESERVE_WORDS = []
for _col in COLUMNS:
    if _col["key"] in RESERVE_KEYS:
        RESERVE_WORDS.extend(_col.get("keywords") or [])

WEEKDAYS = [u"星期一", u"星期二", u"星期三", u"星期四", u"星期五", u"星期六", u"星期日"]

TITLE_SUFFIXES = [u"中国新闻网", u"中新网", u"人民网", u"新华网", u"IT之家", u"36氪",
                  u"央视网", u"环球网", u"澎湃新闻", u"新浪财经", u"证券时报", u"中国日报",
                  u"光明网", u"经济日报", u"中国青年报"]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def now_dt():
    """统一按北京时间（UTC+8）生成日期，避免 Actions 运行在 UTC 时区时日期错位。"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def esc(s):
    return html_mod.escape(s or u"", quote=True)


def strip_tags(text):
    if not text:
        return u""
    s = text
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", u" ", s)
    s = re.sub(r"(?s)<!--.*?-->", u" ", s)
    s = re.sub(r"(?s)<[^>]+>", u" ", s)
    s = html_mod.unescape(s)
    s = s.replace(u"\u3000", u" ").replace(u"\xa0", u" ")
    s = re.sub(r"\s+", u" ", s)
    return s.strip()


def clean_title(text):
    t = strip_tags(text)
    t = re.sub(u"^(【[^】]{1,8}】|\\[[^\\]]{1,8}\\])\\s*", u"", t)
    for sep in (u" - ", u"_", u"—", u"｜", u"|", u"·"):
        for name in TITLE_SUFFIXES:
            for pat in (sep + name, name + sep):
                if t.endswith(pat):
                    t = t[:-len(pat)].strip()
    t = t.strip(u" -_|｜—·")
    return t


def make_summary(text):
    s = strip_tags(text)
    s = re.sub(u"^(IT之家|中国新闻网|人民网|新华网|央视网)\\s*[:：]\\s*", u"", s)
    s = re.sub(u"^(IT之家\\s*\\d+\\s*月\\s*\\d+\\s*日消息\\s*[，,]?)\\s*", u"", s)
    s = re.sub(u"(…+|\\.{3,})\\s*$", u"", s).strip()
    s = s.replace(u"……", u"，")
    if len(s) > 110:
        cut = s.rfind(u"。", 60, 109)
        if cut > 0:
            s = s[:cut + 1]
        else:
            s = s[:108].rstrip(u"，,、；;：: ") + u"…"
    return s


def parse_date(value):
    s = (value or u"").strip()
    if not s:
        return 0
    try:
        tup = email.utils.parsedate_tz(s)
        if tup:
            return int(email.utils.mktime_tz(tup))
    except Exception:
        pass
    s = s.replace(u"年", u"-").replace(u"月", u"-").replace(u"日", u"")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            dt = datetime.datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
            return calendar.timegm(dt.timetuple()) - 8 * 3600
        except Exception:
            continue
    return 0


def fmt_stamp(ts, with_time):
    if not ts:
        return u""
    try:
        dt = datetime.datetime.utcfromtimestamp(ts + 8 * 3600)
    except Exception:
        return u""
    if with_time:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%m-%d")


def relative_time(ts, now_ts):
    if not ts:
        return u"--"
    diff = int(now_ts - ts)
    if diff < 0:
        diff = 0
    if diff < 60:
        return u"刚刚"
    if diff < 3600:
        return u"%d 分钟前" % (diff // 60)
    if diff < 86400:
        return u"%d 小时前" % (diff // 3600)
    if diff < 172800:
        return u"昨天 " + fmt_stamp(ts, True)[-5:]
    return fmt_stamp(ts, False)


# ---------------------------------------------------------------------------
# 抓取与解析
# ---------------------------------------------------------------------------
def fetch(url):
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    resp = urlopen(req, timeout=TIMEOUT)
    code = getattr(resp, "getcode", lambda: 200)()
    if code != 200:
        raise IOError(u"HTTP %s" % code)
    raw = resp.read()
    try:
        resp.close()
    except Exception:
        pass
    return raw


def parse_xml(raw):
    try:
        return ET.fromstring(raw)
    except Exception:
        text = raw.decode("utf-8", "replace")
        text = re.sub(u"&(?!(?:[a-zA-Z][a-zA-Z0-9]{0,8}|#\\d{1,6}|#x[0-9a-fA-F]{1,6});)", u"&amp;", text)
        text = re.sub(u"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]", u"", text)
        return ET.fromstring(text.encode("utf-8"))


def find_media(node):
    for child in node:
        tag = child.tag.split(u"}")[-1].lower()
        if tag in ("content", "thumbnail"):
            url = child.get("url")
            if url:
                return url
        if tag == "enclosure":
            url = child.get("url")
            if url and u"image" in (child.get("type") or u"image"):
                return url
    return u""


def find_img_in_html(text, base):
    if not text:
        return u""
    m = re.search(r"(?is)<img[^>]+?src=[\"']([^\"']+)[\"']", text)
    if not m:
        return u""
    src = html_mod.unescape(m.group(1)).strip()
    if src.startswith("//"):
        return u"https:" + src
    if src.startswith("data:"):
        return u""
    return urljoin(base, src)


def parse_items(raw, source_name, feed_url):
    root = parse_xml(raw)
    out = []
    for node in root.findall(".//item"):
        title = clean_title(node.findtext("title"))
        link = (node.findtext("link") or u"").strip()
        desc = node.findtext("description") or u""
        encoded = node.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or u""
        date_raw = node.findtext("pubDate") or node.findtext(
            "{http://purl.org/dc/elements/1.1/}date") or u""
        if not title or not link:
            continue

        img = find_media(node)
        if not img:
            img = find_img_in_html(desc, feed_url)
        if not img:
            img = find_img_in_html(encoded, feed_url)
        if img.startswith("//"):
            img = u"https:" + img

        ts = parse_date(date_raw)
        out.append({
            "title": title,
            "link": link,
            "summary": make_summary(desc or encoded),
            "ts": ts,
            "image": img,
            "source": source_name,
            "feed": feed_url,
            "tag": u"",
        })
    return out


def load_feed(name, url, memo, failures):
    if url in memo:
        items, err = memo[url]
        if err:
            failures.append((name, url, err))
        return items, err
    try:
        raw = fetch(url)
        items = parse_items(raw, name, url)
        if not items:
            raise ValueError(u"条目为空")
        memo[url] = (items, None)
        return items, None
    except Exception as exc:
        err = u"%s: %s" % (type(exc).__name__, str(exc)[:80])
        memo[url] = ([], err)
        failures.append((name, url, err))
        return [], err


# ---------------------------------------------------------------------------
# 选稿
# ---------------------------------------------------------------------------
def match_tag(col, text):
    for label, words in col.get("groups", []):
        for w in words:
            if w.lower() in text.lower():
                return label
    return col["tag"]


def hit_keywords(col, text):
    for w in col.get("keywords", []):
        if w.lower() in text.lower():
            return True
    return False


def rank(items):
    items.sort(key=lambda it: (it.get("ts") or 0, len(it.get("summary") or u"")), reverse=True)
    return items


def split_recent(items, cutoff):
    recent = [it for it in items if (it.get("ts") or 0) >= cutoff]
    old = [it for it in items if (it.get("ts") or 0) < cutoff]
    return recent, old


def _norm_title(text):
    return re.sub(u"[\\s\\W_]+", u"", text or u"")


def near_dup(a, b):
    """标题近似判重：包含关系或存在 8 字以上公共片段。"""
    a = _norm_title(a)
    b = _norm_title(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    n = 8
    if len(a) < n or len(b) < n:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    for i in range(len(short) - n + 1):
        if short[i:i + n] in long_:
            return True
    return False


def collect_column(col, failures):
    key = col["key"]
    raw = []
    memo = {}
    for idx, feed in enumerate(FEEDS.get(key, [])):
        name, url = feed
        items, _err = load_feed(name, url, memo, failures)
        for it in items:
            it["_rank"] = idx
        raw.extend(items)

    seen = set()
    uniq = []
    for it in rank(raw):
        norm = re.sub(r"[?#].*$", "", it["link"] or u"").rstrip(u"/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        dup = False
        for kept in uniq:
            if near_dup(it["title"], kept["title"]):
                dup = True
                break
        if dup:
            continue
        it["tag"] = match_tag(col, it["title"] + u" " + it["summary"])
        uniq.append(it)

    if key == "news":
        reserved_pool = [it for it in uniq
                         if not any(w.lower() in it["title"].lower() for w in RESERVE_WORDS)]
        if len(reserved_pool) >= MIN_ITEMS:
            uniq = reserved_pool
    return uniq


def _link_of(it):
    return re.sub(r"[?#].*$", "", it.get("link") or u"").rstrip(u"/")


def _focus_noise(sub):
    if sub in STOPWORDS:
        return True
    if sub and (sub[0] in _BAD_EDGE or sub[-1] in _BAD_EDGE):
        return True
    for word in STOPWORDS_LONG:
        if word in sub and len(sub) <= len(word) + 1:
            return True
    return False


def _focus_col(rep, link_col):
    """为焦点事件挑一个「对得上」的栏目：先按栏目关键词打分，否则回落到首次出现栏目。"""
    text = (rep.get("title") or u"") + u" " + (rep.get("summary") or u"")
    best, best_score = u"", 0
    for col in COLUMNS:
        keys = col.get("keywords") or []
        score = 0
        for word in keys:
            if len(word) < 2:
                continue
            if word.lower() in text.lower():
                score += 1
        if score > best_score:
            best, best_score = col["key"], score
    if best:
        return best
    fallback = link_col.get(_link_of(rep)) or u"news"
    if fallback in (u"military", u"stock", u"law", u"agri"):
        return u"news"
    return fallback


def detect_focus(items, link_col, now_ts):
    """在近 24 小时标题中统计 3~6 字共现中文串，返回最多 3 个事件簇。"""
    start = now_ts - FOCUS_HOURS * 3600
    pool = []
    seen = set()
    for it in items:
        ts = it.get("ts") or 0
        if ts < start:
            continue
        norm = _link_of(it)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        pool.append(it)

    counts = {}
    for idx, it in enumerate(pool):
        subs = set()
        for run in re.findall(u"[\u4e00-\u9fa5]+", it.get("title") or u""):
            length = len(run)
            for size in (3, 4, 5, 6):
                if length < size:
                    break
                for i in range(length - size + 1):
                    sub = run[i:i + size]
                    if _focus_noise(sub):
                        continue
                    subs.add(sub)
        for sub in subs:
            counts.setdefault(sub, set()).add(idx)

    cands = []
    for sub, idxs in counts.items():
        if len(idxs) >= FOCUS_MIN_COUNT:
            cands.append((len(idxs), len(sub), sub, idxs))
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)

    chosen = []
    covered = set()
    for cnt, _length, sub, idxs in cands:
        if len(chosen) >= FOCUS_TOP_N or cnt < FOCUS_MIN_COUNT:
            break
        if any(sub in c["keyword"] or c["keyword"] in sub for c in chosen):
            continue
        if covered and len(idxs & covered) >= max(2, int(cnt * 0.5)):
            continue
        members = sorted([pool[i] for i in idxs], key=lambda x: x.get("ts") or 0, reverse=True)
        rep = None
        for m in members:
            if m.get("image"):
                rep = m
                break
        if rep is None:
            rep = members[0]
        chosen.append({
            "keyword": sub,
            "size": cnt,
            "members": members,
            "rep": rep,
            "col": _focus_col(rep, link_col),
        })
        covered |= idxs
    return chosen


def select_column(col, uniq, cache, used, focus, now_ts):
    key = col["key"]
    cutoff = now_ts - RECENT_DAYS * 86400
    recent, old = split_recent(uniq, cutoff)
    ordered = recent + old

    primary_n = PRIMARY_COUNT.get(key, 1)
    primary = [it for it in ordered if it.get("_rank", 99) < primary_n]
    secondary = [it for it in ordered if it.get("_rank", 99) >= primary_n]

    def _link(node):
        return re.sub(r"[?#].*$", "", node.get("link") or u"").rstrip(u"/")

    primary_free = [it for it in primary if _link(it) not in used]
    secondary_free = [it for it in secondary if _link(it) not in used]
    if len(primary_free) + len(secondary_free) >= MIN_ITEMS:
        primary, secondary = primary_free, secondary_free

    picked = []
    if col.get("filter"):
        picked = [it for it in primary if hit_keywords(col, it["title"])]
        picked += [it for it in secondary if hit_keywords(col, it["title"])]
        if len(picked) < MIN_ITEMS:
            chosen = set(id(it) for it in picked)
            for it in primary + secondary:
                if id(it) in chosen:
                    continue
                if hit_keywords(col, it["summary"]):
                    picked.append(it)
                    chosen.add(id(it))
    else:
        picked = primary + secondary

    focus_size = {}
    for f in focus:
        for m in f["members"]:
            norm = _link_of(m)
            if norm:
                focus_size[norm] = f["size"]

    # 焦点簇优先，其次时间倒序
    picked.sort(key=lambda it: (1 if _link_of(it) in focus_size else 0, it.get("ts") or 0),
                reverse=True)

    fresh = [it for it in picked if (it.get("ts") or 0) >= cutoff]
    stale = [it for it in picked if (it.get("ts") or 0) < cutoff]
    picked = fresh[:MAX_ITEMS]

    # 不足 4 条：用补位源稿件填充，明确标注「综合」；优先当日稿件
    if len(picked) < MIN_ITEMS:
        chosen = set(id(it) for it in picked)
        fill_pool = [it for it in primary if (it.get("ts") or 0) >= cutoff]
        fill_pool += [it for it in secondary if (it.get("ts") or 0) >= cutoff]
        for it in fill_pool:
            if len(picked) >= MIN_ITEMS:
                break
            if id(it) in chosen:
                continue
            norm = re.sub(r"[?#].*$", "", it["link"] or u"").rstrip(u"/")
            if norm in used:
                continue
            it["tag"] = u"综合"
            picked.append(it)
            chosen.add(id(it))

    # 仍不足：允许较早稿件的关键词命中补位
    if len(picked) < MIN_ITEMS:
        chosen = set(id(it) for it in picked)
        for it in stale:
            if len(picked) >= MIN_ITEMS:
                break
            if id(it) in chosen:
                continue
            picked.append(it)
            chosen.add(id(it))

    # 仍不足：回落 cache
    if len(picked) < MIN_ITEMS:
        cached = cache.get(key) or []
        links = set(re.sub(r"[?#].*$", "", it["link"] or u"").rstrip(u"/") for it in picked)
        for it in cached:
            if len(picked) >= MIN_ITEMS:
                break
            norm = re.sub(r"[?#].*$", "", it.get("link") or u"").rstrip(u"/")
            if norm and norm in links:
                continue
            item = dict(it)
            item["tag"] = item.get("tag") or u"历史"
            picked.append(item)
            links.add(norm)

    # 最终兜底：占位
    while len(picked) < MIN_ITEMS:
        picked.append({
            "title": u"该栏目今日暂无更多可核实的公开信息",
            "link": u"",
            "summary": u"数据源暂时不可用，栏目版位保留。稍后会自动重试，也可查看其他栏目了解今日进展。",
            "ts": 0,
            "image": u"",
            "source": u"编辑部",
            "feed": u"",
            "tag": u"占位",
            "placeholder": True,
        })

    my_focus = [f for f in focus if f.get("col") == key]
    if my_focus:
        chosen = set(_link_of(it) for it in picked)
        for f in sorted(my_focus, key=lambda x: x["size"], reverse=True):
            norm = _link_of(f["rep"])
            f["rep"]["tag"] = col["tag"]
            if norm not in chosen:
                picked.append(f["rep"])
                chosen.add(norm)
            for it in picked:
                if _link_of(it) == norm:
                    it["focus_count"] = f["size"]
                    break
        leaders = [f for f in my_focus if f["size"] >= FOCUS_LEAD_COUNT]
        if leaders:
            top = max(leaders, key=lambda x: x["size"])
            norm = _link_of(top["rep"])
            target = None
            for it in picked:
                if _link_of(it) == norm:
                    target = it
                    break
            if target is not None and picked[0] is not target:
                picked.remove(target)
                picked.insert(0, target)

    for it in picked:
        it.setdefault("tag", col["tag"])
        if not it.get("tag"):
            it["tag"] = col["tag"]
        if not it.get("summary"):
            it["summary"] = u"点击标题查看原文，了解事件详情与后续进展。"
        norm = re.sub(r"[?#].*$", "", it.get("link") or u"").rstrip(u"/")
        if norm:
            used.add(norm)
    return picked


def load_cache():
    try:
        with io.open(CACHE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        cols = data.get("columns") or {}
        out = {}
        for k, v in cols.items():
            if isinstance(v, list):
                out[k] = v
        return out
    except Exception:
        return {}


def save_cache(columns, stamp):
    payload = {"generated": stamp, "columns": {}}
    for col in COLUMNS:
        items = columns.get(col["key"]) or []
        clean = []
        for it in items:
            if it.get("placeholder"):
                continue
            clean.append({
                "title": it.get("title") or u"",
                "link": it.get("link") or u"",
                "summary": it.get("summary") or u"",
                "ts": it.get("ts") or 0,
                "image": it.get("image") or u"",
                "source": it.get("source") or u"",
                "feed": it.get("feed") or u"",
                "tag": it.get("tag") or col["tag"],
            })
        payload["columns"][col["key"]] = clean
    try:
        if not os.path.isdir(os.path.dirname(CACHE_FILE)):
            os.makedirs(os.path.dirname(CACHE_FILE))
        with io.open(CACHE_FILE, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False, indent=1))
    except Exception as exc:
        print(u"[warn] 写入缓存失败: %s" % exc)


# ---------------------------------------------------------------------------
# 图片本地化
# ---------------------------------------------------------------------------
def image_ext(url):
    path = re.sub(r"[?#].*$", "", url or u"")
    base = path.rsplit(u"/", 1)[-1]
    ext = os.path.splitext(base)[1].lower()
    if ext in IMG_EXT_WHITELIST:
        return ext
    return ".jpg"


def image_filename(url):
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return digest + image_ext(url)


def fetch_image(url, referer, dest, deadline):
    """下载单张图片，失败重试一次；成功返回 True。"""
    for attempt in range(2):
        remain = deadline - time.time()
        if remain <= 0.5:
            return False
        tmp = dest + u".part"
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Referer": referer or url,
                "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            resp = urlopen(req, timeout=min(TIMEOUT, max(1.0, remain)))
            code = getattr(resp, "getcode", lambda: 200)()
            data = resp.read()
            try:
                resp.close()
            except Exception:
                pass
            if code != 200:
                raise IOError(u"HTTP %s" % code)
            if not data or len(data) < 256:
                raise IOError(u"内容过小")
            head = data[:64].lstrip().lower()
            if head.startswith(b"<"):
                raise IOError(u"返回 HTML 而非图片")
            with io.open(tmp, "wb") as fp:
                fp.write(data)
            if os.path.exists(dest):
                os.remove(dest)
            os.rename(tmp, dest)
            return True
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.2)
    return False


def localize_images(jobs, target_dir):
    """jobs 为 [(url, referer)]；返回 {原 url: 'img/文件名'}，失败的 url 不在映射内。"""
    refs = {}
    urls = []
    for url, referer in jobs:
        if not url or not url.startswith(u"http"):
            continue
        if url in refs:
            continue
        refs[url] = referer or u""
        urls.append(url)
    if not urls:
        return {}

    deadline = time.time() + IMG_BUDGET

    def work(url):
        name = image_filename(url)
        ok = fetch_image(url, refs.get(url), os.path.join(target_dir, name), deadline)
        return url, (name if ok else None)

    mapping = {}
    pool = futures.ThreadPoolExecutor(max_workers=IMG_WORKERS)
    try:
        fs = [pool.submit(work, u) for u in urls]
        done, pending = futures.wait(fs, timeout=IMG_BUDGET)
        for f in done:
            try:
                url, name = f.result()
            except Exception:
                continue
            if name:
                mapping[url] = IMG_DIRNAME + u"/" + name
        for f in pending:
            f.cancel()
    finally:
        pool.shutdown(wait=True)
    return mapping


def load_img_cache():
    try:
        with io.open(IMG_CACHE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_img_cache(cache):
    try:
        folder = os.path.dirname(IMG_CACHE_FILE)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(IMG_CACHE_FILE, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))
    except Exception as exc:
        print(u"[warn] 写入图片缓存失败: %s" % exc)


def _meta_content(tag):
    m = re.search(u"(?is)content\\s*=\\s*([\"'])(.*?)\\1", tag)
    if not m:
        return u""
    return html_mod.unescape(m.group(2)).strip()


def _abs_img_url(src, base):
    src = (src or u"").strip()
    if not src or src.startswith(u"data:"):
        return u""
    if src.startswith(u"//"):
        src = u"https:" + src
    try:
        full = urljoin(base, src)
    except Exception:
        return u""
    if not full.startswith(u"http"):
        return u""
    return full


def _is_bad_img(url):
    low = url.lower()
    for word in OG_SKIP_WORDS:
        if word in low:
            return True
    return False


def _img_dim_ok(tag):
    for attr in (u"width", u"height"):
        m = re.search(u"(?i)\\b" + attr + u"\\s*=\\s*[\"']?(\\d+)", tag)
        if m and int(m.group(1)) < 100:
            return False
    return True


def _first_img(chunk, base):
    for tag in re.findall(u"(?is)<img\\b[^>]*>", chunk):
        m = re.search(u"(?is)(?:data-src|data-original|src)\\s*=\\s*([\"'])(.*?)\\1", tag)
        if not m:
            continue
        if not _img_dim_ok(tag):
            continue
        url = _abs_img_url(m.group(2), base)
        if not url or len(url) <= 40 or _is_bad_img(url):
            continue
        return url
    return None


def _extract_og_image(page, base):
    """按 og:image > twitter:image > 正文首个 <img> 的优先级取图。"""
    og, tw = [], []
    for tag in re.findall(u"(?is)<meta\\b[^>]*>", page):
        prop = re.search(u"(?is)(?:property|name|itemprop)\\s*=\\s*([\"'])(.*?)\\1", tag)
        if not prop:
            continue
        key = prop.group(2).strip().lower()
        content = _meta_content(tag)
        if not content:
            continue
        if key in (u"og:image", u"og:image:url", u"og:image:secure_url"):
            og.append(content)
        elif key in (u"twitter:image", u"twitter:image:src"):
            tw.append(content)
    for group in (og, tw):
        for raw in group:
            url = _abs_img_url(raw, base)
            if url and not _is_bad_img(url):
                return url
    for marker in OG_CONTENT_MARKERS:
        pos = page.find(marker)
        if pos < 0:
            continue
        url = _first_img(page[pos:pos + 200000], base)
        if url:
            return url
    return _first_img(page, base)


def fetch_og_image(link):
    """抓取原文页面首图，找不到返回 None。"""
    if not link or not link.startswith(u"http"):
        return None
    try:
        req = Request(link, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": link,
        })
        resp = urlopen(req, timeout=OG_TIMEOUT)
        code = getattr(resp, "getcode", lambda: 200)()
        raw = resp.read(800000)
        ctype = u""
        try:
            ctype = resp.headers.get("Content-Type") or u""
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
    except Exception:
        return None
    if code != 200 or not raw:
        return None
    enc = u"utf-8"
    m = re.search(u"(?i)charset=([\\w-]+)", ctype)
    if m:
        enc = m.group(1)
    else:
        m = re.search(br"(?i)<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096])
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    try:
        page = raw.decode(enc, "replace")
    except Exception:
        page = raw.decode("utf-8", "replace")
    return _extract_og_image(page, link)


def enrich_item_images(columns, img_cache, now_ts):
    """为无图条目抓原文 og:image；7 天内命中缓存不发起网络请求。"""
    tasks = {}
    for col in COLUMNS:
        for it in columns[col["key"]]:
            if it.get("placeholder") or it.get("image"):
                continue
            link = it.get("link") or u""
            if not link.startswith(u"http"):
                continue
            tasks.setdefault(link, []).append(it)

    todo = []
    for link in tasks:
        rec = img_cache.get(link)
        if isinstance(rec, dict) and (now_ts - (rec.get("ts") or 0)) < IMG_CACHE_TTL:
            continue
        todo.append(link)

    fetched = 0
    if todo:
        def work(url):
            return url, fetch_og_image(url)

        pool = futures.ThreadPoolExecutor(max_workers=OG_WORKERS)
        try:
            fs = [pool.submit(work, u) for u in todo]
            done, pending = futures.wait(fs, timeout=OG_BUDGET)
            for f in done:
                try:
                    link, img = f.result()
                except Exception:
                    continue
                img_cache[link] = {"img": img, "ts": int(time.time())}
                fetched += 1
            for f in pending:
                f.cancel()
        finally:
            pool.shutdown(wait=True)

    # 同一张图被多个不同原文复用，多为站点通用图，按无图处理
    usage = {}
    for link in tasks:
        rec = img_cache.get(link)
        if isinstance(rec, dict) and rec.get("img"):
            usage[rec["img"]] = usage.get(rec["img"], 0) + 1
    for link in tasks:
        rec = img_cache.get(link)
        if isinstance(rec, dict) and rec.get("img") and usage.get(rec["img"], 0) >= 3:
            img_cache[link] = {"img": None, "ts": rec.get("ts") or int(time.time())}

    hit = 0
    for link, items in tasks.items():
        rec = img_cache.get(link)
        if not isinstance(rec, dict):
            continue
        img = rec.get("img")
        if not img:
            continue
        hit += 1
        for it in items:
            it["image"] = img
    return fetched, len(tasks), hit


def prepare_image_dir():
    img_dir = os.path.join(OUT_DIR, IMG_DIRNAME)
    if os.path.isdir(img_dir):
        shutil.rmtree(img_dir, ignore_errors=True)
    if not os.path.isdir(img_dir):
        os.makedirs(img_dir)
    return img_dir


def write_nojekyll():
    path = os.path.join(OUT_DIR, ".nojekyll")
    try:
        with io.open(path, "w", encoding="utf-8") as fp:
            fp.write(u"")
    except Exception as exc:
        print(u"[warn] 写入 .nojekyll 失败: %s" % exc)


# ---------------------------------------------------------------------------
# 页面渲染
# ---------------------------------------------------------------------------
CSS = u"""
  :root{
    --paper:#f6f4ef;
    --card:#ffffff;
    --ink:#15171c;
    --ink2:#454a53;
    --muted:#8a8f98;
    --line:rgba(21,23,28,.10);
    --line2:rgba(21,23,28,.06);
    --accent:#c0392b;
    --accent-soft:#faeeeb;
    --gold:#b8894a;
    --green:#2e7d5b;
    --chip:#f0ece4;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;}
  body{
    background:var(--paper);
    color:var(--ink);
    font-family:"PingFang SC","Microsoft YaHei","Hiragino Sans GB",system-ui,-apple-system,sans-serif;
    -webkit-font-smoothing:antialiased;
    font-size:14px;
    line-height:1.65;
  }
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;z-index:0;opacity:.5;
    background-image:radial-gradient(rgba(21,23,28,.035) 1px,transparent 1px);
    background-size:22px 22px;
  }
  a{color:inherit;text-decoration:none;}
  img{display:block;width:100%;height:100%;object-fit:cover;}

  /* ============ 报头 ============ */
  .masthead{position:relative;z-index:1;max-width:1180px;margin:0 auto;padding:26px 24px 0;}
  .mast-top{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap;}
  .brand{display:flex;align-items:center;gap:14px;}
  .brand-mark{
    width:46px;height:46px;flex:0 0 46px;border-radius:6px;
    background:var(--ink);color:#fff;display:grid;place-items:center;
    font-family:"Songti SC","SimSun",serif;font-size:23px;letter-spacing:0;
    box-shadow:0 6px 16px rgba(21,23,28,.18);
  }
  .brand h1{
    margin:0;font-family:"Songti SC","Noto Serif SC","SimSun",serif;
    font-size:23px;font-weight:700;letter-spacing:1px;line-height:1.2;
  }
  .brand p{
    margin:3px 0 0;font-size:10px;letter-spacing:3.4px;color:var(--muted);
    text-transform:uppercase;font-weight:600;
  }
  .mast-date{display:flex;align-items:center;gap:12px;}
  .mast-date .num{
    font-family:"Songti SC","SimSun",serif;font-size:52px;line-height:.9;font-weight:700;
    letter-spacing:-2px;color:var(--ink);
  }
  .mast-date .meta{font-size:12px;color:var(--ink2);line-height:1.5;border-left:1px solid var(--line);padding-left:12px;}
  .mast-date .meta b{display:block;font-size:13px;color:var(--ink);letter-spacing:.5px;}
  .mast-date .meta i{font-style:normal;color:var(--muted);}

  .rule{height:3px;background:var(--ink);margin-top:16px;position:relative;}
  .rule::after{content:"";position:absolute;left:0;bottom:-4px;width:100%;height:1px;background:var(--ink);opacity:.45;}

  /* 滚动快讯 */
  .ticker{
    display:flex;align-items:center;gap:12px;padding:9px 0;margin-top:6px;
    border-bottom:1px solid var(--line);overflow:hidden;
  }
  .live{
    position:relative;z-index:3;flex:0 0 auto;display:flex;align-items:center;gap:6px;
    background:var(--accent);color:#fff;white-space:nowrap;
    font-size:10px;font-weight:700;letter-spacing:1.4px;padding:3px 8px;border-radius:3px;
    box-shadow:0 0 0 6px var(--paper);
  }
  .live b{width:5px;height:5px;border-radius:50%;background:#fff;animation:pulse 1.4s infinite;}
  @keyframes pulse{0%,100%{opacity:1;}50%{opacity:.25;}}
  .ticker-view{
    flex:1 1 auto;min-width:0;overflow:hidden;position:relative;
    -webkit-mask-image:linear-gradient(90deg,transparent 0,#000 14px,#000 calc(100% - 28px),transparent 100%);
    mask-image:linear-gradient(90deg,transparent 0,#000 14px,#000 calc(100% - 28px),transparent 100%);
  }
  .ticker-track{
    display:flex;width:max-content;
    animation:scroll 52s linear infinite;font-size:12.5px;color:var(--ink2);will-change:transform;
  }
  .ticker-group{display:flex;gap:34px;padding-right:34px;}
  .ticker-track span b{color:var(--accent);font-weight:600;margin-right:6px;}
  .ticker:hover .ticker-track{animation-play-state:paused;}
  @keyframes scroll{from{transform:translateX(0);}to{transform:translateX(-50%);}}

  /* ============ 标签栏 ============ */
  .tabs{
    position:sticky;top:0;z-index:60;background:rgba(246,244,239,.93);
    backdrop-filter:saturate(160%) blur(14px);-webkit-backdrop-filter:saturate(160%) blur(14px);
    border-bottom:1px solid var(--line);
  }
  .tabs-inner{
    max-width:1180px;margin:0 auto;padding:0 24px;display:flex;gap:2px;
    overflow-x:auto;scrollbar-width:none;
  }
  .tabs-inner::-webkit-scrollbar{display:none;}
  .tab{
    flex:0 0 auto;border:0;background:none;cursor:pointer;position:relative;
    padding:15px 13px 13px;font-size:14px;color:var(--ink2);font-family:inherit;
    border-bottom:2px solid transparent;transition:color .18s,border-color .18s;
    white-space:nowrap;
  }
  .tab i{font-style:normal;font-size:10px;letter-spacing:.5px;color:var(--muted);margin-right:5px;}
  .tab:hover{color:var(--ink);}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600;}
  .tab.active i{color:var(--accent);opacity:.65;}

  /* ============ 布局 ============ */
  .layout{
    position:relative;z-index:1;max-width:1180px;margin:0 auto;
    padding:26px 24px 56px;display:grid;grid-template-columns:minmax(0,1fr) 322px;gap:30px;
    align-items:start;
  }
  .panel{display:none;}
  .panel.active{display:block;animation:fadeUp .42s cubic-bezier(.2,.7,.3,1) both;}
  @keyframes fadeUp{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:translateY(0);}}

  .panel-head{display:flex;align-items:baseline;gap:12px;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--line);}
  .kicker{font-size:10px;letter-spacing:2.2px;color:var(--accent);font-weight:700;}
  .panel-head h2{
    margin:0;font-family:"Songti SC","SimSun",serif;font-size:21px;font-weight:700;letter-spacing:.5px;
  }
  .panel-head .count{margin-left:auto;font-size:11px;color:var(--muted);letter-spacing:1px;}

  /* 今日焦点条带 */
  .focus-bar{
    display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    background:var(--card);border:1px solid var(--line2);border-left:3px solid var(--accent);
    border-radius:10px;padding:11px 15px;margin-bottom:16px;
  }
  .fb-label{font-size:10px;letter-spacing:2px;color:var(--accent);font-weight:700;flex:0 0 auto;}
  .focus-bar a{display:inline-flex;align-items:baseline;gap:6px;font-size:12.5px;color:var(--ink2);}
  .focus-bar a b{font-weight:600;color:var(--ink);}
  .focus-bar a span{font-size:10px;color:var(--muted);}
  .focus-bar a:hover b{color:var(--accent);}
  .focus-badge{
    display:inline-block;margin-left:8px;font-size:10px;font-weight:600;letter-spacing:.3px;
    color:var(--accent);background:var(--accent-soft);border-radius:3px;padding:1px 6px;
    vertical-align:middle;white-space:nowrap;
  }

  /* 主稿 */
  .lead{
    display:grid;grid-template-columns:45% minmax(0,1fr);gap:0;background:var(--card);
    border:1px solid var(--line2);border-radius:12px;overflow:hidden;
    box-shadow:0 2px 10px rgba(21,23,28,.04);transition:box-shadow .25s,transform .25s;
  }
  .lead:hover{box-shadow:0 14px 34px rgba(21,23,28,.10);transform:translateY(-2px);}
  .lead.no-media{grid-template-columns:1fr;}
  .lead-media{position:relative;min-height:250px;background:var(--chip);overflow:hidden;}
  .lead-media img{transition:transform .7s cubic-bezier(.2,.7,.3,1);}
  .lead:hover .lead-media img{transform:scale(1.04);}
  .lead-media::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 62%,rgba(0,0,0,.28));}
  .lead-body{padding:24px 26px 22px;display:flex;flex-direction:column;justify-content:center;}
  .meta{display:flex;align-items:center;gap:9px;font-size:11px;color:var(--muted);margin-bottom:9px;flex-wrap:wrap;}
  .tag{
    background:var(--accent-soft);color:var(--accent);font-size:10.5px;font-weight:700;
    padding:2px 7px;border-radius:3px;letter-spacing:.6px;
  }
  .lead-body h3{
    margin:0 0 10px;font-family:"Songti SC","SimSun",serif;font-size:25px;line-height:1.35;
    font-weight:700;letter-spacing:.2px;
  }
  .lead-body p{margin:0 0 14px;color:var(--ink2);font-size:13.5px;line-height:1.75;}
  .more{font-size:12px;color:var(--accent);font-weight:600;letter-spacing:.4px;}
  .more span{display:inline-block;transition:transform .2s;}
  .lead:hover .more span{transform:translateX(4px);}

  /* 次级条目 */
  .list{margin-top:8px;}
  .item{
    display:grid;grid-template-columns:minmax(0,1fr) 104px;gap:18px;
    padding:18px 2px;border-bottom:1px solid var(--line2);transition:background .2s;
  }
  .item:last-child{border-bottom:0;}
  .item.no-thumb{grid-template-columns:1fr;}
  .item:hover{background:rgba(255,255,255,.6);}
  .item h4{
    margin:0 0 6px;font-size:16px;font-weight:600;line-height:1.5;letter-spacing:.1px;
  }
  .item p{margin:0;font-size:12.8px;color:var(--ink2);line-height:1.7;}
  .item .meta{margin-bottom:7px;}
  .thumb{height:74px;border-radius:8px;overflow:hidden;background:var(--chip);}
  .thumb img{transition:transform .55s;}
  .item:hover .thumb img{transform:scale(1.06);}

  /* 风险提示 */
  .editor{
    margin-top:22px;padding:18px 20px;background:linear-gradient(135deg,#fbf7ef,#f3eee4);
    border:1px solid rgba(184,137,74,.28);border-radius:10px;position:relative;
  }
  .editor::before{content:"按";position:absolute;right:16px;top:8px;font-family:"Songti SC",serif;font-size:34px;color:rgba(184,137,74,.18);}
  .editor b{display:block;font-size:10.5px;letter-spacing:2px;color:var(--gold);margin-bottom:6px;}
  .editor p{margin:0;font-size:13px;color:#5b5346;line-height:1.8;}

  /* ============ 侧栏 ============ */
  aside{position:sticky;top:64px;display:flex;flex-direction:column;gap:16px;}
  .card{
    background:var(--card);border:1px solid var(--line2);border-radius:12px;padding:18px;
    box-shadow:0 2px 10px rgba(21,23,28,.035);
  }
  .card-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
  .card-h h3{
    margin:0;font-size:12px;letter-spacing:2px;color:var(--ink);font-weight:700;
  }
  .card-h span{font-size:10px;color:var(--muted);letter-spacing:1px;}

  .flash{list-style:none;margin:0;padding:0;}
  .flash li{display:grid;grid-template-columns:66px minmax(0,1fr);gap:9px;padding:9px 0;border-top:1px dashed var(--line2);}
  .flash li:first-child{border-top:0;padding-top:0;}
  .flash time{font-size:10.5px;color:var(--accent);font-variant-numeric:tabular-nums;letter-spacing:.2px;padding-top:2px;}
  .flash p{margin:0;font-size:12.5px;color:var(--ink2);line-height:1.6;}
  .flash a:hover p{color:var(--ink);}
  .live-pill{display:inline-flex;align-items:center;gap:5px;font-size:10px;color:var(--accent);font-weight:600;letter-spacing:.6px;}
  .live-pill i{width:5px;height:5px;border-radius:50%;background:var(--accent);animation:pulse 1.6s infinite;}
  .lead{cursor:pointer;}
  .lead:hover h3{color:var(--accent);}

  .ov{display:flex;flex-direction:column;}
  .ov a{
    display:flex;align-items:center;justify-content:space-between;gap:10px;
    padding:7px 0;border-top:1px dashed var(--line2);font-size:12.5px;color:var(--ink2);
    transition:color .18s;
  }
  .ov a:first-child{border-top:0;}
  .ov a:hover{color:var(--accent);}
  .ov b{font-variant-numeric:tabular-nums;font-weight:600;color:var(--ink);font-size:12px;}
  .ov .dots{flex:1;border-bottom:1px dotted var(--line);margin:0 4px 4px;}

  .hot{list-style:none;margin:0;padding:0;counter-reset:h;}
  .hot li{counter-increment:h;display:flex;gap:10px;padding:8px 0;font-size:12.5px;color:var(--ink2);line-height:1.55;}
  .hot li::before{
    content:counter(h,decimal-leading-zero);font-size:10px;font-weight:700;color:var(--muted);
    font-variant-numeric:tabular-nums;padding-top:2px;min-width:18px;
  }
  .hot li:nth-child(-n+3)::before{color:var(--accent);}
  .hot li:hover{color:var(--ink);}

  /* ============ 页脚 ============ */
  footer{
    position:relative;z-index:1;max-width:1180px;margin:0 auto;padding:0 24px 44px;
    border-top:1px solid var(--line);padding-top:20px;
  }
  .foot-grid{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;font-size:11px;color:var(--muted);line-height:1.9;}
  .foot-grid b{color:var(--ink2);font-weight:600;}
  .foot-note{margin-top:14px;font-size:11px;color:var(--muted);line-height:1.8;}

  @media (max-width:980px){
    .layout{grid-template-columns:1fr;gap:22px;}
    aside{position:static;}
    .lead{grid-template-columns:1fr;}
    .lead-media{min-height:190px;}
    .lead-body{padding:20px;}
    .lead-body h3{font-size:21px;}
    .mast-date .num{font-size:42px;}
  }
  @media (max-width:560px){
    .masthead,.layout,footer{padding-left:16px;padding-right:16px;}
    .tabs-inner{padding:0 10px;}
    .item{grid-template-columns:minmax(0,1fr) 82px;gap:12px;}
    .thumb{height:62px;}
    .brand h1{font-size:19px;}
    .mast-top{gap:12px;}
  }
"""

JS = u"""
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.panel'));

  function activate(key, scroll) {
    var matched = false;
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-tab') === key;
      t.classList.toggle('active', on);
      if (on) { matched = true; }
    });
    if (!matched) { return; }
    panels.forEach(function (p) {
      var on = p.id === 'p-' + key;
      p.classList.toggle('active', on);
      if (on) { p.style.animation = 'none'; void p.offsetWidth; p.style.animation = ''; }
    });
    var cur = document.querySelector('.tab.active');
    if (cur && cur.scrollIntoView) {
      cur.scrollIntoView({ block: 'nearest', inline: 'center' });
    }
    if (scroll && window.scrollTo) {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }

  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      activate(t.getAttribute('data-tab'), false);
      if (history.replaceState) {
        history.replaceState(null, '', '#' + t.getAttribute('data-tab'));
      }
    });
  });

  Array.prototype.slice.call(document.querySelectorAll('[data-goto]')).forEach(function (el) {
    el.addEventListener('click', function (ev) {
      ev.preventDefault();
      activate(el.getAttribute('data-goto'), true);
    });
  });

  // 外链兜底：内嵌浏览器或弹窗拦截时改为当前页跳转
  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('a[target="_blank"]') : null;
    if (!a) { return; }
    var href = a.getAttribute('href');
    if (!href || href === '#') { ev.preventDefault(); return; }
    ev.preventDefault();
    var win = window.open(href, '_blank');
    if (!win) { location.href = href; }
  }, true);

  // 主稿整卡可跳原文
  Array.prototype.slice.call(document.querySelectorAll('.lead[data-href]')).forEach(function (el) {
    el.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('a')) { return; }
      var href = el.getAttribute('data-href');
      if (!href) { return; }
      var win = window.open(href, '_blank');
      if (!win) { location.href = href; }
    });
  });

  // 相对时间实时刷新
  function pad2(n) { return n < 10 ? '0' + n : '' + n; }
  function relTime(ts) {
    var d = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (d < 60) { return '刚刚'; }
    if (d < 3600) { return Math.floor(d / 60) + ' 分钟前'; }
    if (d < 86400) { return Math.floor(d / 3600) + ' 小时前'; }
    var t = new Date(ts * 1000);
    return pad2(t.getMonth() + 1) + '-' + pad2(t.getDate()) + ' ' + pad2(t.getHours()) + ':' + pad2(t.getMinutes());
  }
  function tickTime() {
    Array.prototype.slice.call(document.querySelectorAll('time[data-ts]')).forEach(function (el) {
      var ts = parseInt(el.getAttribute('data-ts'), 10);
      if (ts > 0) { el.textContent = relTime(ts); }
    });
    var stamp = document.getElementById('liveStamp');
    if (stamp) {
      var sts = parseInt(stamp.getAttribute('data-ts'), 10);
      if (sts > 0) { stamp.textContent = '更新于 ' + relTime(sts); }
    }
  }
  tickTime();
  setInterval(tickTime, 30000);

  // 自动检测新版本：云端每 15 分钟构建，页面无需手动刷新
  var lastTag = null, checking = false;
  function reloadKeepState() {
    try {
      sessionStorage.setItem('brief-scroll', String(window.pageYOffset || 0));
      var cur = document.querySelector('.tab.active');
      if (cur) { sessionStorage.setItem('brief-tab', cur.getAttribute('data-tab')); }
    } catch (e) {}
    location.reload();
  }
  function checkUpdate() {
    if (checking) { return; }
    checking = true;
    fetch(location.pathname + '?t=' + Date.now(), { method: 'HEAD', cache: 'no-store' })
      .then(function (r) {
        var tag = r.headers.get('Last-Modified') || r.headers.get('ETag') || r.headers.get('Content-Length');
        if (lastTag === null) { lastTag = tag; }
        else if (tag && lastTag && tag !== lastTag) { reloadKeepState(); }
        checking = false;
      })
      .catch(function () { checking = false; });
  }

  var hash = (location.hash || '').replace('#', '');
  if (hash) { activate(hash, false); }

  var keepScroll = null;
  try {
    keepScroll = sessionStorage.getItem('brief-scroll');
    sessionStorage.removeItem('brief-scroll');
  } catch (e) {}
  if (keepScroll !== null) {
    window.setTimeout(function () { window.scrollTo(0, parseInt(keepScroll, 10) || 0); }, 80);
  }

  if (location.protocol === 'http:' || location.protocol === 'https:') {
    checkUpdate();
    setInterval(checkUpdate, 60000);
  }
})();
"""


def render_item(it, col, index):
    title = esc(it["title"])
    link = it.get("link") or u""
    summary = esc(it.get("summary") or u"")
    source = esc(it.get("source") or u"")
    tag = esc(it.get("tag") or col["tag"])
    img = esc(it.get("image") or u"")
    stamp = esc(fmt_stamp(it.get("ts"), index == 0))
    alt = esc(it["title"][:28])
    badge = u""
    if it.get("focus_count"):
        badge = u'<span class="focus-badge">%d 篇相关报道</span>' % it["focus_count"]
    if index == 0:
        more = (u'<a class="more" href="%s" target="_blank" rel="noopener">阅读原文 <span>&rarr;</span></a>'
                % esc(link)) if link else u''
        media = (u'<div class="lead-media"><img src="%s" alt="%s" loading="lazy"></div>'
                 % (img, alt)) if img else u''
        data_href = (u' data-href="%s"' % esc(link)) if (link and not img) else u""
        return (u'<article class="lead%s"%s>%s'
                u'<div class="lead-body">'
                u'<div class="meta"><span class="tag">%s</span><span>%s</span><span>%s</span></div>'
                u'<h3>%s%s</h3><p>%s</p>%s'
                u'</div></article>') % (
            u"" if img else u" no-media", data_href, media,
            tag, source, stamp, title, badge, summary, more)

    inner = (u'<div class="meta"><span class="tag">%s</span><span>%s</span><span>%s</span></div>'
             u'<h4>%s%s</h4><p>%s</p>') % (tag, source, stamp, title, badge, summary)
    if link:
        inner = u'<a href="%s" target="_blank" rel="noopener">%s</a>' % (esc(link), inner)
    if not img:
        return u'<article class="item no-thumb"><div>%s</div></article>' % inner
    return (u'<article class="item"><div>%s</div>'
            u'<div class="thumb"><img src="%s" alt="%s" loading="lazy"></div></article>'
            % (inner, img, alt))


def render_focus_bar(focus):
    if not focus:
        return u""
    links = u"".join(
        u'<a href="%s" target="_blank" rel="noopener"><b>%s</b><span>%d 篇</span></a>'
        % (esc(f["rep"].get("link") or u"#"), esc(f["keyword"]), f["size"])
        for f in focus)
    return u'<div class="focus-bar"><span class="fb-label">今日焦点</span>%s</div>' % links


def render_panel(col, items, stamp, focus):
    lead = render_item(items[0], col, 0)
    rest = u"".join(render_item(it, col, i + 1) for i, it in enumerate(items[1:]))
    count = u"%d 条 · 更新于 %s" % (len(items), stamp)
    bar = render_focus_bar(focus) if col["key"] == "news" else u""
    note = u""
    if col["key"] == "stock":
        note = (u'<div class="editor"><b>风险提示</b><p>本栏目仅为公开信息汇总，不构成任何投资建议。'
                u'指数、个股相关表述均来自公开报道，交易前请以交易所与券商实时行情为准。</p></div>')
    return (u'<section class="panel%s" id="p-%s">'
            u'<div class="panel-head"><span class="kicker">%s</span><h2>%s</h2>'
            u'<span class="count">%s</span></div>%s%s<div class="list">%s</div>%s</section>') % (
        u" active" if col["key"] == "news" else u"",
        col["key"], esc(col["kicker"]), esc(col["title"]), esc(count), bar, lead, rest, note)


def render_html(columns, latest, counts, stamp, stamp_full, dt, focus, now_ts):
    ticker_items = u"".join(
        u'<span><b>%s</b>%s</span>' % (esc(it.get("col_name") or u"要闻"), esc(it["title"]))
        for it in latest[:6]
    )
    ticker = (u'<div class="ticker-group">%s</div>' % ticker_items) * 2

    tabs = u"".join(
        u'<button class="tab%s" data-tab="%s"><i>%s</i>%s</button>' % (
            u" active" if c["key"] == "news" else u"", c["key"], c["num"], esc(c["name"]))
        for c in COLUMNS
    )

    panels = u"".join(render_panel(c, columns[c["key"]], stamp, focus) for c in COLUMNS)

    flash_items = []
    for c in COLUMNS:
        items = columns[c["key"]]
        if items and items[0].get("ts"):
            flash_items.append(items[0])
    flash_items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    flash = u"".join(
        u'<li><time data-ts="%d">%s</time><p><a href="%s" target="_blank" rel="noopener">%s</a></p></li>'
        % (int(it.get("ts") or 0), esc(relative_time(it.get("ts"), now_ts)),
           esc(it.get("link") or u"#"), esc(it["title"]))
        for it in flash_items[:6]
    )

    overview = u"".join(
        u'<a href="#%s" data-goto="%s"><span>%s</span><span class="dots"></span><b>%d</b></a>'
        % (c["key"], c["key"], esc(c["name"]), counts.get(c["key"], 0))
        for c in COLUMNS
    )

    hot = u"".join(
        u'<li><a href="%s" target="_blank" rel="noopener">%s</a></li>'
        % (esc(it.get("link") or u"#"), esc(it["title"]))
        for it in latest[:6]
    )

    total = sum(counts.get(c["key"], 0) for c in COLUMNS)
    sources = []
    for c in COLUMNS:
        for it in columns[c["key"]]:
            src = it.get("source") or u""
            if src and src not in sources and src != u"编辑部":
                sources.append(src)
    sources = sources[:8]

    html_out = u"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="description" content="每日资讯简报：要闻、军事台海、财经、股票、科技、法律、三农、教育、江苏本地、文化、健康，自动聚合公开信息。" />
<title>飞飞 · 每日资讯简报 | __DATE_DOT__</title>
<style>__CSS__</style>
</head>
<body>

<header class="masthead">
  <div class="mast-top">
    <div class="brand">
      <div class="brand-mark">飞</div>
      <div>
        <h1>飞飞 · 每日资讯简报</h1>
        <p>Daily Briefing</p>
      </div>
    </div>
    <div class="mast-date">
      <div class="num">__DAY__</div>
      <div class="meta">
        <b>__YEAR__年__MONTH__月</b>
        <i>__WEEKDAY__ ｜ 每 15 分钟自动更新</i><br>
        <i id="liveStamp" data-ts="__NOWTS__">更新于 __STAMP__</i>
      </div>
    </div>
  </div>
  <div class="rule"></div>
  <div class="ticker">
    <div class="live"><b></b>快讯</div>
    <div class="ticker-view"><div class="ticker-track">__TICKER__</div></div>
  </div>
</header>

<nav class="tabs">
  <div class="tabs-inner" id="tabs">__TABS__</div>
</nav>

<div class="layout">
  <main>__PANELS__</main>

  <aside>
    <div class="card">
      <div class="card-h"><h3>最新更新</h3><span class="live-pill" id="livePill"><i></i>实时</span></div>
      <ul class="flash">__FLASH__</ul>
    </div>

    <div class="card">
      <div class="card-h"><h3>栏目速览</h3><span>11 栏目</span></div>
      <div class="ov">__OVERVIEW__</div>
    </div>

    <div class="card">
      <div class="card-h"><h3>今日热榜</h3><span>TOP 6</span></div>
      <ul class="hot">__HOT__</ul>
    </div>
  </aside>
</div>

<footer>
  <div class="foot-grid">
    <div><b>飞飞 · 每日资讯简报</b><br>更新于 __STAMP__ ｜ 每 15 分钟自动更新 ｜ 共 11 个栏目 / __TOTAL__ 条</div>
    <div><b>数据来源</b><br>__SOURCES__</div>
  </div>
  <p class="foot-note">说明：本页由定时任务自动构建，内容为公开信息聚合，不构成投资建议。栏目配图为示意图片，文字与链接均指向原始发布页面；如来源更新延迟或暂时不可用，将展示最近一次成功获取的内容。</p>
</footer>

<script>__JS__</script>
</body>
</html>
"""
    reps = [
        (u"__CSS__", CSS),
        (u"__JS__", JS),
        (u"__DATE_DOT__", dt.strftime("%Y.%m.%d")),
        (u"__DAY__", dt.strftime("%d")),
        (u"__YEAR__", dt.strftime("%Y")),
        (u"__MONTH__", str(dt.month)),
        (u"__WEEKDAY__", WEEKDAYS[dt.weekday()]),
        (u"__TICKER__", ticker),
        (u"__TABS__", tabs),
        (u"__PANELS__", panels),
        (u"__FLASH__", flash),
        (u"__OVERVIEW__", overview),
        (u"__HOT__", hot),
        (u"__STAMP__", stamp_full),
        (u"__NOWTS__", str(int(now_ts))),
        (u"__TOTAL__", str(total)),
        (u"__SOURCES__", esc(u" · ".join(sources))),
    ]
    for key, val in reps:
        html_out = html_out.replace(key, val)
    return html_out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    started = time.time()
    dt = now_dt()
    stamp = dt.strftime("%H:%M")
    stamp_full = dt.strftime("%Y-%m-%d %H:%M")

    cache = load_cache()
    failures = []
    used = set()
    columns = {}
    ordered_cols = sorted(COLUMNS, key=lambda c: PROCESS_ORDER.index(c["key"]))
    collected = {}
    link_col = {}
    all_candidates = []
    for col in ordered_cols:
        try:
            items = collect_column(col, failures)
        except Exception:
            print(u"[error] 栏目 %s 抓取异常:" % col["name"])
            traceback.print_exc()
            items = []
        collected[col["key"]] = items
        for it in items:
            norm = _link_of(it)
            if norm and norm not in link_col:
                link_col[norm] = col["key"]
            all_candidates.append(it)

    now_ts = time.time()
    focus = detect_focus(all_candidates, link_col, now_ts)
    if focus:
        print(u"今日焦点: " + u" ｜ ".join(
            u"%s（%d 篇 · %s）" % (f["keyword"], f["size"], f["col"]) for f in focus))
    else:
        print(u"今日焦点: 未识别到明显事件簇")

    for col in ordered_cols:
        try:
            columns[col["key"]] = select_column(
                col, collected[col["key"]], cache, used, focus, now_ts)
        except Exception:
            print(u"[error] 栏目 %s 构建异常:" % col["name"])
            traceback.print_exc()
            columns[col["key"]] = []
            while len(columns[col["key"]]) < MIN_ITEMS:
                columns[col["key"]].append({
                    "title": u"该栏目今日暂无更多可核实的公开信息",
                    "link": u"",
                    "summary": u"数据源暂时不可用，栏目版位保留。稍后会自动重试，也可查看其他栏目了解今日进展。",
                    "ts": 0, "image": "", "source": u"编辑部", "feed": "",
                    "tag": u"占位", "placeholder": True,
                })

    img_cache = load_img_cache()
    og_new, og_total, og_hit = enrich_item_images(columns, img_cache, now_ts)
    save_img_cache(img_cache)
    print(u"原文配图: 待补齐 %d 条链接，本次抓取 %d 条，命中缓存 %d 条" % (og_total, og_new, og_hit))

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    img_dir = prepare_image_dir()
    write_nojekyll()

    # 先按远程地址写入缓存，避免缓存里残留本地相对路径
    save_cache(columns, stamp_full)

    jobs = []
    for col in COLUMNS:
        items = columns[col["key"]] or []
        for it in items:
            url = it.get("image") or u""
            if url:
                jobs.append((url, it.get("link") or u""))

    img_started = time.time()
    mapping = localize_images(jobs, img_dir)
    img_elapsed = time.time() - img_started

    total_unique = 0
    seen_urls = set()
    for url, _ref in jobs:
        if url and url.startswith(u"http") and url not in seen_urls:
            seen_urls.add(url)
            total_unique += 1

    for col in COLUMNS:
        for it in columns[col["key"]]:
            url = it.get("image") or u""
            if url in mapping:
                it["image"] = mapping[url]

    counts = {}
    all_items = []
    img_refs = 0
    img_local = 0
    img_have = 0
    for col in COLUMNS:
        items = columns[col["key"]]
        counts[col["key"]] = len(items)
        for it in items:
            img_refs += 1
            if it.get("image"):
                img_have += 1
            if (it.get("image") or u"").startswith(IMG_DIRNAME + u"/"):
                img_local += 1
            if it.get("placeholder"):
                continue
            item = dict(it)
            item["col_name"] = col["name"]
            all_items.append(item)

    seen = set()
    latest = []
    for it in sorted(all_items, key=lambda x: x.get("ts") or 0, reverse=True):
        if len(latest) >= 6:
            break
        norm = re.sub(r"[?#].*$", "", it.get("link") or u"").rstrip(u"/")
        if not norm or norm in seen:
            continue
        if any(near_dup(it["title"], kept["title"]) for kept in latest):
            continue
        seen.add(norm)
        latest.append(it)
    if len(latest) < 6:
        for it in all_items:
            if len(latest) >= 6:
                break
            if it not in latest:
                latest.append(it)

    html_out = render_html(columns, latest, counts, stamp, stamp_full, dt, focus, now_ts)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    with io.open(OUT_FILE, "w", encoding="utf-8") as fp:
        fp.write(html_out)

    img_files = 0
    img_bytes = 0
    try:
        for name in os.listdir(img_dir):
            p = os.path.join(img_dir, name)
            if os.path.isfile(p):
                img_files += 1
                img_bytes += os.path.getsize(p)
    except Exception:
        pass

    failed_domains = {}
    for url in seen_urls:
        if url in mapping:
            continue
        try:
            dom = urlsplit(url).netloc
        except Exception:
            dom = u"unknown"
        failed_domains[dom] = failed_domains.get(dom, 0) + 1

    size = os.path.getsize(OUT_FILE)
    elapsed = time.time() - started

    print(u"")
    print(u"==== 构建完成 ====")
    print(u"输出文件: %s" % OUT_FILE)
    print(u"文件大小: %d 字节 (%.1f KB)" % (size, size / 1024.0))
    print(u"生成时间: %s" % stamp_full)
    print(u"配图情况: 条目 %d 条，有图 %d 条，无图 %d 条；本地化 %d 条，远程回退 %d 条" % (
        img_refs, img_have, img_refs - img_have, img_local, img_have - img_local))
    print(u"图片本地化: 引用 %d/%d (%.0f%%)，去重后 %d/%d，文件 %d 个，共 %.1f KB，下载耗时 %.1f 秒" % (
        img_local, img_refs,
        (100.0 * img_local / img_refs) if img_refs else 0.0,
        len(mapping), total_unique,
        img_files, img_bytes / 1024.0, img_elapsed))
    if failed_domains:
        detail = u", ".join(u"%s×%d" % (d, n) for d, n in
                            sorted(failed_domains.items(), key=lambda x: x[1], reverse=True))
        print(u"未本地化域名: %s" % detail)
    for col in COLUMNS:
        items = columns[col["key"]]
        srcs = []
        for it in items:
            s = it.get("source") or u""
            if s and s != u"编辑部" and s not in srcs:
                srcs.append(s)
        print(u"  %-2s %-8s %d 条  [%s]" % (col["num"], col["name"], len(items), u" / ".join(srcs)))
    print(u"失败源: %d 个" % len(failures))
    shown = set()
    for name, url, err in failures:
        if url in shown:
            continue
        shown.add(url)
        print(u"  - %s %s (%s)" % (name, url, err))
    print(u"耗时: %.2f 秒" % elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
