"""TenantRegistry env parsing."""

import pytest

from matrix_mcp.client import TenantRegistry
from tests.conftest import ENV_ONE_TENANT, ENV_TWO_TENANTS


def test_single_tenant_parsed():
    reg = TenantRegistry(ENV_ONE_TENANT)
    assert reg.names() == ["main"]
    cfg = reg.get("main").cfg
    assert cfg.base_url == "https://matrix.example.org"
    assert cfg.user_id == "@alice:example.org"
    assert cfg.token == "sekrit-matrix-token"


def test_two_tenants_parsed_sorted():
    reg = TenantRegistry(ENV_TWO_TENANTS)
    assert reg.names() == ["main", "work"]
    assert reg.get("work").cfg.user_id == "@alice:work.example"


def test_tenant_name_lowercased():
    env = {
        "TENANT_MyServer_BASE_URL": "https://hs.example",
        "TENANT_MyServer_USER_ID": "@a:hs.example",
        "TENANT_MyServer_TOKEN": "t",
    }
    reg = TenantRegistry(env)
    assert reg.names() == ["myserver"]


def test_base_url_trailing_slash_stripped():
    env = dict(ENV_ONE_TENANT, TENANT_MAIN_BASE_URL="https://matrix.example.org/")
    reg = TenantRegistry(env)
    assert reg.get("main").cfg.base_url == "https://matrix.example.org"


def test_missing_field_raises():
    env = {k: v for k, v in ENV_ONE_TENANT.items() if not k.endswith("_TOKEN")}
    with pytest.raises(RuntimeError, match="missing required env.*token"):
        TenantRegistry(env)


def test_no_tenants_raises():
    with pytest.raises(RuntimeError, match="No tenants configured"):
        TenantRegistry({"UNRELATED": "x"})


def test_unrelated_env_ignored():
    env = dict(ENV_ONE_TENANT, PATH="/usr/bin", TENANTLESS_THING="x")
    assert TenantRegistry(env).names() == ["main"]
