#!/usr/bin/env python3
"""
🔐 CONFIG VALIDATOR - Walidacja konfiguracji przy starcie

Fail-fast dla krytycznych ustawień:
- MONAD_RPC_URL (required for trading)
- PRIVATE_KEY (required for transactions)
- DEEPSEEK_API_KEY (required for AI agents)
- TELEGRAM (optional but recommended)
- chainId verification
"""

import os
import sys
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path
import aiohttp
from dotenv import load_dotenv
import requests

load_dotenv()


@dataclass
class ConfigStatus:
    """Status walidacji konfiguracji"""
    valid: bool
    errors: List[str]
    warnings: List[str]
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None


# Expected Monad chain IDs
MONAD_MAINNET_CHAIN_ID = 143  # Monad mainnet
MONAD_TESTNET_CHAIN_ID = 41454  # Monad testnet (example)
ENV_EXPECTED_CHAIN_ID = os.getenv("EXPECTED_MONAD_CHAIN_ID")
EXPECTED_CHAIN_IDS = {MONAD_MAINNET_CHAIN_ID, MONAD_TESTNET_CHAIN_ID}
if ENV_EXPECTED_CHAIN_ID:
    try:
        EXPECTED_CHAIN_IDS.add(int(ENV_EXPECTED_CHAIN_ID))
    except ValueError:
        pass


async def validate_config(require_trading: bool = True,
                          require_ai: bool = True) -> ConfigStatus:
    """
    Waliduje konfigurację i zwraca status.
    
    Args:
        require_trading: Czy wymagane są klucze do tradingu (RPC, PRIVATE_KEY)
        require_ai: Czy wymagany jest klucz DeepSeek
        
    Returns:
        ConfigStatus z listą błędów i ostrzeżeń
    """
    errors = []
    warnings = []
    chain_id = None
    chain_name = None
    
    # 1. MONAD_RPC_URL
    monad_rpc = os.getenv("MONAD_RPC_URL", "")
    if require_trading:
        if not monad_rpc:
            errors.append("❌ MONAD_RPC_URL nie ustawiony - trading niemożliwy")
        elif not monad_rpc.startswith(("http://", "https://")):
            errors.append(f"❌ MONAD_RPC_URL ma niepoprawny format: {monad_rpc[:30]}...")
        else:
            # Verify RPC and get chainId
            chain_id, chain_error = await _verify_rpc_and_chain(monad_rpc)
            if chain_error:
                errors.append(chain_error)
            elif chain_id:
                if chain_id == MONAD_MAINNET_CHAIN_ID:
                    chain_name = "Monad Mainnet"
                elif chain_id == MONAD_TESTNET_CHAIN_ID:
                    chain_name = "Monad Testnet"
                else:
                    chain_name = f"Unknown ({chain_id})"
                
                if chain_id not in EXPECTED_CHAIN_IDS:
                    errors.append(f"❌ RPC chainId {chain_id} nie jest dozwolony (oczekiwane: {sorted(EXPECTED_CHAIN_IDS)})")
    else:
        if not monad_rpc:
            warnings.append("⚠️ MONAD_RPC_URL nie ustawiony - on-chain funkcje wyłączone")
    
    # 2. PRIVATE_KEY
    private_key = os.getenv("PRIVATE_KEY", "")
    if require_trading:
        if not private_key:
            errors.append("❌ PRIVATE_KEY nie ustawiony - transakcje niemożliwe")
        elif len(private_key) < 64:
            errors.append("❌ PRIVATE_KEY wygląda na niepoprawny (za krótki)")
        elif not private_key.replace("0x", "").replace("0X", "").isalnum():
            errors.append("❌ PRIVATE_KEY zawiera niepoprawne znaki")
    else:
        if not private_key:
            warnings.append("⚠️ PRIVATE_KEY nie ustawiony - tylko read-only mode")
    
    # 3. WALLET_ADDRESS
    wallet = os.getenv("WALLET_ADDRESS", "")
    if not wallet:
        if private_key:
            warnings.append("⚠️ WALLET_ADDRESS nie ustawiony - zostanie obliczony z PRIVATE_KEY")
    elif not wallet.startswith("0x") or len(wallet) != 42:
        errors.append(f"❌ WALLET_ADDRESS niepoprawny format: {wallet}")
    
    # 4. DEEPSEEK_API_KEY
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
    if require_ai:
        if not deepseek_key:
            errors.append("❌ DEEPSEEK_API_KEY nie ustawiony - AI agenty nie będą działać")
        elif len(deepseek_key) < 20:
            warnings.append("⚠️ DEEPSEEK_API_KEY wygląda na krótki - sprawdź poprawność")
    else:
        if not deepseek_key:
            warnings.append("⚠️ DEEPSEEK_API_KEY nie ustawiony - AI analiza wyłączona")
    
    # 5. TELEGRAM (optional)
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not telegram_token or not telegram_chat:
        warnings.append("⚠️ TELEGRAM nie skonfigurowany - powiadomienia wyłączone")
    
    valid = len(errors) == 0
    
    return ConfigStatus(
        valid=valid,
        errors=errors,
        warnings=warnings,
        chain_id=chain_id,
        chain_name=chain_name
    )


