#!/usr/bin/env python3
"""
🚫 TOKEN BLOCKLIST - Centralized risk blocking

File-based blocklist (no Redis needed)
Used by all agents to check/block risky tokens
"""

import json
import os
import time
from pathlib import Path
from typing import Optional, Tuple

BASE_DIR = Path(__file__).parent
BLOCKLIST_FILE = BASE_DIR / "blocked_tokens.json"

# Block reasons
REASON_HONEYPOT = "honeypot"
REASON_DEV_SELL = "dev_sell"
REASON_RUG = "rug_pull"
REASON_LOW_LIQ = "low_liquidity"
REASON_BUNDLE = "bundle"
REASON_WASH = "wash_trading"
REASON_SCAM = "scam"

# Default TTL for blocks (24 hours)
DEFAULT_TTL = 86400


def _load_blocklist() -> dict:
    """Load blocklist from file"""
    try:
        if BLOCKLIST_FILE.exists():
            with open(BLOCKLIST_FILE) as f:
                return json.load(f)
    except:
        pass
    return {"blocked": {}}


def _save_blocklist(data: dict):
    """Save blocklist to file"""
    try:
        with open(BLOCKLIST_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving blocklist: {e}")


def block_token(token: str, reason: str, ttl: int = DEFAULT_TTL) -> bool:
    """Block a token for specified duration"""
    token = token.lower()
    data = _load_blocklist()
    
    data["blocked"][token] = {
        "reason": reason,
        "blocked_at": int(time.time()),
        "expires_at": int(time.time()) + ttl
    }
    
    _save_blocklist(data)
    print(f"Blocked token {token[:10]}... reason={reason} ttl={ttl}s")
    return True


def unblock_token(token: str) -> bool:
    """Unblock a token"""
    token = token.lower()
    data = _load_blocklist()
    
    if token in data["blocked"]:
        del data["blocked"][token]
        _save_blocklist(data)
        print(f"Unblocked token {token[:10]}...")
        return True
    return False


def is_blocked(token: str) -> Tuple[bool, Optional[str]]:
    """Check if token is blocked
    Returns: (is_blocked, reason)
    """
    token = token.lower()
    data = _load_blocklist()
    
    if token in data["blocked"]:
        entry = data["blocked"][token]
        expires_at = entry.get("expires_at", 0)
        
        if expires_at > time.time():
            return True, entry.get("reason", "unknown")
        else:
            # Expired - remove it
            del data["blocked"][token]
            _save_blocklist(data)
    
    return False, None


def get_blocked_count() -> int:
    """Get number of currently blocked tokens"""
    data = _load_blocklist()
    now = time.time()
    return sum(1 for e in data["blocked"].values() if e.get("expires_at", 0) > now)


def get_all_blocked() -> list:
    """Get list of all blocked tokens with reasons"""
    data = _load_blocklist()
    now = time.time()
    result = []
    for token, entry in data["blocked"].items():
        if entry.get("expires_at", 0) > now:
            result.append({
                "token": token,
                "reason": entry.get("reason"),
                "blocked_at": entry.get("blocked_at"),
                "expires_at": entry.get("expires_at"),
                "ttl_remaining": entry.get("expires_at", 0) - now
            })
    return result


def cleanup_expired():
    """Remove expired blocks"""
    data = _load_blocklist()
    now = time.time()
    expired = [t for t, e in data["blocked"].items() if e.get("expires_at", 0) <= now]
    
    for token in expired:
        del data["blocked"][token]
    
    if expired:
        _save_blocklist(data)
        print(f"Cleaned up {len(expired)} expired blocks")


if __name__ == "__main__":
    print(f"Blocked tokens: {get_blocked_count()}")
    for b in get_all_blocked():
        print(f"  {b['token'][:15]}... reason={b['reason']} ttl={b['ttl_remaining']:.0f}s")
    cleanup_expired()
