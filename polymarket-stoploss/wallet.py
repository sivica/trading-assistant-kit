"""Bot wallet: key generation/loading, Deposit Wallet, authenticated Polymarket clients, approvals.

The private key never leaves this server and is never printed or logged.
"""
from __future__ import annotations

import dataclasses
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

import config as cfg
import pmapi


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def has_wallet() -> bool:
    return cfg.KEY_FILE.exists()


def create_wallet() -> str:
    """Generate a brand-new wallet on this machine. Returns the public address."""
    if cfg.KEY_FILE.exists():
        raise RuntimeError(f"{cfg.KEY_FILE} already exists; refusing to overwrite your wallet")
    from eth_account import Account
    acct = Account.create()
    _write_secret(cfg.KEY_FILE, acct.key.hex())
    cfg.ADDRESS_FILE.write_text(acct.address + "\n")
    return acct.address


def address() -> str:
    if cfg.ADDRESS_FILE.exists():
        return cfg.ADDRESS_FILE.read_text().strip()
    if not cfg.KEY_FILE.exists():
        raise RuntimeError("no bot wallet yet; run `pmbot setup` first")
    from eth_account import Account
    a = Account.from_key(_key()).address
    cfg.ADDRESS_FILE.write_text(a + "\n")
    return a


def _key() -> str:
    st = cfg.KEY_FILE.stat()
    if st.st_mode & 0o077:
        os.chmod(cfg.KEY_FILE, 0o600)
    k = cfg.KEY_FILE.read_text().strip()
    return k if k.startswith("0x") else "0x" + k


def _environment(rpc_url: str):
    from polymarket.environments import PRODUCTION
    try:
        from polymarket.environments import _create_environment
        return _create_environment(name="production",
                                   config=dataclasses.replace(PRODUCTION._config, rpc_url=rpc_url))
    except Exception:
        return PRODUCTION


def _load_creds():
    from polymarket import ApiKeyCreds
    if not cfg.CREDS_FILE.exists():
        return None
    try:
        d = json.loads(cfg.CREDS_FILE.read_text())
        return ApiKeyCreds(**d)
    except Exception:
        return None  # re-derive


def _save_creds(c) -> None:
    d = {"apiKey": c.key, "secret": c.secret, "passphrase": c.passphrase}
    if cfg.CREDS_FILE.exists():
        cfg.CREDS_FILE.unlink()
    _write_secret(cfg.CREDS_FILE, json.dumps(d))


def signer_client(conf: "cfg.Config"):
    """Authenticated SecureClient acting for the bot's own key (EOA). Used to move money that
    sits on the EOA itself (gas-paid, needs POL). Derives CLOB API creds once."""
    from polymarket import SecureClient
    creds = _load_creds()
    c = SecureClient.create(private_key=_key(), wallet=address(),
                            environment=_environment(conf.rpc_urls[0]), credentials=creds)
    if creds is None:
        _save_creds(c.credentials)
    return c


# ---------- Deposit Wallet (Polymarket's smart wallet; required for new accounts) ----------
# Since 2026-05-03 the CLOB only accepts orders from a plain EOA if that EOA traded before then
# ("maker address not allowed, please use the deposit wallet flow"). New accounts trade through a
# Deposit Wallet: a smart wallet owned by the bot key, deployed and operated gaslessly through
# Polymarket's relayer, which needs a Builder API key. Docs:
#   https://docs.polymarket.com/trading/wallets-auth   (Create New Accounts / Deposit Wallet)

def load_builder_key():
    from polymarket import BuilderApiKey
    if not cfg.BUILDER_KEY_FILE.exists():
        return None
    try:
        d = json.loads(cfg.BUILDER_KEY_FILE.read_text())
        return BuilderApiKey(key=d["key"], secret=d["secret"], passphrase=d["passphrase"])
    except Exception:
        return None


