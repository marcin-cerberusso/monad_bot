"""
📊 DECISION LOGGER - Zapisuje wszystkie decyzje AI do analizy i ML
"""
import json
import os
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path

DECISIONS_DIR = Path(__file__).parent.parent / "data" / "decisions"
TRADES_FILE = DECISIONS_DIR / "trades.jsonl"
SIGNALS_FILE = DECISIONS_DIR / "signals.jsonl"


def ensure_dirs():
    """Create directories if needed"""
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)


def log_whale_signal(data: Dict[str, Any]):
    """Log raw whale signal"""
    ensure_dirs()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": "whale_signal",
        "token": data.get("token"),
        "whale": data.get("whale"),
        "amount_mon": data.get("amount_mon"),
        "tx_hash": data.get("tx_hash"),
    }
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_risk_check(token: str, passed: bool, reason: str, data: Dict[str, Any]):
    """Log risk check result"""
    ensure_dirs()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": "risk_check",
        "token": token,
        "passed": passed,
        "reason": reason,
        "tax_percent": data.get("tax_percent"),
        "liquidity_usd": data.get("liquidity_usd"),
        "is_honeypot": data.get("is_honeypot"),
    }
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_ai_decision(token: str, decision: Dict[str, Any], input_data: Dict[str, Any]):
    """Log AI decision with all context"""
    ensure_dirs()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": "ai_decision",
        "token": token,
        "action": decision.get("action"),
        "confidence": decision.get("confidence"),
        "suggested_amount": decision.get("amount_mon"),
        "reason": decision.get("reason"),
        "input": {
            "whale_amount": input_data.get("amount_mon"),
            "tax_percent": input_data.get("tax_percent"),
            "liquidity_usd": input_data.get("liquidity_usd"),
            "pump_1h": input_data.get("pump_1h"),
        }
    }
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_trade(
    token: str,
    action: str,  # "BUY" or "SELL"
    amount_mon: float,
    price: Optional[float] = None,
    tx_hash: Optional[str] = None,
    success: bool = True,
    error: Optional[str] = None,
    pnl_percent: Optional[float] = None,
    whale_amount: Optional[float] = None,
    ai_confidence: Optional[int] = None,
):
    """Log executed trade with all details"""
    ensure_dirs()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": "trade",
        "token": token,
        "action": action,
        "amount_mon": amount_mon,
        "price": price,
        "tx_hash": tx_hash,
        "success": success,
        "error": error,
        "pnl_percent": pnl_percent,
        "whale_amount": whale_amount,
        "ai_confidence": ai_confidence,
    }
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def get_stats() -> Dict[str, Any]:
    """Get trading stats from logs"""
    ensure_dirs()
    
    stats = {
        "total_signals": 0,
        "risk_passed": 0,
        "risk_failed": 0,
        "ai_buy": 0,
        "ai_skip": 0,
        "trades_success": 0,
        "trades_failed": 0,
        "total_pnl": 0.0,
    }
    
    # Count signals
    if SIGNALS_FILE.exists():
        with open(SIGNALS_FILE) as f:
            for line in f:
                entry = json.loads(line)
                if entry["type"] == "whale_signal":
                    stats["total_signals"] += 1
                elif entry["type"] == "risk_check":
                    if entry["passed"]:
                        stats["risk_passed"] += 1
                    else:
                        stats["risk_failed"] += 1
                elif entry["type"] == "ai_decision":
                    if entry["action"] == "BUY":
                        stats["ai_buy"] += 1
                    else:
                        stats["ai_skip"] += 1
    
    # Count trades
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            for line in f:
                entry = json.loads(line)
                if entry["success"]:
                    stats["trades_success"] += 1
                    if entry.get("pnl_percent"):
                        stats["total_pnl"] += entry["pnl_percent"]
                else:
                    stats["trades_failed"] += 1
    
    return stats


def export_for_ml() -> list:
    """Export all data as ML-ready format"""
    ensure_dirs()
    
    data = []
    
    if SIGNALS_FILE.exists():
        with open(SIGNALS_FILE) as f:
            for line in f:
                data.append(json.loads(line))
    
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            for line in f:
                data.append(json.loads(line))
    
    return sorted(data, key=lambda x: x["timestamp"])
