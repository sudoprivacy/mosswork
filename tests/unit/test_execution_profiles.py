import pytest

from mosswork.infra.execution.profiles import model_concurrency, profile


@pytest.mark.parametrize("resource_profile", ["4g", "8g", "16g"])
def test_model_concurrency_is_independent_of_host_profile(monkeypatch, resource_profile):
    monkeypatch.setenv("MOSSWORK_RESOURCE_PROFILE", resource_profile)
    monkeypatch.delenv("MOSSWORK_MODEL_CONCURRENCY", raising=False)
    assert profile().running > 0
    assert model_concurrency() == 0
    monkeypatch.setenv("MOSSWORK_MODEL_CONCURRENCY", "1000")
    assert model_concurrency() == 1000
    monkeypatch.setenv("MOSSWORK_MODEL_CONCURRENCY", "0")
    assert model_concurrency() == 0


@pytest.mark.parametrize("value", ["-1", "1.5", "", "invalid"])
def test_invalid_model_concurrency_fails_explicitly(monkeypatch, value):
    monkeypatch.setenv("MOSSWORK_MODEL_CONCURRENCY", value)
    with pytest.raises(ValueError, match="MOSSWORK_MODEL_CONCURRENCY"):
        model_concurrency()
