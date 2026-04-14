from __future__ import annotations

from app.services.llm.client import LLMError, LLMRateLimitError


def test_llm_error_status_code_defaults_to_none() -> None:
    exc = LLMError("boom")
    assert exc.status_code is None


def test_llm_error_accepts_status_code_kwarg() -> None:
    exc = LLMError("upstream 503", status_code=503)
    assert exc.status_code == 503


def test_rate_limit_error_is_llm_error() -> None:
    exc = LLMRateLimitError("429", retry_after=5.0)
    assert isinstance(exc, LLMError)
    assert exc.retry_after == 5.0
