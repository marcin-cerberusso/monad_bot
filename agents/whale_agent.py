"""
🐳 WHALE AGENT - Wykrywa whale buys na NAD.FUN
"""
import asyncio
import json
import os
from typing import Optional
import aiohttp
import websockets
from dotenv import load_dotenv

from .base_agent import BaseAgent, Message, MessageTypes, Channels

load_dotenv()

ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
from .config import MIN_WHALE_BUY_MON as MIN_WHALE_SIZE


class WhaleAgent(BaseAgent):
    """Agent wykrywający whale buys"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        super().__init__("WhaleAgent", redis_url)
        self.ws_url = os.getenv("MONAD_WS_URL")
        self.rpc_url = os.getenv("MONAD_RPC_URL")
        self.session: Optional[aiohttp.ClientSession] = None
        self.whales_seen = 0
        
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
        """WebSocket loop"""
        self.log(f"Connecting to {self.ws_url[:50]}...")
        
        async with websockets.connect(self.ws_url, ping_interval=30) as ws:
            
            # Subscribe to pending txs
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["newPendingTransactions"]
            }))
            
            response = await ws.recv()
            sub_id = json.loads(response).get("result")
            self.log(f"Subscribed: {sub_id}")
            
            async for msg in ws:
                if not self.running:
                    break
                try:
                    data = json.loads(msg)
                    if "params" in data:
                        tx_hash = data["params"].get("result")
                        if tx_hash:
                            asyncio.create_task(self._check_tx(tx_hash))
                except:
                    pass
    
    async def _check_tx(self, tx_hash: str):
        """Sprawdź czy tx to whale buy"""
        try:
            tx = await self._get_tx(tx_hash)
            if not tx:
                return
            
            to = tx.get("to", "").lower()
            if to != ROUTER.lower():
                return
            
            value_hex = tx.get("value", "0x0")
            value_mon = int(value_hex, 16) / 1e18
            
            if value_mon < MIN_WHALE_SIZE:
                return
            
            # Extract token from input
            token = self._extract_token(tx.get("input", ""))
            if not token:
                return
            
            whale = tx.get("from", "").lower()
            self.whales_seen += 1
            
            self.log(f"🐳 WHALE BUY: {value_mon:.1f} MON -> {token[:12]}...")
            
            # Notify
            await self.notify(
                "🐳 Whale Detected",
                f"Whale bought {value_mon:.1f} MON of {token}\nTx: {tx_hash}",
                0x00FFFF  # Cyan
            )
            
            # Send to risk agent
            await self.publish(Channels.RISK, Message(
                type=MessageTypes.WHALE_BUY,
                data={
                    "token": token,
                    "whale": whale,
                    "amount_mon": value_mon,
                    "tx_hash": tx_hash
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
        """Extract token address from buy() calldata"""
        if len(input_data) < 138:
            return None
        try:
            address_part = input_data[10:74]
            return "0x" + address_part[-40:].lower()
        except:
            return None
    
    async def on_message(self, message: Message):
        """Handle incoming messages"""
        pass  # Whale agent only sends, doesn't receive


if __name__ == "__main__":
    agent = WhaleAgent()
    asyncio.run(agent.start())
