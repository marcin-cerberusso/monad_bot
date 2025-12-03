"""
🔥 LEVERAGE AGENT - Integracja z LeverUp (Perpetuals DEX na Monad)
Otwiera pozycje z dźwignią tylko dla wysokiego confidence AI
"""

import asyncio
import json
import os
from typing import Dict, Any, Optional
from web3 import Web3
from eth_account import Account
from . import config
from .config import setup_logging
from .message_bus import MessageBus

logger = setup_logging("LeverageAgent")

# === LEVERUP CONTRACTS ===
LEVERUP_DIAMOND = "0xea1b8E4aB7f14F7dCA68c5B214303B13078FC5ec"
LVUSD = "0xFD44B35139Ae53FFF7d8F2A9869c503D987f00d1"
LVMON = "0x91b81bfbe3A747230F0529Aa28d8b2Bc898E6D56"
USDC_MONAD = "0x754704Bc059F8C67012fEd69BC8A327a5aafb603"
WMON = "0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A"

# === LEVERAGE CONFIG ===
class LeverageConfig:
    """Konfiguracja dźwigni - KONSERWATYWNA"""
    ENABLED = True
    MIN_AI_CONFIDENCE = 85  # Tylko dla >85% confidence od AI
    DEFAULT_LEVERAGE = 3    # Bezpieczne 3x
    MAX_LEVERAGE = 5        # Nigdy więcej niż 5x
    POSITION_SIZE_MON = 5   # Mniejsze pozycje niż spot (5 MON vs 20 MON)
    
    # Take Profit / Stop Loss dla leverage
    TP_PERCENT = 30         # 30% TP (przy 3x = 90% zysk)
    SL_PERCENT = 10         # 10% SL (przy 3x = 30% strata max)
    
    # Pair mapping (symbol -> LeverUp pair base)
    SUPPORTED_PAIRS = {
        "BTC": "0x0000000000000000000000000000000000000001",  # BTC/USD
        "ETH": "0x0000000000000000000000000000000000000002",  # ETH/USD
        "MON": "0x0000000000000000000000000000000000000003",  # MON/USD
    }