def save_builder_key(key: str, secret: str, passphrase: str) -> None:
    key, secret, passphrase = key.strip(), secret.strip(), passphrase.strip()
    if not (key and secret and passphrase):
        raise ValueError("key, secret and passphrase are all required")
    if cfg.BUILDER_KEY_FILE.exists():
        cfg.BUILDER_KEY_FILE.unlink()
    _write_secret(cfg.BUILDER_KEY_FILE, json.dumps({"key": key, "secret": secret, "passphrase": passphrase}))


def try_create_builder_key(conf: "cfg.Config") -> bool:
    """Ask the CLOB for a Builder API key owned by the bot key itself (no website needed).
    Returns False if Polymarket refuses; then the user creates one on polymarket.com."""
    try:
        b = signer_client(conf).create_builder_api_key()
    except Exception as e:  # noqa: BLE001
        log_msg = f"{type(e).__name__}: {str(e)[:200]}"
        print("  Polymarket did not create a builder key automatically:", log_msg)
        return False
    save_builder_key(b.key, b.secret, b.passphrase)
    return True


def predicted_deposit_wallet() -> str:
    """Address the bot's Deposit Wallet has (or will have once deployed). Offline, deterministic."""
    from eth_utils import to_checksum_address
    from polymarket._internal.environment import get_environment_config
    from polymarket._internal.wallet import derive_beacon_deposit_wallet_address
    from polymarket.environments import PRODUCTION
    wd = get_environment_config(PRODUCTION).wallet_derivation
    return derive_beacon_deposit_wallet_address(to_checksum_address(address()), wd)


def deposit_wallet_address() -> Optional[str]:
    """The deployed Deposit Wallet recorded by `pmbot setup`, or None if not set up yet."""
    if cfg.DEPOSIT_WALLET_FILE.exists():
        a = cfg.DEPOSIT_WALLET_FILE.read_text().strip()
        return a or None
    return None


def trading_address(conf: "cfg.Config") -> str:
    """Address that holds pUSD and positions and places orders (what the stop-loss watches)."""
    if not conf.use_deposit_wallet:
        return address()
    dw = deposit_wallet_address()
    if not dw:
        raise RuntimeError("Deposit Wallet not set up yet; run `pmbot setup`")
    return dw


def deposit_client(conf: "cfg.Config", deploy: bool = False):
    """SecureClient acting for the Deposit Wallet (orders signed by the bot key, signature type 3).
    With deploy=True the SDK deploys the wallet through the relayer if needed (gasless; needs the
    builder key) and the address is recorded."""
    from polymarket import SecureClient
    creds = _load_creds()
    bkey = load_builder_key()
    wallet_addr = None if deploy else deposit_wallet_address()
    if deploy and bkey is None:
        raise RuntimeError("no Builder API key yet; run `pmbot setup` (or `pmbot set-builder-key`)")
    if not deploy and wallet_addr is None:
        raise RuntimeError("Deposit Wallet not set up yet; run `pmbot setup`")
    c = SecureClient.create(private_key=_key(), wallet=wallet_addr,
                            environment=_environment(conf.rpc_urls[0]), credentials=creds,
                            api_key=bkey)
    if creds is None:
        _save_creds(c.credentials)
    if c.wallet_type != "DEPOSIT_WALLET":
        raise RuntimeError(f"expected a Deposit Wallet, SDK resolved {c.wallet_type} {c.wallet}")
    if deploy:
        cfg.DEPOSIT_WALLET_FILE.write_text(str(c.wallet) + "\n")
    return c


def client(conf: "cfg.Config"):
    """Client used for trading (buy/sell/cancel) and for the stop-loss loop."""
    return deposit_client(conf) if conf.use_deposit_wallet else signer_client(conf)


def wait_tx(handle, what: str) -> None:
    """Wait for a transaction; public RPCs sometimes fail the receipt lookup even though the
    transaction went through, so a failed wait is reported, not fatal. Callers re-check balances."""
    try:
        handle.wait()
    except Exception as e:  # noqa: BLE001
        print(f"  ({what}: sent, but could not confirm yet: {type(e).__name__}. Re-checking balances.)")