async def _verify_rpc_and_chain(rpc_url: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Weryfikuje połączenie RPC i pobiera chainId.
    
    Returns:
        (chain_id, error_message) - error_message jest None jeśli sukces
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Get chainId
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_chainId",
                "params": [],
                "id": 1
            }
            
            async with session.post(
                rpc_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None, f"❌ RPC zwrócił status {resp.status}"
                
                data = await resp.json()
                
                if "error" in data:
                    return None, f"❌ RPC error: {data['error'].get('message', 'unknown')}"
                
                result = data.get("result")
                if not result:
                    return None, "❌ RPC nie zwrócił chainId"
                
                chain_id = int(result, 16)
                return chain_id, None
                
    except aiohttp.ClientConnectorError as e:
        return None, f"❌ Nie można połączyć z RPC: {e}"
    except asyncio.TimeoutError:
        return None, "❌ RPC timeout - sprawdź URL i połączenie"
    except Exception as e:
        return None, f"❌ RPC verification error: {e}"


def print_config_status(status: ConfigStatus) -> None:
    """Wyświetla status konfiguracji"""
    print("\n" + "=" * 60)
    print("🔐 CONFIG VALIDATION")
    print("=" * 60)
    
    if status.chain_name:
        print(f"\n🔗 Chain: {status.chain_name} (ID: {status.chain_id})")
    
    if status.errors:
        print("\n❌ ERRORS:")
        for error in status.errors:
            print(f"   {error}")
    
    if status.warnings:
        print("\n⚠️  WARNINGS:")
        for warning in status.warnings:
            print(f"   {warning}")
    
    if status.valid:
        print("\n✅ Konfiguracja poprawna - można uruchomić system")
    else:
        print("\n🛑 Konfiguracja niepoprawna - napraw błędy przed startem")
    
    print("=" * 60 + "\n")


def notify_telegram_on_failure(status: ConfigStatus) -> None:
    """Wyślij powiadomienie na Telegram o błędzie konfiguracji"""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id or status.valid:
        return
    
    try:
        text = "🛑 AGENT SWARM - konfiguracja niepoprawna\n\n"
        for err in status.errors:
            text += f"- {err}\n"
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass


async def validate_and_exit_on_error(require_trading: bool = True,
                                      require_ai: bool = True) -> ConfigStatus:
    """
    Waliduje konfigurację i kończy program jeśli są krytyczne błędy.
    
    Returns:
        ConfigStatus jeśli walidacja przeszła
    """
    status = await validate_config(require_trading, require_ai)
    print_config_status(status)
    
    if not status.valid:
        print("🛑 Zatrzymuję - napraw konfigurację i uruchom ponownie")
        sys.exit(1)
    
    return status


# CLI dla testowania
async def main():
    """Test walidacji"""
    print("Testing config validation...")
    status = await validate_config(require_trading=True, require_ai=True)
    print_config_status(status)
    return 0 if status.valid else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
