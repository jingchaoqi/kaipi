"""Refresh the maintained model/price catalog without changing endpoint credentials."""

from __future__ import annotations

import json
import math
import os
import tempfile
import tomllib

import httpx

from kaipi import ledger, providers

URL = "https://raw.githubusercontent.com/jingchaoqi/kaipi/master/kaipi/pricing.toml"


def fetch(name: str) -> dict[str, ledger.Price]:
    response = httpx.get(URL, timeout=10)
    response.raise_for_status()
    raw = tomllib.loads(response.text)
    rows = raw.get("models")
    if not isinstance(rows, dict) or not all(isinstance(row, dict) for row in rows.values()):
        raise ValueError("Invalid model catalog")
    shared = providers.BUILTIN[name].model_catalog or name
    prices = {
        key: ledger.Price.model_validate(value)
        for key, value in rows.items()
        if value.get("provider") in {name, shared}
    }
    if not prices:
        raise ValueError("No models in the online catalog")
    for price in prices.values():
        if price.context < 0 or any(
            not math.isfinite(rate) or rate < 0
            for rate in (price.input, price.output, price.cache_read, price.cache_write)
        ):
            raise ValueError("Invalid model price")
    return prices


def model_ids(prices: dict[str, ledger.Price]) -> list[str]:
    return [key.removeprefix(f"{price.provider}/") for key, price in prices.items()]


def save(name: str, models: list[str], prices: dict[str, ledger.Price]) -> None:
    path = ledger.catalog_path()
    before = path.read_bytes() if path.exists() else None
    merged = ledger.catalog_prices() | prices
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({key: price.model_dump() for key, price in merged.items()}, stream)
        os.replace(temporary, path)
        try:
            providers.save_auth(name, models=models)
        except BaseException:
            if before is None:
                path.unlink()
            else:
                path.write_bytes(before)
            raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def preview(name: str, saved_models: list[str]) -> tuple[dict[str, ledger.Price], list[str]]:
    """Prepare a refresh without saving it; interfaces decide when to confirm."""
    prices = fetch(name)
    return prices, list(dict.fromkeys([*saved_models, *model_ids(prices)]))
