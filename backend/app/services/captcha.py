"""图形验证码（登录防自动化爆破）。

实现说明（给新手）：
- 用 Pillow 画一张 140x44 的图：4 个字符，每个带随机旋转/颜色，加干扰线和噪点。
- 字符集去掉了 0/O、1/l/I 这类肉眼难分辨的，减少"看错了赖系统"的投诉。
- 校验时统一转小写——**不区分大小写**是产品要求。
- 存储：优先 Redis（多副本部署时 A 副本出的码 B 副本也能校验），
  Redis 不可用则退回本进程内存（单机开发/测试）。一次一用防重放；
  内存兜底有上限，防被恶意请求刷爆。
- 10-4 多副本部署实测：纯内存存储在 Nginx 轮询下必然 E6005（取码在 A、登录落 B），
  这就是本模块改为 Redis 共享的原因。
"""
from __future__ import annotations

import base64
import io
import logging
import random
import secrets
import time

from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings

logger = logging.getLogger(__name__)

_CHAR_POOL = "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉 0/o、1/l/i 等易混字符
_CODE_LEN = 4
_TTL_SECONDS = 300  # 5 分钟
_MAX_STORED = 5000  # 内存保护上限

_store: dict[str, tuple[str, float]] = {}
_KEY_PREFIX = "captcha:"

_redis_client = None
_redis_failed = False


def _redis():
    """惰性连接 Redis；失败一次就记住，后续直接走内存兜底（不反复重试拖慢登录）。"""
    global _redis_client, _redis_failed
    if _redis_client is not None or _redis_failed:
        return _redis_client
    try:
        import redis

        client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=0.3, socket_timeout=0.5,
        )
        client.ping()
        _redis_client = client
    except Exception as exc:  # noqa: BLE001 —— 任何 Redis 问题都退化为内存
        _redis_failed = True
        logger.warning("验证码改用内存存储（Redis 不可用：%s）", exc)
    return _redis_client

# 备选字体：Linux 容器装 fonts-dejavu-core，Windows 是 arial/微软雅黑
_FONT_CANDIDATES = ("DejaVuSans.ttf", "arial.ttf", "segoeui.ttf", "msyh.ttc")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    # 兜底：slim 镜像可能零字体。Pillow 10.1+ 的 load_default 支持指定字号；
    # 更老版本不支持 size 参数，退回无参调用（字小但至少能用）。
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _cleanup_locked() -> None:
    """过期清理 + 容量上限（超出时丢最旧的）。无锁：CPython dict 操作原子，够用。"""
    now = time.time()
    expired = [k for k, (_, exp) in _store.items() if exp < now]
    for k in expired:
        _store.pop(k, None)
    while len(_store) > _MAX_STORED:
        oldest = min(_store.items(), key=lambda kv: kv[1][1])[0]
        _store.pop(oldest, None)


def _render_png(text: str) -> bytes:
    """把 4 个字符画成 PNG：随机颜色、随机上下抖动与旋转感、干扰线 + 噪点。"""
    rng = random.SystemRandom()
    width, height = 140, 44
    img = Image.new("RGB", (width, height), (rng.randint(235, 245), rng.randint(238, 246), rng.randint(245, 252)))
    draw = ImageDraw.Draw(img)
    # 字号 32：用户反馈 26 号在前端 116px 容器里缩放后仅约 21px，偏小难认。
    # 现在前端按 140x44 原尺寸展示，32 号字显示效果约 32px。
    font = _load_font(32)

    # 干扰线（3 条淡色弧线感直线）
    for _ in range(3):
        x1, y1 = rng.randint(0, 30), rng.randint(6, height - 6)
        x2, y2 = rng.randint(width - 30, width), rng.randint(6, height - 6)
        draw.line([(x1, y1), (x2, y2)], fill=(rng.randint(170, 210), rng.randint(180, 215), rng.randint(195, 230)), width=1)

    # 逐字符绘制：左右均分，上下抖动，字符颜色用深色系保证可读
    slot = width / _CODE_LEN
    for i, ch in enumerate(text):
        color = (rng.randint(20, 90), rng.randint(40, 110), rng.randint(60, 140))
        tmp = Image.new("RGBA", (46, 44), (0, 0, 0, 0))
        tmp_draw = ImageDraw.Draw(tmp)
        tmp_draw.text((4, rng.randint(-2, 6)), ch, font=font, fill=color + (255,))
        tmp = tmp.rotate(rng.randint(-22, 22), expand=False, resample=Image.BICUBIC)
        img.paste(tmp, (int(i * slot) + rng.randint(0, 4), 0), tmp)

    # 噪点
    for _ in range(120):
        x, y = rng.randint(0, width - 1), rng.randint(0, height - 1)
        img.putpixel((x, y), (rng.randint(120, 220), rng.randint(130, 220), rng.randint(140, 225)))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def new_captcha() -> tuple[str, str, str]:
    """生成一张验证码。返回 (captcha_id, data URI, 答案原文)。

    答案只在 dev 环境由 API 层回显给本地脚本；无论如何它都不会进入图片本身。
    """
    text = "".join(secrets.choice(_CHAR_POOL) for _ in range(_CODE_LEN))
    captcha_id = secrets.token_urlsafe(16)
    client = _redis()
    if client is not None:
        try:
            client.setex(_KEY_PREFIX + captcha_id, _TTL_SECONDS, text.lower())
        except Exception:  # noqa: BLE001
            logger.warning("验证码写入 Redis 失败，本次落内存")
            client = None
    if client is None:
        _cleanup_locked()
        _store[captcha_id] = (text.lower(), time.time() + _TTL_SECONDS)
    png = _render_png(text)
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return captcha_id, data_uri, text


def verify_captcha(captcha_id: str | None, text: str | None) -> bool:
    """校验。开关关闭时直接放行（离线评测 / 自动化测试用）。一次一用，失败也作废。"""
    if not get_settings().login_captcha_enabled:
        return True
    if not captcha_id or not text:
        return False

    client = _redis()
    answer = None
    if client is not None:
        try:
            raw = client.getdel(_KEY_PREFIX + captcha_id)  # 原子取+删：多副本下也不会被重放
            answer = raw.decode() if raw else None
        except Exception:  # noqa: BLE001
            logger.warning("验证码读取 Redis 失败，退回内存校验")
            client = None
    if client is None:
        item = _store.pop(captcha_id, None)
        if item is None:
            return False
        stored, expires = item
        if expires < time.time():
            return False
        answer = stored

    if not answer:
        return False
    return text.strip().lower() == answer
