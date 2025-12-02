// ═══════════════════════════════════════════════════════════════════════════════
// 🧠 STRATEGY ENGINE - Professional Algo Trading for Crypto
// Inspired by Jesse, QuantConnect, and professional trading systems
// ═══════════════════════════════════════════════════════════════════════════════

use chrono::Local;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 PRICE DATA
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PricePoint {
    pub timestamp: u64,
    pub price: f64,
    pub volume: f64,
}

#[derive(Debug, Clone, Default)]
pub struct PriceHistory {
    pub prices: VecDeque<PricePoint>,
    pub max_size: usize,
}

impl PriceHistory {
    pub fn new(max_size: usize) -> Self {
        Self {
            prices: VecDeque::with_capacity(max_size),
            max_size,
        }
    }

    pub fn add(&mut self, price: f64, volume: f64) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();

        self.prices.push_back(PricePoint {
            timestamp: now,
            price,
            volume,
        });

        if self.prices.len() > self.max_size {
            self.prices.pop_front();
        }
    }

    pub fn get_prices(&self) -> Vec<f64> {
        self.prices.iter().map(|p| p.price).collect()
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📈 TECHNICAL INDICATORS (Jesse-style)
// ═══════════════════════════════════════════════════════════════════════════════

/// Simple Moving Average
pub fn sma(prices: &[f64], period: usize) -> Option<f64> {
    if prices.len() < period {
        return None;
    }
    let sum: f64 = prices[prices.len() - period..].iter().sum();
    Some(sum / period as f64)
}

/// Exponential Moving Average
pub fn ema(prices: &[f64], period: usize) -> Option<f64> {
    if prices.len() < period {
        return None;
    }

    let multiplier = 2.0 / (period as f64 + 1.0);
    let mut ema_value = sma(&prices[..period], period)?;

    for price in &prices[period..] {
        ema_value = (price - ema_value) * multiplier + ema_value;
    }

    Some(ema_value)
}

/// Relative Strength Index (0-100)
pub fn rsi(prices: &[f64], period: usize) -> Option<f64> {
    if prices.len() < period + 1 {
        return None;
    }

    let mut gains = 0.0;
    let mut losses = 0.0;

    for i in (prices.len() - period)..prices.len() {
        let change = prices[i] - prices[i - 1];
        if change > 0.0 {
            gains += change;
        } else {
            losses += change.abs();
        }
    }

    let avg_gain = gains / period as f64;
    let avg_loss = losses / period as f64;

    if avg_loss == 0.0 {
        return Some(100.0);
    }

    let rs = avg_gain / avg_loss;
    Some(100.0 - (100.0 / (1.0 + rs)))
}

/// Rate of Change (momentum)
pub fn roc(prices: &[f64], period: usize) -> Option<f64> {
    if prices.len() < period + 1 {
        return None;
    }

    let current = prices[prices.len() - 1];
    let past = prices[prices.len() - 1 - period];

    if past == 0.0 {
        return None;
    }

    Some(((current - past) / past) * 100.0)
}

/// Average True Range (volatility)
pub fn atr(highs: &[f64], lows: &[f64], closes: &[f64], period: usize) -> Option<f64> {
    if highs.len() < period + 1 || lows.len() < period + 1 || closes.len() < period + 1 {
        return None;
    }

    let mut tr_values = Vec::new();

    for i in 1..highs.len() {
        let tr1 = highs[i] - lows[i];
        let tr2 = (highs[i] - closes[i - 1]).abs();
        let tr3 = (lows[i] - closes[i - 1]).abs();
        tr_values.push(tr1.max(tr2).max(tr3));
    }

    sma(&tr_values, period)
}

/// Bollinger Bands (upper, middle, lower)
pub fn bollinger_bands(prices: &[f64], period: usize, std_dev: f64) -> Option<(f64, f64, f64)> {
    if prices.len() < period {
        return None;
    }

    let middle = sma(prices, period)?;

    let variance: f64 = prices[prices.len() - period..]
        .iter()
        .map(|p| (p - middle).powi(2))
        .sum::<f64>()
        / period as f64;

    let std = variance.sqrt();
    let upper = middle + std_dev * std;
    let lower = middle - std_dev * std;

    Some((upper, middle, lower))
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🎯 ENTRY SIGNALS
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, PartialEq)]
pub enum Signal {
    StrongBuy,
    Buy,
    Hold,
    Sell,
    StrongSell,
}

