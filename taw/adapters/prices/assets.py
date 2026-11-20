"""Which priced asset a transfer moves, or None when no feed covers it.

Only an issuer's own token is priced with its feed. Bridged copies issued by
someone else (USDC.e on Arbitrum and OP Mainnet) get no price. USDT0 on
Arbitrum is issued by Tether, so it is priced as USDT. DAI's contracts were read
on-chain on 2026-11-20 (symbol "DAI", 18 decimals); the stablecoin contracts are
the ones in evm_etherscan.CHAINS and tron_trongrid.STABLECOINS.
"""
from __future__ import annotations

from taw.adapters.base import TransferRecord

NATIVE = {"eth": "ETH", "arbitrum": "ETH", "optimism": "ETH", "tron": "TRX", "btc": "BTC"}

TOKENS = {
    ("eth", "0xdac17f958d2ee523a2206206994597c13d831ec7"): "USDT",
    ("eth", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"): "USDC",
    ("eth", "0x6b175474e89094c44da98b954eedeac495271d0f"): "DAI",
    ("arbitrum", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"): "USDT",
    ("arbitrum", "0xaf88d065e77c8cc2239327c5edb3a432268e5831"): "USDC",
    ("arbitrum", "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1"): "DAI",
    ("optimism", "0x0b2c639c533813f4aa9d7837caf62653d097ff85"): "USDC",
    ("optimism", "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1"): "DAI",
    ("tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"): "USDT",
    ("tron", "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"): "USDC",
}


def asset_of(record: TransferRecord) -> str | None:
    if record.token_contract is None:
        return NATIVE.get(record.chain)
    contract = record.token_contract if record.chain == "tron" else record.token_contract.lower()
    return TOKENS.get((record.chain, contract))
