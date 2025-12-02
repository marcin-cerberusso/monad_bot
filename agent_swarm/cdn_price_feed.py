#!/usr/bin/env python3
"""
📡 CDN PRICE FEED - Real-time price monitoring dla Agent Swarm

Źródła danych:
- NAD.FUN API (on-chain prices)
- Whale transactions (mempool)
- Price aggregation
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Callable
import aiohttp
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# NAD.FUN Config
NADFUN_API = "https://api.nad.fun"
NADFUN_ROUTER = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"
MONAD_RPC_DEFAULT = os.getenv("MONAD_RPC_URL", "")


@dataclass
class TokenPrice:
    """Cena tokena"""
    address: str
    symbol: str = ""
    price_mon: float = 0.0
    price_usd: float = 0.0
    volume_24h: float = 0.0
    market_cap: float = 0.0
    holders: int = 0
    liquidity_mon: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    last_update: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class WhaleTransaction:
    """Transakcja wieloryba"""
    tx_hash: str
    whale_address: str
    token_address: str
    action: str  # BUY/SELL
    amount_mon: float
    token_amount: float = 0.0
    timestamp: str = ""
    block_number: int = 0


class PriceFeed:
    """
    CDN warstwa dla cen i whale activity
    
    Responsibilities:
    - Real-time price monitoring
    - Price caching z TTL
    - Whale transaction detection
    - Price aggregation
    - Rate limiting to avoid API hammering
    """
    
    def __init__(self):
        self.prices: Dict[str, TokenPrice] = {}
        self.price_history: Dict[str, List[Dict]] = {}
        self.callbacks: List[Callable] = []
        self.whale_callbacks: List[Callable] = []
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None
        self.monad_rpc = MONAD_RPC_DEFAULT
        self.last_whale_block: Optional[int] = None
        
        # Cache config
        self.price_ttl_seconds = 5
        self.history_max_entries = 1000
        
        # Rate limiting
        self._request_count = 0
        self._request_window_start = 0.0
        self._max_requests_per_minute = 60  # Max 1 request/second average
        self._min_request_interval = 0.5  # Min 500ms between requests
        self._last_request_time = 0.0
        
    async def start(self):
        """Start price feed"""
        self.running = True
        self.session = aiohttp.ClientSession()
        print("📡 CDN Price Feed started")
        if not self.monad_rpc:
            print("⚠️ MONAD_RPC_URL nie ustawiony - on-chain price/whale detection wyłączone")
        
    async def stop(self):
        """Stop price feed"""
        self.running = False
        if self.session:
            await self.session.close()
        print("📡 CDN Price Feed stopped")

    async def _rate_limit(self) -> None:
        """Apply rate limiting before making requests"""
        import time
        now = time.time()

        # Reset window if needed (60s window)
        if now - self._request_window_start > 60:
            self._request_count = 0
            self._request_window_start = now

        # Wait if too many requests in window
        if self._request_count >= self._max_requests_per_minute:
            wait_time = 60 - (now - self._request_window_start)
            if wait_time > 0:
                print(f"⏳ Rate limit: waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
                self._request_count = 0
                self._request_window_start = time.time()

        # Enforce minimum interval between requests
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            await asyncio.sleep(self._min_request_interval - elapsed)

        self._request_count += 1
        self._last_request_time = time.time()
        
    def on_price_update(self, callback: Callable):
        """Register price update callback"""
        self.callbacks.append(callback)
        
    def on_whale_activity(self, callback: Callable):
        """Register whale activity callback"""
        self.whale_callbacks.append(callback)
        
    async def get_token_price(self, token_address: str) -> Optional[TokenPrice]:
        """
        Pobierz cenę tokena (z cache lub API)
        """
        # Check cache
        if token_address in self.prices:
            cached = self.prices[token_address]
            age = (datetime.now() - datetime.fromisoformat(cached.last_update)).total_seconds()
            if age < self.price_ttl_seconds:
                return cached
                
        # Fetch from API
        try:
            price = await self._fetch_nadfun_price(token_address)
            if price:
                self.prices[token_address] = price
                self._record_history(token_address, price)
                await self._notify_price_update(token_address, price)
                return price
        except Exception as e:
            print(f"❌ Price fetch error: {e}")
            
        return self.prices.get(token_address)
        
    async def _fetch_nadfun_price(self, token_address: str) -> Optional[TokenPrice]:
        """Fetch price from NAD.FUN API - multiple endpoints"""
        if not self.session:
            return None

        # Apply rate limiting
        await self._rate_limit()
        
        # Normalize address
        token_address = token_address.lower()
        
        # Try multiple API endpoints
        endpoints = [
            f"https://api.nad.fun/v1/token/{token_address}",
            f"https://api.nad.fun/token/{token_address}",
            f"https://api.nad.fun/v2/tokens/{token_address}",
        ]
        
        for endpoint in endpoints:
            try:
                async with self.session.get(
                    endpoint,
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"Accept": "application/json"}
                ) as resp:
                    if resp.status != 200:
                        continue
                        
                    data = await resp.json()
                    
                    # Handle different response formats
                    if "data" in data:
                        data = data["data"]
                    
                    # Extract price - try multiple field names
                    price_mon = 0.0
                    for field in ["priceInMon", "price_mon", "priceNative", "price", "currentPrice"]:
                        if field in data:
                            price_mon = float(data[field])
                            break
                    
                    return TokenPrice(
                        address=token_address,
                        symbol=data.get("symbol", data.get("name", "")[:8]),
                        price_mon=price_mon,
                        price_usd=float(data.get("priceUsd", data.get("price_usd", 0))),
                        volume_24h=float(data.get("volume24h", data.get("volume_24h", 0))),
                        market_cap=float(data.get("marketCap", data.get("market_cap", 0))),
                        holders=int(data.get("holders", data.get("holder_count", 0))),
                        liquidity_mon=float(data.get("liquidityMon", data.get("liquidity", 0))),
                        price_change_1h=float(data.get("priceChange1h", data.get("change_1h", 0))),
                        price_change_24h=float(data.get("priceChange24h", data.get("change_24h", 0)))
                    )
                    
            except Exception as e:
                continue
        
        # Fallback: Try to get price from on-chain
        try:
            price = await self._fetch_onchain_price(token_address)
            if price:
                return price
        except Exception as e:
            print(f"⚠️ On-chain price fetch failed: {e}")
                
        print(f"⚠️ NAD.FUN API: No price for {token_address[:12]}...")
        return None
    
    async def _fetch_onchain_price(self, token_address: str) -> Optional[TokenPrice]:
        """Fetch price directly from on-chain (bonding curve)"""
        if not self.session or not self.monad_rpc:
            return None
            
        try:
            # Call getAmountOut on router to get current price
            # This is a read-only call to estimate price
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{
                    "to": NADFUN_ROUTER,
                    "data": f"0x5e1e1004{token_address[2:].zfill(64)}{'1'.zfill(64)}"  # getAmountOut selector
                }, "latest"],
                "id": 1
            }
            
            async with self.session.post(
                self.monad_rpc,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
                result = data.get("result", "0x0")
                
                if result and result != "0x":
                    price_wei = int(result, 16)
                    price_mon = price_wei / 10**18
                    
                    return TokenPrice(
                        address=token_address,
                        symbol="",
                        price_mon=price_mon
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as e:
            pass  # On-chain price fetch is optional fallback
            
        return None
            
    async def get_batch_prices(self, token_addresses: List[str]) -> Dict[str, TokenPrice]:
        """Pobierz ceny dla wielu tokenów"""
        results = {}
        
        # Parallel fetch
        tasks = [self.get_token_price(addr) for addr in token_addresses]
        prices = await asyncio.gather(*tasks, return_exceptions=True)
        
        for addr, price in zip(token_addresses, prices):
            if isinstance(price, TokenPrice):
                results[addr] = price
                
        return results
        
    def _record_history(self, token_address: str, price: TokenPrice):
        """Record price to history"""
        if token_address not in self.price_history:
            self.price_history[token_address] = []
            
        self.price_history[token_address].append({
            "price_mon": price.price_mon,
            "timestamp": price.last_update
        })
        
        # Trim history
        if len(self.price_history[token_address]) > self.history_max_entries:
            self.price_history[token_address] = self.price_history[token_address][-self.history_max_entries:]
            
    async def _notify_price_update(self, token_address: str, price: TokenPrice):
        """Notify callbacks about price update"""
        # Check for significant change
        history = self.price_history.get(token_address, [])
        if len(history) >= 2:
            old_price = history[-2].get("price_mon", 0)
            new_price = price.price_mon
            
            # Avoid division by zero
            if old_price > 0 and new_price > 0:
                change_pct = ((new_price - old_price) / old_price) * 100
                
                # Notify if significant change
                if abs(change_pct) > 1:
                    for callback in self.callbacks:
                        try:
                            await callback({
                                "token": token_address,
                                "old_price": old_price,
                                "new_price": new_price,
                                "change_pct": change_pct
                            })
                        except Exception as e:
                            print(f"❌ Callback error: {e}")
                            
    async def monitor_whale_transactions(self, whale_addresses: List[str]):
        """Monitor whale transactions via RPC"""
        if not self.session or not self.monad_rpc:
            return
            
        while self.running:
            try:
                # Apply rate limiting for RPC calls
                await self._rate_limit()

                # Get latest block
                async with self.session.post(
                    self.monad_rpc,
                    json={
                        "jsonrpc": "2.0",
                        "method": "eth_blockNumber",
                        "params": [],
                        "id": 1
                    }
                ) as resp:
                    data = await resp.json()
                    block_num = int(data.get("result", "0x0"), 16)
                
                if self.last_whale_block is not None and block_num <= self.last_whale_block:
                    await asyncio.sleep(2)  # Increased from 1s to reduce RPC load
                    continue

                # Apply rate limiting before next RPC call
                await self._rate_limit()

                # Get block with transactions
                async with self.session.post(
                    self.monad_rpc,
                    json={
                        "jsonrpc": "2.0",
                        "method": "eth_getBlockByNumber",
                        "params": [hex(block_num), True],
                        "id": 1
                    }
                ) as resp:
                    data = await resp.json()
                    block = data.get("result", {})
                    
                    for tx in block.get("transactions", []):
                        from_addr = tx.get("from", "").lower()
                        
                        if from_addr in [w.lower() for w in whale_addresses]:
                            # Found whale transaction!
                            whale_tx = await self._parse_whale_transaction(tx)
                            if whale_tx:
                                for callback in self.whale_callbacks:
                                    await callback(whale_tx)
                    
                    self.last_whale_block = block_num
                                    
            except Exception as e:
                print(f"❌ Whale monitor error: {e}")
                
            await asyncio.sleep(1)
            
    async def _parse_whale_transaction(self, tx: dict) -> Optional[WhaleTransaction]:
        """Parse whale transaction"""
        to_addr = tx.get("to", "")
        
        # Check if it's NAD.FUN router
        if to_addr.lower() != NADFUN_ROUTER.lower():
            return None
            
        input_data = tx.get("input", "")
        value = int(tx.get("value", "0x0"), 16) / 10**18  # Convert to MON
        
        # Parse function selector
        if len(input_data) >= 10:
            selector = input_data[:10]
            
            # Known selectors
            if selector == "0xe597a5ae":  # buy
                action = "BUY"
            elif selector in ["0x46ab8d6f", "0x47e7ef24"]:  # sell variants
                action = "SELL"
            else:
                return None
                
            # Extract token address from input data
            if len(input_data) >= 74:
                token_address = "0x" + input_data[34:74]
            else:
                token_address = "unknown"
                
            return WhaleTransaction(
                tx_hash=tx.get("hash", ""),
                whale_address=tx.get("from", ""),
                token_address=token_address,
                action=action,
                amount_mon=value,
                block_number=int(tx.get("blockNumber", "0x0"), 16),
                timestamp=datetime.now().isoformat()
            )
            
        return None
        
    def get_price_history(self, token_address: str, limit: int = 100) -> List[Dict]:
        """Get price history"""
        history = self.price_history.get(token_address, [])
        return history[-limit:]
        
    def calculate_momentum(self, token_address: str, periods: int = 10) -> float:
        """Calculate price momentum"""
        history = self.get_price_history(token_address, periods)
        
        if len(history) < 2:
            return 0.0
            
        first_price = history[0].get("price_mon", 0)
        last_price = history[-1].get("price_mon", 0)
        
        if first_price > 0:
            return ((last_price - first_price) / first_price) * 100
            
        return 0.0


class PriceAggregator:
    """
    Agregacja cen z wielu źródeł
    """
    
    def __init__(self):
        self.sources: Dict[str, PriceFeed] = {}
        
    def add_source(self, name: str, feed: PriceFeed):
        """Add price source"""
        self.sources[name] = feed
        
    async def get_aggregated_price(self, token_address: str) -> Optional[TokenPrice]:
        """Get aggregated price from all sources"""
        prices = []
        
        for name, feed in self.sources.items():
            price = await feed.get_token_price(token_address)
            if price:
                prices.append(price)
                
        if not prices:
            return None
            
        # Simple average
        avg_price = sum(p.price_mon for p in prices) / len(prices)
        
        # Return first with averaged price
        result = prices[0]
        result.price_mon = avg_price
        return result


# Singleton instance
_price_feed: Optional[PriceFeed] = None


def get_price_feed() -> PriceFeed:
    """Get singleton price feed"""
    global _price_feed
    if _price_feed is None:
        _price_feed = PriceFeed()
    return _price_feed


async def main():
    """Test price feed"""
    feed = get_price_feed()
    await feed.start()
    
    try:
        # Test token
        token = "0x5E1b1A14c8758104B8560514e94ab8320e587777"
        
        print(f"\n📊 Fetching price for {token[:12]}...")
        price = await feed.get_token_price(token)
        
        if price:
            print(f"  Symbol: {price.symbol}")
            print(f"  Price: {price.price_mon:.8f} MON")
            print(f"  Volume 24h: {price.volume_24h:.2f}")
            print(f"  Change 24h: {price.price_change_24h:.2f}%")
        else:
            print("  ❌ Could not fetch price")
            
    finally:
        await feed.stop()


if __name__ == "__main__":
    asyncio.run(main())
