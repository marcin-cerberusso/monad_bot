#!/usr/bin/env python3
"""
🎯 SYNTHETIC LEVERAGE CONFIG - Central Configuration
=====================================================
Jeden plik konfiguracyjny dla wszystkich botów.
Import: from leverage_config import LEVERAGE_CONFIG, calculate_position_size
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# 📊 PROGI I LIMITY
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class LeverageConfig:
    """Centralna konfiguracja syntetycznej dźwigni"""
    
    # ─────────────────────────────────────────────────────────────────────────
    # POSITION SIZING
    # ─────────────────────────────────────────────────────────────────────────
    
    # Bazowy position size (MON)
    base_size_mon: float = 20.0              # Bazowy buy = 20 MON
    
    # Score -> Multiplier (pseudo-leverage)
    # Score < 75: nie kupujemy
    # Score 75-79: 1.0x (20 MON)
    # Score 80-84: 1.5x (30 MON)  
    # Score 85-89: 2.0x (40 MON)
    # Score 90-94: 2.5x (50 MON)
    # Score 95+:   3.0x (60 MON)
    score_multipliers: Dict[int, float] = field(default_factory=lambda: {
        75: 1.0,
        80: 1.5,
        85: 2.0,
        90: 2.5,
        95: 3.0,
    })
    
    # Liquidity adjustment (niższa liq = mniejsza pozycja)
    # Liq < $3K: SKIP (za mała)
    # Liq $3K-$10K: 0.5x
    # Liq $10K-$30K: 0.75x
    # Liq $30K-$100K: 1.0x
    # Liq > $100K: 1.25x
    liquidity_multipliers: Dict[int, float] = field(default_factory=lambda: {
        3000: 0.5,      # Min liq, max caution
        10000: 0.75,
        30000: 1.0,
        100000: 1.25,   # High liq = can go bigger
    })
    
    min_liquidity_usd: float = 3000.0        # Skip tokens below this
    
    # ─────────────────────────────────────────────────────────────────────────
    # EXPOSURE LIMITS
    # ─────────────────────────────────────────────────────────────────────────
    
    max_single_exposure_pct: float = 0.12    # Max 12% portfolio per token
    max_total_exposure_pct: float = 0.50     # Max 50% portfolio invested
    max_positions: int = 8                   # Max concurrent positions
    
    min_position_mon: float = 10.0           # Min trade size
    max_position_mon: float = 80.0           # Max trade size (hard cap)
    
    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY GUARDS
    # ─────────────────────────────────────────────────────────────────────────
    
    max_slippage_bps: int = 250              # 2.5% max slippage
    max_price_impact_pct: float = 3.0        # Max 3% price impact
    max_gas_gwei: int = 120                  # Max gas price
    
    min_whale_score: int = 75                # Min whale score to follow
    min_whale_amount_mon: float = 300.0      # Min whale buy to follow
    
    # ─────────────────────────────────────────────────────────────────────────
    # PYRAMIDING (dokupywanie)
    # ─────────────────────────────────────────────────────────────────────────
    
    enable_pyramiding: bool = True
    max_pyramid_count: int = 2               # Max 2 dogrywki
    
    # Pyramid levels: (min_pnl_pct, size_multiplier_of_original)
    # Przy +25% PnL: dokup 50% oryginalnej pozycji
    # Przy +50% PnL: dokup 30% oryginalnej pozycji
    pyramid_levels: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.25, 0.50),   # +25% -> add 50% of original (10-30 MON)
        (0.50, 0.30),   # +50% -> add 30% more
    ])
    
    # Pyramid guards
    pyramid_min_liquidity_usd: float = 5000.0   # Need more liq for pyramid
    pyramid_max_slippage_bps: int = 200         # Tighter slippage for pyramid
    
    # ─────────────────────────────────────────────────────────────────────────
    # TAKE PROFIT (częściowe)
    # ─────────────────────────────────────────────────────────────────────────
    
    # Partial TP: (pnl_pct, sell_pct)
    partial_tp_levels: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.30, 0.25),   # +30% -> sell 25% (secure initial)
        (0.60, 0.25),   # +60% -> sell 25% more
        (1.00, 0.25),   # +100% -> sell 25% (now in pure profit)
        # Remaining 25% = moonbag with trailing
    ])
    
    # ─────────────────────────────────────────────────────────────────────────
    # TRAILING STOP
    # ─────────────────────────────────────────────────────────────────────────
    
    trailing_activation_pct: float = 0.25    # Activate at +25%
    trailing_stop_pct: float = 0.12          # 12% drop from ATH
    
    # Dynamic trailing (tighter at higher profits)
    dynamic_trailing: bool = True
    trailing_tightening: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.50, 0.10),   # +50% -> 10% trail
        (1.00, 0.08),   # +100% -> 8% trail
        (2.00, 0.06),   # +200% -> 6% trail (protect gains!)
    ])
    
    # ─────────────────────────────────────────────────────────────────────────
    # STOP LOSS
    # ─────────────────────────────────────────────────────────────────────────
    
    hard_stop_loss_pct: float = 0.15         # -15% = cut loss
    
    # Low liquidity exit (if liq drops, exit in portions)
    low_liq_exit_threshold: float = 2000.0   # If liq < $2K
    low_liq_exit_portions: int = 3           # Exit in 3 parts
    low_liq_exit_delay_sec: int = 30         # Wait between parts
    

# ═══════════════════════════════════════════════════════════════════════════════
# 🧮 KALKULATORY
# ═══════════════════════════════════════════════════════════════════════════════

# Singleton config
LEVERAGE_CONFIG = LeverageConfig()


def calculate_position_size(
    portfolio_value_mon: float,
    whale_score: int,
    liquidity_usd: float,
    config: LeverageConfig = None,
    max_impact_pct: float = 3.0,
    expected_impact_pct: float = 0.0,
) -> Tuple[float, float, str]:
    """
    Oblicz wielkość pozycji z pseudo-dźwignią.
    
    Returns: (position_size_mon, effective_multiplier, reason_if_skip)
    """
    cfg = config or LEVERAGE_CONFIG
    
    # Check minimum score
    if whale_score < cfg.min_whale_score:
        return 0, 0, f"Score {whale_score} < min {cfg.min_whale_score}"
    
    # Check minimum liquidity
    if liquidity_usd < cfg.min_liquidity_usd:
        return 0, 0, f"Liquidity ${liquidity_usd:,.0f} < min ${cfg.min_liquidity_usd:,.0f}"

    # Price impact guard
    if expected_impact_pct > max_impact_pct:
        return 0, 0, f"Impact {expected_impact_pct:.2f}% > max {max_impact_pct:.2f}%"
    
    # Get score multiplier
    score_mult = 1.0
    for threshold in sorted(cfg.score_multipliers.keys()):
        if whale_score >= threshold:
            score_mult = cfg.score_multipliers[threshold]
    
    # Get liquidity multiplier
    liq_mult = 0.5  # Default for low liq
    for threshold in sorted(cfg.liquidity_multipliers.keys()):
        if liquidity_usd >= threshold:
            liq_mult = cfg.liquidity_multipliers[threshold]
    
    # Calculate effective multiplier
    effective_mult = score_mult * liq_mult
    
    # Calculate position size
    position_size = cfg.base_size_mon * effective_mult
    
    # Apply hard limits
    position_size = max(position_size, cfg.min_position_mon)
    position_size = min(position_size, cfg.max_position_mon)
    
    # Check max single exposure
    max_exposure = portfolio_value_mon * cfg.max_single_exposure_pct
    position_size = min(position_size, max_exposure)
    
    return position_size, effective_mult, ""


def calculate_pyramid_size(
    original_position_mon: float,
    current_pnl_pct: float,
    pyramid_count: int,
    config: LeverageConfig = None
) -> Tuple[float, str]:
    """
    Oblicz wielkość dogrywki (pyramid).
    
    Returns: (pyramid_size_mon, reason_if_skip)
    """
    cfg = config or LEVERAGE_CONFIG
    
    if not cfg.enable_pyramiding:
        return 0, "Pyramiding disabled"
    
    if pyramid_count >= cfg.max_pyramid_count:
        return 0, f"Max pyramids ({cfg.max_pyramid_count}) reached"
    
    # Find applicable pyramid level
    for level_pct, size_mult in cfg.pyramid_levels:
        if current_pnl_pct >= level_pct:
            pyramid_size = original_position_mon * size_mult
            return pyramid_size, ""
    
    return 0, f"PnL {current_pnl_pct*100:.0f}% below pyramid threshold"


def calculate_trailing_stop(
    entry_price: float,
    highest_price: float,
    current_pnl_pct: float,
    config: LeverageConfig = None
) -> Tuple[float, float]:
    """
    Oblicz trailing stop price i aktualny trail %.
    
    Returns: (trailing_stop_price, current_trail_pct)
    """
    cfg = config or LEVERAGE_CONFIG
    
    # Not activated yet
    if current_pnl_pct < cfg.trailing_activation_pct:
        return 0, 0
    
    # Get trail percentage
    trail_pct = cfg.trailing_stop_pct
    
    if cfg.dynamic_trailing:
        for level_pct, new_trail in cfg.trailing_tightening:
            if current_pnl_pct >= level_pct:
                trail_pct = new_trail
    
    # Calculate stop price
    stop_price = highest_price * (1 - trail_pct)
    
    return stop_price, trail_pct


def should_partial_tp(
    current_pnl_pct: float,
    tp_levels_taken: List[float],
    config: LeverageConfig = None
) -> Tuple[float, float]:
    """
    Sprawdź czy należy wziąć partial TP.
    
    Returns: (sell_percentage, pnl_level) or (0, 0) if no TP
    """
    cfg = config or LEVERAGE_CONFIG
    
    for level_pct, sell_pct in cfg.partial_tp_levels:
        # Skip already taken levels
        if any(abs(taken - level_pct) < 0.05 for taken in tp_levels_taken):
            continue
        
        if current_pnl_pct >= level_pct:
            return sell_pct, level_pct
    
    return 0, 0


def check_entry_guards(
    slippage_bps: int,
    price_impact_pct: float,
    gas_gwei: int,
    is_blocked: bool,
    config: LeverageConfig = None
) -> Tuple[bool, str]:
    """
    Sprawdź guardy przed wejściem.
    
    Returns: (can_enter, reason_if_blocked)
    """
    cfg = config or LEVERAGE_CONFIG
    
    if is_blocked:
        return False, "Token is blocked (dev/honeypot)"
    
    if slippage_bps > cfg.max_slippage_bps:
        return False, f"Slippage {slippage_bps}bps > max {cfg.max_slippage_bps}bps"
    
    if price_impact_pct > cfg.max_price_impact_pct:
        return False, f"Price impact {price_impact_pct:.1f}% > max {cfg.max_price_impact_pct:.1f}%"
    
    if gas_gwei > cfg.max_gas_gwei:
        return False, f"Gas {gas_gwei} gwei > max {cfg.max_gas_gwei} gwei"
    
    return True, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 📋 SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

def print_config_summary():
    """Wyświetl podsumowanie konfiguracji"""
    cfg = LEVERAGE_CONFIG
    
    print("\n" + "═" * 60)
    print("🎯 SYNTHETIC LEVERAGE CONFIG")
    print("═" * 60)
    
    print("\n📊 POSITION SIZING:")
    print(f"   Base size: {cfg.base_size_mon} MON")
    print(f"   Score multipliers: {cfg.score_multipliers}")
    print(f"   Liq multipliers: {cfg.liquidity_multipliers}")
    print(f"   Max position: {cfg.max_position_mon} MON")
    
    print("\n🛡️ EXPOSURE LIMITS:")
    print(f"   Max per token: {cfg.max_single_exposure_pct*100:.0f}%")
    print(f"   Max total: {cfg.max_total_exposure_pct*100:.0f}%")
    print(f"   Max positions: {cfg.max_positions}")
    
    print("\n🚪 ENTRY GUARDS:")
    print(f"   Max slippage: {cfg.max_slippage_bps} bps")
    print(f"   Max price impact: {cfg.max_price_impact_pct}%")
    print(f"   Max gas: {cfg.max_gas_gwei} gwei")
    
    print("\n🔺 PYRAMIDING:")
    print(f"   Enabled: {cfg.enable_pyramiding}")
    print(f"   Max count: {cfg.max_pyramid_count}")
    print(f"   Levels: {cfg.pyramid_levels}")
    
    print("\n💰 TAKE PROFIT:")
    print(f"   Partial levels: {cfg.partial_tp_levels}")
    
    print("\n🎯 TRAILING STOP:")
    print(f"   Activation: +{cfg.trailing_activation_pct*100:.0f}%")
    print(f"   Base trail: {cfg.trailing_stop_pct*100:.0f}%")
    print(f"   Dynamic: {cfg.dynamic_trailing}")
    
    print("\n🛑 STOP LOSS:")
    print(f"   Hard SL: -{cfg.hard_stop_loss_pct*100:.0f}%")
    
    print("═" * 60)


if __name__ == "__main__":
    print_config_summary()
    
    # Test scenarios
    print("\n🧪 TEST SCENARIOS (500 MON portfolio):")
    
    tests = [
        (75, 5000),
        (80, 15000),
        (85, 35000),
        (90, 50000),
        (95, 120000),
    ]
    
    for score, liq in tests:
        size, mult, reason = calculate_position_size(500, score, liq)
        if reason:
            print(f"   Score {score}, Liq ${liq:,}: SKIP - {reason}")
        else:
            print(f"   Score {score}, Liq ${liq:,}: {size:.0f} MON ({mult:.1f}x)")
