// ═══════════════════════════════════════════════════════════════════════════════
// 📊 TRADING STATS - Portfolio Analysis & Performance Metrics
// ═══════════════════════════════════════════════════════════════════════════════

use chrono::Local;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Position {
    #[serde(alias = "token")]
    token_address: String,
    #[serde(default = "default_name")]
    token_name: String,
    #[serde(alias = "buy_price_mon")]
    amount_mon: f64,
    #[serde(default)]
    entry_price_mon: f64,
    #[serde(alias = "buy_time")]
    timestamp: u64,
    #[serde(default)]
    highest_value_mon: f64,
    #[serde(default)]
    moonbag_secured: bool,
    #[serde(default)]
    copied_from: String,
    #[serde(default)]
    token_id: Option<u64>,
    #[serde(default)]
    tp_level_1_taken: bool,
    #[serde(default)]
    tp_level_2_taken: bool,
    #[serde(default)]
    tp_level_3_taken: bool,
}

fn default_name() -> String {
    "Unknown".to_string()
}

#[derive(Debug, Default)]
struct Stats {
    total_positions: u32,
    total_invested: f64,
    total_current_value: f64,
    total_ath_value: f64,

    // By performance
    winning_positions: u32,
    losing_positions: u32,
    breakeven_positions: u32,

    // PnL
    unrealized_pnl: f64,
    best_performer_pct: f64,
    best_performer_name: String,
    worst_performer_pct: f64,
    worst_performer_name: String,

    // Take Profit tracking
    tp1_taken: u32,
    tp2_taken: u32,
    tp3_taken: u32,
    moonbags: u32,
}

fn log(msg: &str) {
    println!("[{}] {}", Local::now().format("%H:%M:%S"), msg);
}

