"""Tests for review settings — config.yaml 优先，env 兜底。"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.settings import resolve_llm_settings


class TestResolveLlmSettings:
    """resolve_llm_settings tests — 行为设置走 config.yaml，secret 走 env。"""

    def test_config_takes_precedence_over_env(self):
        """config.yaml 的 gateway.review 节优先于 env。"""
        with patch(
            "hermes_cli.config.load_config_readonly",
            return_value={
                "gateway": {
                    "review": {"llm_url": "http://cfg-url", "llm_model": "cfg-model"}
                }
            },
        ), patch.dict(
            "os.environ", {"LLM_API_URL": "http://env-url", "LLM_MODEL": "env-model"}
        ):
            url, model = resolve_llm_settings()
            assert url == "http://cfg-url"
            assert model == "cfg-model"

    def test_env_fallback_when_config_absent(self):
        """config 无 gateway.review 节时回退 env（兼容既有部署）。"""
        with patch(
            "hermes_cli.config.load_config_readonly", return_value={"gateway": {}}
        ), patch.dict(
            "os.environ", {"LLM_API_URL": "http://env-url", "LLM_MODEL": "env-model"}
        ):
            url, model = resolve_llm_settings()
            assert url == "http://env-url"
            assert model == "env-model"

    def test_env_fallback_not_shadowed_by_defaults(self):
        """回归：DEFAULT_CONFIG 合并后无 review 节，env 回退不被默认值遮蔽。"""
        from copy import deepcopy

        from hermes_cli.config_defaults import DEFAULT_CONFIG

        cfg = deepcopy(DEFAULT_CONFIG)
        assert "review" not in cfg.get("gateway", {})

        with patch(
            "hermes_cli.config.load_config_readonly", return_value=cfg
        ), patch.dict(
            "os.environ", {"LLM_API_URL": "http://env-url", "LLM_MODEL": "env-model"}
        ):
            url, model = resolve_llm_settings()
            assert url == "http://env-url"
            assert model == "env-model"

    def test_defaults_when_neither_set(self):
        """config 与 env 均未设置时使用 DeepSeek 默认值。"""
        with patch(
            "hermes_cli.config.load_config_readonly", return_value={"gateway": {}}
        ), patch.dict("os.environ", {}, clear=True):
            url, model = resolve_llm_settings()
            assert url == "https://api.deepseek.com/v1/chat/completions"
            assert model == "deepseek-chat"

    def test_config_load_failure_falls_back_to_env(self):
        """config 加载异常不阻断审核流程，回退 env。"""
        with patch(
            "hermes_cli.config.load_config_readonly", side_effect=RuntimeError("boom")
        ), patch.dict("os.environ", {"LLM_MODEL": "env-model"}):
            url, model = resolve_llm_settings()
            assert model == "env-model"
