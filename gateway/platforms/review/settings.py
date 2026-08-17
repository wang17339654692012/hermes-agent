"""review 模块配置解析 — config.yaml 优先，env 兜底。

API key 属于 secret，只从 env 读取；URL/model 属于行为设置，
config.yaml 的 review 节优先，env 兜底兼容既有部署。
"""

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

_DEFAULT_LLM_URL = "https://api.deepseek.com/v1/chat/completions"
_DEFAULT_LLM_MODEL = "deepseek-chat"


def resolve_llm_settings() -> Tuple[str, str]:
    """返回 (api_url, model)。

    优先级：config.yaml gateway.review 节 > env（LLM_API_URL/LLM_MODEL）> DeepSeek 默认值。
    """
    api_url = os.getenv("LLM_API_URL", _DEFAULT_LLM_URL)
    model = os.getenv("LLM_MODEL", _DEFAULT_LLM_MODEL)

    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        cfg = load_config_readonly()
        api_url = cfg_get(cfg, "gateway", "review", "llm_url", default=api_url)
        model = cfg_get(cfg, "gateway", "review", "llm_model", default=model)
    except Exception as e:  # config 缺失/损坏时回退 env/默认值，不阻断审核
        logger.warning("读取 review 配置失败，使用 env/默认值: %s", e)
    return api_url, model