/// Golden Cross / Death Cross strategy
pub fn ema_cross_signal(prices: &[f64], fast_period: usize, slow_period: usize) -> Signal {
    let fast_ema = ema(prices, fast_period);
    let slow_ema = ema(prices, slow_period);

    match (fast_ema, slow_ema) {
        (Some(fast), Some(slow)) => {
            let diff_pct = ((fast - slow) / slow) * 100.0;

            if diff_pct > 5.0 {
                Signal::StrongBuy
            } else if diff_pct > 1.0 {
                Signal::Buy
            } else if diff_pct < -5.0 {
                Signal::StrongSell
            } else if diff_pct < -1.0 {
                Signal::Sell
            } else {
                Signal::Hold
            }
        }
        _ => Signal::Hold,
    }
}

/// RSI Overbought/Oversold strategy
pub fn rsi_signal(prices: &[f64], period: usize) -> Signal {
    match rsi(prices, period) {
        Some(rsi_value) => {
            if rsi_value < 20.0 {
                Signal::StrongBuy // Oversold
            } else if rsi_value < 30.0 {
                Signal::Buy
            } else if rsi_value > 80.0 {
                Signal::StrongSell // Overbought
            } else if rsi_value > 70.0 {
                Signal::Sell
            } else {
                Signal::Hold
            }
        }
        None => Signal::Hold,
    }
}

/// Momentum breakout strategy
pub fn momentum_signal(prices: &[f64], period: usize) -> Signal {
    match roc(prices, period) {
        Some(momentum) => {
            if momentum > 20.0 {
                Signal::StrongBuy
            } else if momentum > 10.0 {
                Signal::Buy
            } else if momentum < -20.0 {
                Signal::StrongSell
            } else if momentum < -10.0 {
                Signal::Sell
            } else {
                Signal::Hold
            }
        }
        None => Signal::Hold,
    }
}