fn main() {
    dotenv::dotenv().ok();

    println!();
    println!("╔════════════════════════════════════════════════════════════════╗");
    println!("║           📊 TRADING STATS & PORTFOLIO ANALYSIS 📊            ║");
    println!("╚════════════════════════════════════════════════════════════════╝");
    println!();

    // Load positions
    let positions_file = "positions.json";
    let positions: HashMap<String, Position> = match fs::read_to_string(positions_file) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => {
            log("❌ No positions.json found");
            return;
        }
    };

    if positions.is_empty() {
        log("📊 No positions to analyze");
        return;
    }

    let mut stats = Stats::default();
    stats.total_positions = positions.len() as u32;

    // Analyze each position
    let mut performance: Vec<(String, f64)> = Vec::new();

    for (_addr, pos) in &positions {
        let entry = pos.amount_mon.max(pos.entry_price_mon);
        let current = pos.highest_value_mon.max(entry * 0.9); // Estimate current
        let ath = pos.highest_value_mon;

        stats.total_invested += entry;
        stats.total_current_value += current;
        stats.total_ath_value += ath;

        let pnl_pct = if entry > 0.0 {
            ((current - entry) / entry) * 100.0
        } else {
            0.0
        };

        performance.push((pos.token_name.clone(), pnl_pct));

        if pnl_pct > 5.0 {
            stats.winning_positions += 1;
        } else if pnl_pct < -5.0 {
            stats.losing_positions += 1;
        } else {
            stats.breakeven_positions += 1;
        }

        if pnl_pct > stats.best_performer_pct {
            stats.best_performer_pct = pnl_pct;
            stats.best_performer_name = pos.token_name.clone();
        }
        if pnl_pct < stats.worst_performer_pct {
            stats.worst_performer_pct = pnl_pct;
            stats.worst_performer_name = pos.token_name.clone();
        }

        // Take profit tracking
        if pos.tp_level_1_taken {
            stats.tp1_taken += 1;
        }
        if pos.tp_level_2_taken {
            stats.tp2_taken += 1;
        }
        if pos.tp_level_3_taken {
            stats.tp3_taken += 1;
        }
        if pos.moonbag_secured {
            stats.moonbags += 1;
        }
    }

    stats.unrealized_pnl = stats.total_current_value - stats.total_invested;

    // Sort by performance
    performance.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());

    // Print stats
    println!("═══════════════════════════════════════════════════════════════════");
    println!("                      📈 PORTFOLIO OVERVIEW");
    println!("═══════════════════════════════════════════════════════════════════");
    println!();
    println!("  📊 Total Positions:     {}", stats.total_positions);
    println!("  💰 Total Invested:      {:.2} MON", stats.total_invested);
    println!(
        "  💵 Current Value (est): {:.2} MON",
        stats.total_current_value
    );
    println!("  🏔️  ATH Value:          {:.2} MON", stats.total_ath_value);
    println!();

    let pnl_emoji = if stats.unrealized_pnl > 0.0 {
        "🟢"
    } else {
        "🔴"
    };
    let pnl_pct = if stats.total_invested > 0.0 {
        (stats.unrealized_pnl / stats.total_invested) * 100.0
    } else {
        0.0
    };
    println!(
        "  {} Unrealized PnL:     {:+.2} MON ({:+.1}%)",
        pnl_emoji, stats.unrealized_pnl, pnl_pct
    );
    println!();

    println!("═══════════════════════════════════════════════════════════════════");
    println!("                      🎯 PERFORMANCE BREAKDOWN");
    println!("═══════════════════════════════════════════════════════════════════");
    println!();
    println!("  🟢 Winning:    {} positions", stats.winning_positions);
    println!("  🟡 Breakeven:  {} positions", stats.breakeven_positions);
    println!("  🔴 Losing:     {} positions", stats.losing_positions);
    println!();

    let win_rate = if stats.total_positions > 0 {
        (stats.winning_positions as f64 / stats.total_positions as f64) * 100.0
    } else {
        0.0
    };
    println!("  📈 Win Rate:   {:.1}%", win_rate);
    println!();

    println!(
        "  🏆 Best:  {} (+{:.1}%)",
        stats.best_performer_name, stats.best_performer_pct
    );
    println!(
        "  💀 Worst: {} ({:.1}%)",
        stats.worst_performer_name, stats.worst_performer_pct
    );
    println!();

    println!("═══════════════════════════════════════════════════════════════════");
    println!("                      💰 TAKE PROFIT LEVELS");
    println!("═══════════════════════════════════════════════════════════════════");
    println!();
    println!("  💰 TP Level 1 (+50%):  {} taken", stats.tp1_taken);
    println!("  💎 TP Level 2 (+100%): {} taken", stats.tp2_taken);
    println!("  🚀 TP Level 3 (+200%): {} taken", stats.tp3_taken);
    println!("  🌙 Moonbags Secured:   {}", stats.moonbags);
    println!();

    println!("═══════════════════════════════════════════════════════════════════");
    println!("                      📋 POSITION RANKING");
    println!("═══════════════════════════════════════════════════════════════════");
    println!();

    for (i, (name, pnl)) in performance.iter().take(10).enumerate() {
        let emoji = if *pnl > 100.0 {
            "🔥🔥"
        } else if *pnl > 50.0 {
            "🔥"
        } else if *pnl > 0.0 {
            "📈"
        } else if *pnl > -20.0 {
            "📉"
        } else {
            "💀"
        };
        println!("  {}. {} {} {:+.1}%", i + 1, emoji, name, pnl);
    }

    if performance.len() > 10 {
        println!("  ... and {} more positions", performance.len() - 10);
    }
    println!();

    println!("═══════════════════════════════════════════════════════════════════");
    println!("                      🎓 TRADING ADVICE");
    println!("═══════════════════════════════════════════════════════════════════");
    println!();

    // Advice based on stats
    if win_rate < 40.0 {
        println!(
            "  ⚠️  Low win rate ({:.0}%) - consider tightening entry filters",
            win_rate
        );
    } else if win_rate > 60.0 {
        println!(
            "  ✅ Good win rate ({:.0}%) - strategy is working!",
            win_rate
        );
    }

    if stats.losing_positions > stats.winning_positions {
        println!("  💡 More losers than winners - review your entry criteria");
    }

    if stats.worst_performer_pct < -50.0 {
        println!(
            "  🛑 Deep loser detected ({:.0}%) - check stop-loss settings",
            stats.worst_performer_pct
        );
    }

    if stats.moonbags < stats.winning_positions / 3 {
        println!("  💎 Consider securing more moonbags from winners");
    }

    if stats.tp1_taken < stats.winning_positions / 2 {
        println!("  💰 Many winning positions haven't hit TP1 yet - be patient!");
    }

    println!();
    println!("═══════════════════════════════════════════════════════════════════");
    log("✅ Analysis complete!");
    println!();
}
