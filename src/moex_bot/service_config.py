from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

PROD_GRPC = "invest-public-api.tbank.ru:443"
SANDBOX_GRPC = "sandbox-invest-public-api.tbank.ru:443"
PROD_REST = "https://invest-public-api.tbank.ru/rest"
SANDBOX_REST = "https://sandbox-invest-public-api.tbank.ru/rest"
PROD_WEBSOCKET = "wss://invest-public-api.tbank.ru/ws/"
MOEX_ISS = "https://iss.moex.com/iss"
MOEX_ALGOPACK = "https://apim.moex.com/iss"
TELEGRAM_API = "https://api.telegram.org"


class TInvestEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PROD = "prod"


@dataclass(frozen=True, slots=True)
class TInvestServiceConfig:
    prod_grpc: str
    sandbox_grpc: str
    prod_rest: str
    sandbox_rest: str
    prod_websocket: str


@dataclass(frozen=True, slots=True)
class ExternalServiceConfig:
    t_invest: TInvestServiceConfig
    moex_iss: str
    moex_algopack: str
    telegram_api: str


@dataclass(frozen=True, slots=True)
class TInvestRuntimeConfig:
    environment: TInvestEnvironment
    grpc_endpoint: str
    rest_endpoint: str
    token_env: str
    account_id_env: str
    token: str
    account_id: str
    live_orders_enabled: bool = False


def resolve_tinvest_runtime(
    services: ExternalServiceConfig,
    *,
    environment: TInvestEnvironment,
) -> TInvestRuntimeConfig:
    if environment is TInvestEnvironment.SANDBOX:
        token_env = "T_INVEST_SANDBOX_TOKEN"
        account_env = "T_INVEST_SANDBOX_ACCOUNT_ID"
        grpc = services.t_invest.sandbox_grpc
        rest = services.t_invest.sandbox_rest
    else:
        token_env = "T_INVEST_PROD_TOKEN"
        account_env = "T_INVEST_PROD_ACCOUNT_ID"
        grpc = services.t_invest.prod_grpc
        rest = services.t_invest.prod_rest
    token = os.getenv(token_env, "").strip()
    account_id = os.getenv(account_env, "").strip()
    if bool(token) != bool(account_id):
        raise ValueError(f"{environment.value} token and account id must be configured together")
    return TInvestRuntimeConfig(
        environment=environment,
        grpc_endpoint=grpc,
        rest_endpoint=rest,
        token_env=token_env,
        account_id_env=account_env,
        token=token,
        account_id=account_id,
        live_orders_enabled=False,
    )


def load_service_config(path: Path) -> ExternalServiceConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    tinvest = raw["t_invest"]
    config = ExternalServiceConfig(
        t_invest=TInvestServiceConfig(
            prod_grpc=str(tinvest["prod_grpc"]),
            sandbox_grpc=str(tinvest["sandbox_grpc"]),
            prod_rest=str(tinvest["prod_rest"]),
            sandbox_rest=str(tinvest["sandbox_rest"]),
            prod_websocket=str(tinvest["prod_websocket"]),
        ),
        moex_iss=str(raw["moex"]["iss"]),
        moex_algopack=str(raw["moex"]["algopack"]),
        telegram_api=str(raw["telegram"]["api"]),
    )
    _require_exact(config.t_invest.prod_grpc, PROD_GRPC, "T-Invest prod gRPC")
    _require_exact(config.t_invest.sandbox_grpc, SANDBOX_GRPC, "T-Invest sandbox gRPC")
    _require_url(config.t_invest.prod_rest, PROD_REST, "T-Invest prod REST")
    _require_url(config.t_invest.sandbox_rest, SANDBOX_REST, "T-Invest sandbox REST")
    _require_url(config.t_invest.prod_websocket, PROD_WEBSOCKET, "T-Invest prod WebSocket")
    _require_url(config.moex_iss, MOEX_ISS, "MOEX ISS")
    _require_url(config.moex_algopack, MOEX_ALGOPACK, "MOEX ALGOPACK")
    _require_url(config.telegram_api, TELEGRAM_API, "Telegram API")
    return config


def _require_exact(value: str, expected: str, label: str) -> None:
    if value.rstrip("/") != expected.rstrip("/"):
        raise ValueError(f"{label} must use the approved official endpoint")


def _require_url(value: str, expected: str, label: str) -> None:
    parsed = urlparse(value)
    approved = urlparse(expected)
    if parsed.scheme != approved.scheme or parsed.hostname != approved.hostname:
        raise ValueError(f"{label} must use the approved official host")
    if value.rstrip("/") != expected.rstrip("/"):
        raise ValueError(f"{label} path must match the approved endpoint")