/// Bollinger Band breakout strategy
pub fn bollinger_signal(prices: &[f64], period: usize, std_dev: f64) -> Signal {
    match bollinger_bands(prices, period, std_dev) {
        Some((upper, middle, lower)) => {
            let current = prices[prices.len() - 1];

            if current > upper {
                Signal::StrongBuy // Breakout above
            } else if current < lower {
                Signal::StrongSell // Breakdown below
            } else if current > middle {
                Signal::Buy
            } else {
                Signal::Sell
            }
        }
        None => Signal::Hold,
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 💰 POSITION SIZING (Kelly Criterion & Risk Management)
// ═══════════════════════════════════════════════════════════════════════════════

/// Kelly Criterion - optimal bet sizing
/// win_rate: 0-1, avg_win/avg_loss ratio
pub fn kelly_criterion(win_rate: f64, win_loss_ratio: f64) -> f64 {
    let kelly = win_rate - ((1.0 - win_rate) / win_loss_ratio);
    // Use fractional Kelly (25%) for safety
    (kelly * 0.25).max(0.0).min(0.25)
}

/// Fixed risk position sizing
/// balance: total capital
/// risk_per_trade: 1-5% typically
/// stop_loss_pct: distance to stop loss
pub fn position_size_fixed_risk(balance: f64, risk_per_trade: f64, stop_loss_pct: f64) -> f64 {
    let risk_amount = balance * (risk_per_trade / 100.0);
    let position = risk_amount / (stop_loss_pct / 100.0);
    position.min(balance * 0.1) // Max 10% of balance per trade
}

/// Volatility-adjusted position sizing
pub fn position_size_volatility_adjusted(
    balance: f64,
    base_risk: f64,
    current_volatility: f64,
    average_volatility: f64,
) -> f64 {
    let volatility_factor = average_volatility / current_volatility.max(0.01);
    let adjusted_risk = base_risk * volatility_factor;
    balance * (adjusted_risk / 100.0).min(0.1)
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📉 DYNAMIC STOP LOSS STRATEGIES
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DynamicStop {
    pub initial_stop: f64,
    pub current_stop: f64,
    pub trailing_pct: f64,
    pub breakeven_activated: bool,
    pub profit_lock_pct: f64, // Lock in X% of profits
}

impl DynamicStop {
    pub fn new(entry_price: f64, stop_loss_pct: f64, trailing_pct: f64) -> Self {
        Self {
            initial_stop: entry_price * (1.0 - stop_loss_pct / 100.0),
            current_stop: entry_price * (1.0 - stop_loss_pct / 100.0),
            trailing_pct,
            breakeven_activated: false,
            profit_lock_pct: 0.0,
        }
    }

    /// Update stop based on new price (trailing stop logic)
    pub fn update(&mut self, entry_price: f64, current_price: f64, highest_price: f64) -> bool {
        let pnl_pct = ((current_price - entry_price) / entry_price) * 100.0;

        // Move to breakeven after 20% profit
        if pnl_pct >= 20.0 && !self.breakeven_activated {
            self.current_stop = entry_price * 1.02; // 2% above entry
            self.breakeven_activated = true;
            self.profit_lock_pct = 2.0;
        }

        // Trailing stop - lock in profits
        if pnl_pct >= 50.0 {
            let trailing_stop = highest_price * (1.0 - self.trailing_pct / 100.0);
            if trailing_stop > self.current_stop {
                self.current_stop = trailing_stop;
                self.profit_lock_pct = ((self.current_stop - entry_price) / entry_price) * 100.0;
            }
        }

        // Tighten stop at high profits
        if pnl_pct >= 100.0 {
            let tight_trailing = highest_price * 0.85; // 15% trailing
            if tight_trailing > self.current_stop {
                self.current_stop = tight_trailing;
            }
        }

        // Check if stop hit
        current_price <= self.current_stop
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🎰 MULTI-LEVEL TAKE PROFIT (Jesse-style)
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TakeProfitLevels {
    pub levels: Vec<TakeProfitLevel>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TakeProfitLevel {
    pub profit_pct: f64,   // At what profit % to trigger
    pub sell_portion: f64, // What % of position to sell
    pub executed: bool,
}

impl TakeProfitLevels {
    /// Conservative take profit strategy
    pub fn conservative() -> Self {
        Self {
            levels: vec![
                TakeProfitLevel {
                    profit_pct: 30.0,
                    sell_portion: 0.25,
                    executed: false,
                },
                TakeProfitLevel {
                    profit_pct: 50.0,
                    sell_portion: 0.25,
                    executed: false,
                },
                TakeProfitLevel {
                    profit_pct: 100.0,
                    sell_portion: 0.30,
                    executed: false,
                },
                // 20% moonbag remains
            ],
        }
    }

    /// Aggressive take profit strategy (for moonshots)
    pub fn aggressive() -> Self {
        Self {
            levels: vec![
                TakeProfitLevel {
                    profit_pct: 50.0,
                    sell_portion: 0.20,
                    executed: false,
                },
                TakeProfitLevel {
                    profit_pct: 100.0,
                    sell_portion: 0.20,
                    executed: false,
                },
                TakeProfitLevel {
                    profit_pct: 200.0,
                    sell_portion: 0.20,
                    executed: false,
                },
                TakeProfitLevel {
                    profit_pct: 500.0,
                    sell_portion: 0.20,
                    executed: false,
                },
                // 20% moonbag remains
            ],
        }
    }

    /// Check if any level should trigger
    pub fn check_triggers(&mut self, current_pnl_pct: f64) -> Option<(f64, f64)> {
        for level in &mut self.levels {
            if !level.executed && current_pnl_pct >= level.profit_pct {
                level.executed = true;
                return Some((level.profit_pct, level.sell_portion));
            }
        }
        None
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🚫 ANTI-RUG DETECTION
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone)]
pub struct RugIndicators {
    pub price_dump_1m: f64,  // % drop in 1 minute
    pub price_dump_5m: f64,  // % drop in 5 minutes
    pub volume_spike: f64,   // Unusual sell volume
    pub liquidity_drop: f64, // Liquidity removal
}

impl RugIndicators {
    pub fn is_potential_rug(&self) -> bool {
        // Rapid price dump
        if self.price_dump_1m < -30.0 || self.price_dump_5m < -50.0 {
            return true;
        }

        // Massive sell volume spike
        if self.volume_spike > 500.0 {
            return true;
        }

        // Liquidity being pulled
        if self.liquidity_drop < -40.0 {
            return true;
        }

        false
    }

    pub fn severity(&self) -> &str {
        if self.price_dump_1m < -50.0 || self.liquidity_drop < -60.0 {
            "🚨 CRITICAL - LIKELY RUG"
        } else if self.price_dump_1m < -30.0 || self.liquidity_drop < -40.0 {
            "⚠️ HIGH - POSSIBLE RUG"
        } else if self.price_dump_5m < -30.0 {
            "⚡ MEDIUM - HEAVY SELLING"
        } else {
            "✅ LOW - NORMAL"
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 COMPOSITE SIGNAL (combine multiple indicators)
// ═══════════════════════════════════════════════════════════════════════════════

pub fn composite_signal(prices: &[f64]) -> (Signal, f64) {
    let mut score: f64 = 0.0;
    let mut count = 0;

    // EMA Cross (weight: 30%)
    match ema_cross_signal(prices, 8, 21) {
        Signal::StrongBuy => {
            score += 30.0;
            count += 1;
        }
        Signal::Buy => {
            score += 15.0;
            count += 1;
        }
        Signal::Hold => {
            count += 1;
        }
        Signal::Sell => {
            score -= 15.0;
            count += 1;
        }
        Signal::StrongSell => {
            score -= 30.0;
            count += 1;
        }
    }

    // RSI (weight: 25%)
    match rsi_signal(prices, 14) {
        Signal::StrongBuy => {
            score += 25.0;
            count += 1;
        }
        Signal::Buy => {
            score += 12.0;
            count += 1;
        }
        Signal::Hold => {
            count += 1;
        }
        Signal::Sell => {
            score -= 12.0;
            count += 1;
        }
        Signal::StrongSell => {
            score -= 25.0;
            count += 1;
        }
    }

    // Momentum (weight: 25%)
    match momentum_signal(prices, 10) {
        Signal::StrongBuy => {
            score += 25.0;
            count += 1;
        }
        Signal::Buy => {
            score += 12.0;
            count += 1;
        }
        Signal::Hold => {
            count += 1;
        }
        Signal::Sell => {
            score -= 12.0;
            count += 1;
        }
        Signal::StrongSell => {
            score -= 25.0;
            count += 1;
        }
    }

    // Bollinger (weight: 20%)
    match bollinger_signal(prices, 20, 2.0) {
        Signal::StrongBuy => {
            score += 20.0;
            count += 1;
        }
        Signal::Buy => {
            score += 10.0;
            count += 1;
        }
        Signal::Hold => {
            count += 1;
        }
        Signal::Sell => {
            score -= 10.0;
            count += 1;
        }
        Signal::StrongSell => {
            score -= 20.0;
            count += 1;
        }
    }

    let confidence = (score.abs() / 100.0).min(1.0);

    let signal = if score > 50.0 {
        Signal::StrongBuy
    } else if score > 20.0 {
        Signal::Buy
    } else if score < -50.0 {
        Signal::StrongSell
    } else if score < -20.0 {
        Signal::Sell
    } else {
        Signal::Hold
    };

    (signal, confidence)
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🎯 ENTRY FILTER (only enter good setups)
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone)]
pub struct EntryFilter {
    pub min_liquidity_usd: f64,
    pub min_volume_24h: f64,
    pub max_price_pump_1h: f64, // Don't chase pumps
    pub min_holders: u32,
    pub max_top_holder_pct: f64, // Avoid concentrated holdings
    pub min_age_minutes: u32,    // Token age
}

impl Default for EntryFilter {
    fn default() -> Self {
        Self {
            min_liquidity_usd: 500.0,
            min_volume_24h: 1000.0,
            max_price_pump_1h: 100.0,
            min_holders: 10,
            max_top_holder_pct: 50.0,
            min_age_minutes: 5,
        }
    }
}

impl EntryFilter {
    pub fn passes(
        &self,
        liquidity: f64,
        volume: f64,
        pump_1h: f64,
        holders: u32,
        top_holder_pct: f64,
        age_min: u32,
    ) -> (bool, Vec<String>) {
        let mut reasons = Vec::new();
        let mut pass = true;

        if liquidity < self.min_liquidity_usd {
            reasons.push(format!(
                "Low liquidity: ${:.0} < ${:.0}",
                liquidity, self.min_liquidity_usd
            ));
            pass = false;
        }

        if volume < self.min_volume_24h {
            reasons.push(format!(
                "Low volume: ${:.0} < ${:.0}",
                volume, self.min_volume_24h
            ));
            pass = false;
        }

        if pump_1h > self.max_price_pump_1h {
            reasons.push(format!(
                "Chasing pump: {:.0}% > {:.0}%",
                pump_1h, self.max_price_pump_1h
            ));
            pass = false;
        }

        if holders < self.min_holders {
            reasons.push(format!("Few holders: {} < {}", holders, self.min_holders));
            pass = false;
        }

        if top_holder_pct > self.max_top_holder_pct {
            reasons.push(format!(
                "Concentrated: {:.0}% > {:.0}%",
                top_holder_pct, self.max_top_holder_pct
            ));
            pass = false;
        }

        if age_min < self.min_age_minutes {
            reasons.push(format!(
                "Too new: {}min < {}min",
                age_min, self.min_age_minutes
            ));
            pass = false;
        }

        (pass, reasons)
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📈 PERFORMANCE TRACKING
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct TradingStats {
    pub total_trades: u32,
    pub winning_trades: u32,
    pub losing_trades: u32,
    pub total_profit: f64,
    pub total_loss: f64,
    pub largest_win: f64,
    pub largest_loss: f64,
    pub current_streak: i32, // Positive = wins, negative = losses
    pub best_streak: i32,
    pub worst_streak: i32,
}

impl TradingStats {
    pub fn record_trade(&mut self, pnl: f64) {
        self.total_trades += 1;

        if pnl > 0.0 {
            self.winning_trades += 1;
            self.total_profit += pnl;
            self.largest_win = self.largest_win.max(pnl);

            if self.current_streak >= 0 {
                self.current_streak += 1;
            } else {
                self.current_streak = 1;
            }
            self.best_streak = self.best_streak.max(self.current_streak);
        } else {
            self.losing_trades += 1;
            self.total_loss += pnl.abs();
            self.largest_loss = self.largest_loss.max(pnl.abs());

            if self.current_streak <= 0 {
                self.current_streak -= 1;
            } else {
                self.current_streak = -1;
            }
            self.worst_streak = self.worst_streak.min(self.current_streak);
        }
    }

    pub fn win_rate(&self) -> f64 {
        if self.total_trades == 0 {
            return 0.0;
        }
        (self.winning_trades as f64 / self.total_trades as f64) * 100.0
    }

    pub fn profit_factor(&self) -> f64 {
        if self.total_loss == 0.0 {
            return f64::INFINITY;
        }
        self.total_profit / self.total_loss
    }

    pub fn avg_win(&self) -> f64 {
        if self.winning_trades == 0 {
            return 0.0;
        }
        self.total_profit / self.winning_trades as f64
    }

    pub fn avg_loss(&self) -> f64 {
        if self.losing_trades == 0 {
            return 0.0;
        }
        self.total_loss / self.losing_trades as f64
    }

    pub fn expectancy(&self) -> f64 {
        let win_rate = self.win_rate() / 100.0;
        (win_rate * self.avg_win()) - ((1.0 - win_rate) * self.avg_loss())
    }

    pub fn summary(&self) -> String {
        format!(
            "📊 Stats: {} trades | {:.1}% win rate | {:.2}x profit factor | ${:.2} expectancy",
            self.total_trades,
            self.win_rate(),
            self.profit_factor(),
            self.expectancy()
        )
    }
}

// ═══════════════════════════════════════════════════════════════════════════════

fn log(msg: &str) {
    println!("[{}] {}", Local::now().format("%H:%M:%S"), msg);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sma() {
        let prices = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        assert_eq!(sma(&prices, 3), Some(4.0));
    }

    #[test]
    fn test_rsi() {
        let prices = vec![
            44.0, 44.5, 43.5, 44.0, 44.5, 44.0, 43.5, 43.0, 43.5, 44.0, 44.5, 45.0, 45.5, 46.0,
            45.5,
        ];
        let rsi_value = rsi(&prices, 14).unwrap();
        assert!(rsi_value > 0.0 && rsi_value < 100.0);
    }

    #[test]
    fn test_dynamic_stop() {
        let mut stop = DynamicStop::new(100.0, 10.0, 20.0);
        assert_eq!(stop.initial_stop, 90.0);

        // Price goes up 25%
        stop.update(100.0, 125.0, 125.0);
        assert!(stop.breakeven_activated);
        assert!(stop.current_stop > 100.0);
    }

    #[test]
    fn test_kelly() {
        // 60% win rate, 2:1 risk/reward
        let kelly = kelly_criterion(0.6, 2.0);
        assert!(kelly > 0.0 && kelly < 1.0);
    }
}

fn main() {
    log("🧠 Strategy Engine - Library module");
    log("Import this in other binaries");
}
