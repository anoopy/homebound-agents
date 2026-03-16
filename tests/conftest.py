"""Shared fixtures for homebound tests."""

from __future__ import annotations

import pytest

from homebound.config import HomeboundConfig


@pytest.fixture
def default_config() -> HomeboundConfig:
    """A HomeboundConfig with sensible defaults for testing."""
    return HomeboundConfig()


@pytest.fixture
def config_with_allowlist() -> HomeboundConfig:
    """A HomeboundConfig with an allowlist for security tests."""
    from homebound.config import SecurityConfig

    config = HomeboundConfig()
    config.security = SecurityConfig(allowed_users=["WFAKE_ADMIN"])
    return config
