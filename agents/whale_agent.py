"""
🐳 WHALE AGENT - Wykrywa whale buys na NAD.FUN
Now with SmartAgent memory integration!
"""
import asyncio
import json
import os
from typing import Optional
import aiohttp
import websockets
from dotenv import load_dotenv

from .base_agent import BaseAgent, Message, MessageTypes, Channels
from . import decision_logger
from .smart_agent import SmartTradingAgent

load_dotenv()

ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
from .config import MIN_WHALE_BUY_MON as MIN_WHALE_SIZE


class WhaleAgent(BaseAgent):
    """Agent wykrywający whale buys z pamięcią"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        super().__init__("WhaleAgent", redis_url)
        self.ws_url = os.getenv("MONAD_WS_URL")
        self.rpc_url = os.getenv("MONAD_RPC_URL")
        self.session: Optional[aiohttp.ClientSession] = None
        self.whales_seen = 0
        self.tx_checked = 0
        self.router_tx = 0
        
        # Initialize SmartAgent for memory
        self.smart = SmartTradingAgent("WhaleMemory", "data")
        self.log("🧠 Memory system initialized")
        
    async def run(self):
        """Główna pętla - monitoruj WebSocket"""
        self.session = aiohttp.ClientSession()
        
        while self.running:
            try:
                await self._ws_loop()
            except Exception as e:
                self.log(f"WS error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)
    
    async def _ws_loop(self):
        """WebSocket loop - subscribe to new blocks"""
        self.log(f"Connecting to {self.ws_url[:50]}...")
        
        async with websockets.connect(self.ws_url, ping_interval=30) as ws:
            
            # Subscribe to new blocks (newPendingTransactions not supported on Monad)
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["newHeads"]
            }))
            
            response = await ws.recv()
            sub_id = json.loads(response).get("result")
            self.log(f"Subscribed to newHeads: {sub_id}")
            
            async for msg in ws:
                if not self.running:
                    break
                try:
                    data = json.loads(msg)
                    if "params" in data:
                        block = data["params"].get("result", {})
                        block_num = int(block.get("number", "0x0"), 16)
                        asyncio.create_task(self._check_block(block_num))
                except Exception as e:
                    pass
    
    async def _check_block(self, block_num: int):
        """Check all transactions in a block for whale buys"""
        try:
            # Get block with transactions
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getBlockByNumber",
                "params": [hex(block_num), True]  # True = include full tx objects
            }
            async with self.session.post(self.rpc_url, json=payload, timeout=10) as resp:
                data = await resp.json()
                block = data.get("result")
                if not block:
                    return
                
                txs = block.get("transactions", [])
                router_count = 0
                
                for tx in txs:
                    to = tx.get("to", "")
                    if to and to.lower() == ROUTER.lower():
                        router_count += 1
                        await self._process_tx(tx)
                
                if router_count > 0:
                    self.log(f"📦 Block {block_num}: {len(txs)} tx, {router_count} to router")
                    
        except Exception as e:
            self.log(f"Block {block_num} error: {e}")
    
    async def _process_tx(self, tx: dict):
        """Process a router transaction - check if whale buy"""
        try:
            self.tx_checked += 1
            
            value_hex = tx.get("value", "0x0")
            value_mon = int(value_hex, 16) / 1e18
            tx_hash = tx.get("hash", "")
            
            # Log all router transactions for debugging
            if value_mon >= 10:
                self.log(f"🔍 Router tx: {value_mon:.1f} MON (min: {MIN_WHALE_SIZE})")
            
            if value_mon < MIN_WHALE_SIZE:
                return
            
            # Extract token from input
            token = self._extract_token(tx.get("input", ""))
            if not token:
                return
            
            whale = tx.get("from", "").lower()
            self.whales_seen += 1
            
            self.log(f"🐳 WHALE BUY: {value_mon:.1f} MON -> {token[:12]}...")
            
            # 🧠 MEMORY: Check whale profile and get recommendation
            whale_profile = self.smart.long_memory.get_whale_profile(whale)
            if whale_profile:
                trust = whale_profile['trust_score']
                win_rate = whale_profile['win_rate']
                trades = whale_profile['total_trades']
                self.log(f"   Whale history: {trades} trades, {win_rate:.0%} win, trust: {trust:.2f}")
            else:
                self.log(f"   ⚠️ New whale - no history")
                trust = 0.5
            
            # 🧠 MEMORY: Store whale activity
            self.smart.short_memory.remember('whale', {
                'whale': whale,
                'token': token,
                'amount_mon': value_mon,
                'trust_score': trust if whale_profile else 0.5
            }, importance=0.8)
            
            # Log for ML
            decision_logger.log_whale_signal({
                "token": token,
                "whale": whale,
                "amount_mon": value_mon,
                "tx_hash": tx_hash,
                "whale_trust": trust if whale_profile else None,
                "whale_history": whale_profile['total_trades'] if whale_profile else 0
            })
            
            # 🧠 MEMORY: Evaluate trade using SmartAgent
            recommendation = await self.smart.evaluate_trade(
                token=token,
                trigger_type="whale_copy",
                whale_address=whale,
                whale_amount=value_mon,
                token_data=None  # Will be enriched by risk agent
            )
            action = recommendation.action
            confidence = recommendation.confidence
            reasoning = "; ".join(recommendation.reasoning)[:50]
            
            self.log(f"   🧠 SmartAgent: {action} ({confidence:.0%}) - {reasoning}...")
            
            # Store decision in memory
            self.smart.short_memory.remember('decision', {
                'type': 'whale_evaluation',
                'token': token,
                'action': action,
                'confidence': confidence
            }, importance=0.9)
            
            # Only notify and publish if confidence > 40% and action is not skip
            if action == 'skip' or confidence < 0.4:
                self.log(f"   ❌ Low confidence ({confidence:.0%}) or skip, skipping signal")
                return
            
            # Notify
            await self.notify(
                "🐳 Whale Detected",
                f"Whale bought {value_mon:.1f} MON of {token}\n🧠 {action} ({confidence:.0%})\nTx: {tx_hash}",
                0x00FFFF  # Cyan
            )
            
            # Send to risk agent with smart recommendation
            await self.publish(Channels.RISK, Message(
                type=MessageTypes.WHALE_BUY,
                data={
                    "token": token,
                    "whale": whale,
                    "amount_mon": value_mon,
                    "tx_hash": tx_hash,
                    "smart_action": action,
                    "smart_confidence": confidence,
                    "smart_amount": recommendation.amount_mon,
                    "whale_trust": trust if whale_profile else 0.5
                },
                sender=self.name
            ))
            
        except Exception as e:
            pass
    
    async def _get_tx(self, tx_hash: str) -> Optional[dict]:
        """Get transaction"""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionByHash",
                "params": [tx_hash]
            }
            async with self.session.post(self.rpc_url, json=payload, timeout=5) as resp:
                data = await resp.json()
                return data.get("result")
        except:
            return None
    
    def _extract_token(self, input_data: str) -> Optional[str]:
        """Extract token address from buy() calldata
        
        NAD.FUN buy() signature: buy(uint256 minTokensOut, address token, address referrer, uint256 deadline)
        Method: 0x6df9e92b
        Param 0 (bytes 10-74): minTokensOut
        Param 1 (bytes 74-138): token address  <-- this is what we need
        Param 2 (bytes 138-202): referrer
        Param 3 (bytes 202-266): deadline
        """
        if len(input_data) < 138:
            return None
        try:
            # Token is in Param 1 (bytes 74-138), last 40 chars are the address
            token_param = input_data[74:138]
            token = "0x" + token_param[-40:].lower()
            # Validate it's not zero address
            if token == "0x" + "0" * 40:
                return None
            return token
        except:
            return None
    
    async def on_message(self, message: Message):
        """Handle incoming messages"""
        pass  # Whale agent only sends, doesn't receive


if __name__ == "__main__":
    agent = WhaleAgent()
    asyncio.run(agent.start())
