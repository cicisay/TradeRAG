"""翻译服务（对应 PRD FR-202a 查询多语变体 / FR-202b 翻译副本）。

给新手的说明：
- 中文提问想查到英文/西语/阿语资料，两条路：bge-m3 天然跨语言（一路），
  把问题翻译成英/西/阿再用各语种检索（另一路）。本模块提供第二路的翻译能力。
- 翻译复用问答的 LLM 调用链（管理后台配置的当前主用模型），不再绑定 Qwen——
  只要配了任意可用厂商，多语变体这条路就是开的。
- 链路不可用时返回空变体 + degraded 标记（PRD 降级原则：
  翻译不可用 → 仅靠跨语言向量单路召回，并提示用户）。
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.services.llm import get_llm_client

logger = logging.getLogger(__name__)

_TARGET_LANG_NAMES = {"en": "English", "es": "Spanish", "ar": "Arabic"}


class Translator:
    def translate(self, text: str, target_lang: str, db: Session | None = None) -> str | None:
        """把 text 翻译成 target_lang；失败返回 None（调用方降级）。"""
        lang_name = _TARGET_LANG_NAMES.get(target_lang)
        if not lang_name:
            return None
        client = get_llm_client(db)
        if not client.available:
            return None
        try:
            return client.chat_once(
                [
                    {"role": "system", "content": "你是外贸领域专业翻译，只输出译文，不要解释。"},
                    {"role": "user", "content": f"把下面内容翻译成{lang_name}：\n{text}"},
                ],
                temperature=0.1,
                max_tokens=300,
                db=db,
            ).strip()
        except Exception:  # noqa: BLE001
            logger.warning("翻译失败 target=%s，降级单路检索", target_lang, exc_info=True)
            return None

    def query_variants(
        self, query: str, target_langs: tuple[str, ...] = ("en", "es", "ar"), db: Session | None = None
    ) -> tuple[dict[str, str], bool]:
        """生成多语种查询变体。

        返回 (variants, degraded)：variants={"en": "...", ...}；degraded=True 表示翻译不可用/部分失败。
        """
        client = get_llm_client(db)
        if not client.available:
            # 未配置模型 = 未尝试，不算"降级失败"（否则开发环境降级率恒 100%，指标失真）
            return {}, False

        # 10-4 性能修复：原实现三语串行（每次约 1.4s，合计 4s+ 卡在检索前）。
        # 改为并发三路；注意 DB session 不能跨线程复用——client 在主线程取好，
        # 线程内调用不传 db（翻译不需要写审计/用量表）。
        def _one(lang: str) -> tuple[str, str | None]:
            lang_name = _TARGET_LANG_NAMES.get(lang)
            if not lang_name:
                return lang, None
            try:
                out = client.chat_once(
                    [
                        {"role": "system", "content": "你是外贸领域专业翻译，只输出译文，不要解释。"},
                        {"role": "user", "content": f"把下面内容翻译成{lang_name}：\n{query}"},
                    ],
                    temperature=0.1,
                    max_tokens=300,
                )
                return lang, (out or "").strip() or None
            except Exception:  # noqa: BLE001
                logger.warning("翻译失败 target=%s，降级单路检索", lang, exc_info=True)
                return lang, None

        variants: dict[str, str] = {}
        degraded = False
        with ThreadPoolExecutor(max_workers=max(1, len(target_langs))) as pool:
            for lang, translated in pool.map(_one, target_langs):
                if translated:
                    variants[lang] = translated
                else:
                    degraded = True
        return variants, degraded


_translator: Translator | None = None


def get_translator() -> Translator:
    global _translator
    if _translator is None:
        _translator = Translator()
    return _translator


def reset_translator() -> None:
    global _translator
    _translator = None