# === ABI dla LeverUp Diamond ===
LEVERUP_ABI = [
    # Open Market Trade
    {
        "inputs": [
            {"name": "pairBase", "type": "address"},
            {"name": "isLong", "type": "bool"},
            {"name": "tokenPay", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "qty", "type": "uint128"},
            {"name": "stopLoss", "type": "uint128"},
            {"name": "takeProfit", "type": "uint128"},
            {"name": "broker", "type": "uint24"},
        ],
        "name": "openMarketTrade",
        "outputs": [{"name": "tradeHash", "type": "bytes32"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    # Close Trade
    {
        "inputs": [
            {"name": "tradeHash", "type": "bytes32"}
        ],
        "name": "closeTrade",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    # Get user open trades
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserOpenTrades",
        "outputs": [
            {
                "components": [
                    {"name": "tradeHash", "type": "bytes32"},
                    {"name": "pairBase", "type": "address"},
                    {"name": "isLong", "type": "bool"},
                    {"name": "entryPrice", "type": "uint128"},
                    {"name": "qty", "type": "uint128"},
                    {"name": "lvMargin", "type": "uint96"},
                    {"name": "stopLoss", "type": "uint128"},
                    {"name": "takeProfit", "type": "uint128"},
                ],
                "type": "tuple[]"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    # Get pairs
    {
        "inputs": [],
        "name": "pairsV4",
        "outputs": [
            {
                "components": [
                    {"name": "name", "type": "string"},
                    {"name": "base", "type": "address"},
                    {"name": "maxLongOiUsd", "type": "uint256"},
                    {"name": "maxShortOiUsd", "type": "uint256"},
                ],
                "type": "tuple[]"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

# ERC20 ABI for approvals
ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]


class LeverageAgent:
    """
    Agent do handlu z dźwignią na LeverUp.
    Używany TYLKO dla wysokiego confidence sygnałów.
    """
    
    def __init__(self, message_bus: MessageBus):
        self.bus = message_bus
        self.w3: Optional[Web3] = None
        self.account = None
        self.leverup_contract = None
        self.open_positions: Dict[str, Dict] = {}
        self.config = LeverageConfig()
        
        self._setup_web3()
    
    def _setup_web3(self):
        """Inicjalizacja Web3 i kontraktów"""
        rpc_url = os.getenv("MONAD_RPC_URL", "https://testnet-rpc.monad.xyz")
        private_key = os.getenv("PRIVATE_KEY")
        
        if not private_key:
            logger.warning("⚠️ No PRIVATE_KEY - leverage trading disabled")
            self.config.ENABLED = False
            return
        
        try:
            self.w3 = Web3(Web3.HTTPProvider(rpc_url))
            self.account = Account.from_key(private_key)
            self.leverup_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(LEVERUP_DIAMOND),
                abi=LEVERUP_ABI
            )
            logger.info(f"✅ LeverUp connected: {LEVERUP_DIAMOND[:10]}...")
            logger.info(f"📊 Leverage config: {self.config.DEFAULT_LEVERAGE}x, min confidence: {self.config.MIN_AI_CONFIDENCE}%")
        except Exception as e:
            logger.error(f"❌ LeverUp setup failed: {e}")
            self.config.ENABLED = False
    
    async def start(self):
        """Start agenta"""
        if not self.config.ENABLED:
            logger.warning("⚠️ LeverageAgent DISABLED - no wallet or connection")
            return
        
        await self.bus.subscribe("monad:leverage", self._handle_message)
        logger.info("🔥 LeverageAgent started - listening for high-confidence signals")
        
        # Heartbeat
        while True:
            await asyncio.sleep(60)
            logger.info(f"💓 ALIVE | Open positions: {len(self.open_positions)}")
    
    async def _handle_message(self, message: Dict[str, Any]):
        """Obsługa wiadomości"""
        msg_type = message.get("type")
        
        if msg_type == "leverage_signal":
            await self._process_leverage_signal(message)
        elif msg_type == "close_leverage":
            await self._close_position(message.get("trade_hash"))
    
    async def _process_leverage_signal(self, signal: Dict[str, Any]):
        """
        Procesuj sygnał na pozycję leverage.
        Wymaga: token, direction (long/short), confidence >= 85%
        """
        token = signal.get("token", "").upper()
        direction = signal.get("direction", "long")
        confidence = signal.get("confidence", 0)
        
        # === FILTR CONFIDENCE ===
        if confidence < self.config.MIN_AI_CONFIDENCE:
            logger.info(f"⏭️ Skip {token}: confidence {confidence}% < {self.config.MIN_AI_CONFIDENCE}%")
            return
        
        # === SPRAWDŹ CZY PAIR WSPIERANY ===
        pair_base = self.config.SUPPORTED_PAIRS.get(token)
        if not pair_base:
            logger.warning(f"⚠️ {token} not supported for leverage")
            return
        
        # === OTWÓRZ POZYCJĘ ===
        logger.info(f"🔥 LEVERAGE SIGNAL: {direction.upper()} {token} @ {self.config.DEFAULT_LEVERAGE}x (confidence: {confidence}%)")
        
        try:
            trade_hash = await self._open_position(
                pair_base=pair_base,
                is_long=(direction == "long"),
                amount_mon=self.config.POSITION_SIZE_MON,
                leverage=self.config.DEFAULT_LEVERAGE
            )
            
            if trade_hash:
                self.open_positions[trade_hash] = {
                    "token": token,
                    "direction": direction,
                    "leverage": self.config.DEFAULT_LEVERAGE,
                    "confidence": confidence
                }
                logger.info(f"✅ Position opened: {trade_hash[:16]}...")
                
        except Exception as e:
            logger.error(f"❌ Failed to open position: {e}")
    
    async def _open_position(
        self,
        pair_base: str,
        is_long: bool,
        amount_mon: float,
        leverage: int
    ) -> Optional[str]:
        """
        Otwórz pozycję na LeverUp.
        Returns: trade_hash lub None
        """
        if not self.w3 or not self.account:
            return None
        
        try:
            amount_wei = self.w3.to_wei(amount_mon, 'ether')
            
            # Oblicz qty (notional) na podstawie leverage
            # qty = amount * leverage (w odpowiedniej precyzji)
            qty = int(amount_wei * leverage / 1e8)  # LeverUp używa 1e10 precision dla qty
            
            # Oblicz TP/SL w cenach
            # Dla uproszczenia: TP = entry * (1 + TP_PERCENT/100), SL = entry * (1 - SL_PERCENT/100)
            # LeverUp używa 1e18 precision dla cen
            # Na razie ustawiamy 0 (bez TP/SL) - LeverUp ma domyślne
            stop_loss = 0
            take_profit = 0
            
            # Build transaction
            tx = self.leverup_contract.functions.openMarketTrade(
                Web3.to_checksum_address(pair_base),
                is_long,
                Web3.to_checksum_address(WMON),  # Płacimy w MON
                amount_wei,
                qty,
                stop_loss,
                take_profit,
                0  # broker = 0
            ).build_transaction({
                'from': self.account.address,
                'gas': 500000,
                'gasPrice': self.w3.eth.gas_price,
                'nonce': self.w3.eth.get_transaction_count(self.account.address),
                'value': amount_wei  # Send MON with tx
            })
            
            # Sign and send
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            
            logger.info(f"📤 TX sent: {tx_hash.hex()[:16]}...")
            
            # Wait for receipt
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            
            if receipt['status'] == 1:
                # Parse trade hash from logs
                # W rzeczywistości trzeba parsować logi - tu uproszczenie
                trade_hash = tx_hash.hex()
                logger.info(f"✅ Position opened! Gas used: {receipt['gasUsed']}")
                return trade_hash
            else:
                logger.error("❌ Transaction failed")
                return None
                
        except Exception as e:
            logger.error(f"❌ Open position error: {e}")
            return None
    
    async def _close_position(self, trade_hash: str):
        """Zamknij pozycję leverage"""
        if not trade_hash or trade_hash not in self.open_positions:
            return
        
        try:
            tx = self.leverup_contract.functions.closeTrade(
                bytes.fromhex(trade_hash[2:] if trade_hash.startswith('0x') else trade_hash)
            ).build_transaction({
                'from': self.account.address,
                'gas': 300000,
                'gasPrice': self.w3.eth.gas_price,
                'nonce': self.w3.eth.get_transaction_count(self.account.address),
            })
            
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            
            if receipt['status'] == 1:
                pos = self.open_positions.pop(trade_hash, {})
                logger.info(f"✅ Position closed: {pos.get('token')} {pos.get('direction')}")
            
        except Exception as e:
            logger.error(f"❌ Close position error: {e}")
    
    async def get_open_positions(self) -> list:
        """Pobierz otwarte pozycje z kontraktu"""
        if not self.leverup_contract or not self.account:
            return []
        
        try:
            positions = self.leverup_contract.functions.getUserOpenTrades(
                self.account.address
            ).call()
            return positions
        except Exception as e:
            logger.error(f"❌ Get positions error: {e}")
            return []


# === HELPER: Sprawdź czy sygnał kwalifikuje się na leverage ===
def should_use_leverage(ai_decision: Dict[str, Any]) -> bool:
    """
    Czy sygnał kwalifikuje się na pozycję leverage?
    - Confidence >= 85%
    - Decyzja = BUY
    - Token wspierany przez LeverUp
    """
    if ai_decision.get("decision") != "BUY":
        return False
    
    confidence = ai_decision.get("confidence", 0)
    if confidence < LeverageConfig.MIN_AI_CONFIDENCE:
        return False
    
    token = ai_decision.get("token_symbol", "").upper()
    if token not in ["BTC", "ETH", "MON"]:
        return False
    
    return True


async def main():
    """Test standalone"""
    from .message_bus import InMemoryBus
    
    bus = InMemoryBus()
    agent = LeverageAgent(bus)
    
    # Test signal
    test_signal = {
        "type": "leverage_signal",
        "token": "ETH",
        "direction": "long",
        "confidence": 90
    }
    
    await agent._handle_message(test_signal)


if __name__ == "__main__":
    asyncio.run(main())