def move_pusd_to_deposit_wallet(conf: "cfg.Config", amount: Decimal) -> None:
    """Send pUSD from the bot key's own address (EOA) into its Deposit Wallet. Costs a little POL."""
    dw = trading_address(conf)
    h = signer_client(conf).transfer_erc20(token_address=pmapi.PUSD, recipient_address=dw,
                                           amount=int(amount * 10**6))
    wait_tx(h, "pUSD transfer")


def setup_approvals(c) -> None:
    """One-time approvals so the exchange can move pUSD and outcome shares. Already-granted
    approvals are skipped. Deposit Wallet: gasless via the relayer. EOA: costs a little POL."""
    h = c.setup_trading_approvals()
    h.wait()


def missing_neg_risk_approvals(rpc, owner: str) -> list:
    """Approvals the CLOB needs for CTF neg-risk markets that the SDK does not set up.
    Returns a list of ("erc20"|"erc1155", token) still missing for `owner`."""
    out = []
    if rpc.erc20_allowance(pmapi.PUSD, owner, pmapi.NEG_RISK_ADAPTER) < 2**128:
        out.append(("erc20", pmapi.PUSD))
    if not rpc.is_approved_for_all(pmapi.CTF, owner, pmapi.NEG_RISK_ADAPTER):
        out.append(("erc1155", pmapi.CTF))
    return out


def approve_neg_risk_adapter(c, rpc) -> list:
    """Grant the missing Neg Risk Adapter approvals in one gasless batch (Deposit Wallet) or
    directly (EOA), then ask the CLOB to refresh its cached allowance. Returns what was approved."""
    from polymarket._internal.actions.relayer.calls import (erc1155_set_approval_for_all_call,
                                                            erc20_approval_call)
    missing = missing_neg_risk_approvals(rpc, str(c.wallet))
    calls = []
    for kind, token in missing:
        if kind == "erc20":
            calls.append(erc20_approval_call(token_address=token, spender=pmapi.NEG_RISK_ADAPTER,
                                             amount=2**256 - 1))
        else:
            calls.append(erc1155_set_approval_for_all_call(token_address=token,
                                                           operator=pmapi.NEG_RISK_ADAPTER, approved=True))
    if calls:
        wait_tx(c.execute_transaction(calls=calls, metadata="Approve Neg Risk Adapter"), "approval")
    refresh_clob_allowance(c)
    return missing


def refresh_clob_allowance(c) -> None:
    """Ask the CLOB to re-read on-chain balance/allowance (it caches them)."""
    from polymarket._internal.actions import account as acct
    from polymarket._internal.wallet import signature_type_for
    try:
        path, params = acct.build_update_balance_allowance_request(
            asset_type="COLLATERAL", signature_type=signature_type_for(c.wallet_type))
        c._ctx.secure_clob.get_bytes(path, params=params)  # side-effect endpoint
    except Exception as e:  # noqa: BLE001
        print(f"  (could not refresh Polymarket's cached allowance: {type(e).__name__})")


def wrap_usdce(c, amount: Decimal) -> None:
    """Convert USDC.e in the bot wallet to pUSD (Polymarket's trading collateral)
    via the official CollateralOnramp (docs.polymarket.com/concepts/pusd)."""
    from eth_abi import encode
    from polymarket import TransactionCall
    units = int(amount * 10**6)
    c.approve_erc20(token_address=pmapi.USDCE, spender_address=pmapi.ONRAMP, amount=units).wait()
    from eth_utils import keccak
    selector = keccak(text="wrap(address,address,uint256)")[:4]
    data = "0x" + (selector + encode(["address", "address", "uint256"],
                                      [pmapi.USDCE, c.wallet, units])).hex()
    c.execute_transaction(calls=[TransactionCall(to=pmapi.ONRAMP, data=data)],
                          metadata="Wrap USDC.e to pUSD").wait()


def transfer_pusd(c, to: str, amount: Decimal) -> None:
    c.transfer_erc20(token_address=pmapi.PUSD, recipient_address=to, amount=int(amount * 10**6)).wait()


def order_summary(resp) -> dict:
    d = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}
