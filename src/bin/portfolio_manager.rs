//! 📊 PORTFOLIO MANAGER - Zarządzanie pozycjami długoterminowymi
//!
//! Rust bot do:
//! - Śledzenia wszystkich pozycji (nie tylko whale follows)
//! - Inteligentnego hold/sell na podstawie analizy
//! - Różne strategie: HOLD, SWING, SCALP
//! - Okresowa rebalansacja portfolio
//! - Alerty na Telegram

use alloy::{
    network::EthereumWallet,
    primitives::{Address, U256},
    providers::{Provider, ProviderBuilder},
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use anyhow::{Context, Result};
use chrono::Utc;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::str::FromStr;
use tokio::time::{interval, Duration as TokioDuration};

// NAD.FUN Router
const NADFUN_ROUTER: &str = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22";

// Portfolio strategies
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum Strategy {
    Hold,      // Trzymaj długoterminowo (tygodnie)
    Swing,     // Swing trade (dni)
    Scalp,     // Szybki zysk (godziny)
    Moon,      // Moon bag - trzymaj do księżyca
    Emergency, // Do natychmiastowej sprzedaży
}

impl Default for Strategy {
    fn default() -> Self {
        Strategy::Swing
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortfolioPosition {
    pub token_address: String,
    pub token_name: String,
    pub token_symbol: String,

    // Purchase info
    pub buy_price_mon: f64,
    pub amount_mon: f64,
    pub tokens_held: f64,
    pub buy_timestamp: String,
    pub buy_tx: String,

    // Current state
    pub current_price_mon: f64,
    pub current_value_mon: f64,
    pub pnl_mon: f64,
    pub pnl_percent: f64,
    pub last_update: String,

    // Strategy
    pub strategy: Strategy,
    pub take_profit_percent: f64,   // e.g., 100 = 2x
    pub stop_loss_percent: f64,     // e.g., -30 = -30%
    pub trailing_stop_percent: f64, // e.g., 20 = sell if drops 20% from ATH
    pub ath_price: f64,             // All-time high since purchase

    // Notes
    pub notes: String,
    pub tags: Vec<String>,

    // Auto-management
    pub auto_sell: bool,
    pub partial_sells: Vec<PartialSell>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PartialSell {
    pub at_percent: f64,   // Sell when profit reaches X%
    pub sell_percent: f64, // Sell X% of position
    pub executed: bool,
    pub executed_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Portfolio {
    pub positions: HashMap<String, PortfolioPosition>,
    pub total_invested: f64,
    pub total_value: f64,
    pub total_pnl: f64,
    pub last_rebalance: String,
    pub config: PortfolioConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortfolioConfig {
    pub max_position_percent: f64, // Max % of portfolio in one token
    pub rebalance_threshold: f64,  // Rebalance if position > X% of portfolio
    pub check_interval_secs: u64,
    pub auto_take_profit: bool,
    pub auto_stop_loss: bool,
    pub telegram_alerts: bool,
}

impl Default for PortfolioConfig {
    fn default() -> Self {
        Self {
            max_position_percent: 20.0,
            rebalance_threshold: 30.0,
            check_interval_secs: 60,
            auto_take_profit: true,
            auto_stop_loss: true,
            telegram_alerts: true,
        }
    }
}

impl Default for Portfolio {
    fn default() -> Self {
        Self {
            positions: HashMap::new(),
            total_invested: 0.0,
            total_value: 0.0,
            total_pnl: 0.0,
            last_rebalance: Utc::now().to_rfc3339(),
            config: PortfolioConfig::default(),
        }
    }
}

// NAD.FUN sell function ABI
sol! {
    function sell(address token, uint256 amount, uint256 minOut) external payable returns (uint256);
    function balanceOf(address account) external view returns (uint256);
}

pub struct PortfolioManager {
    portfolio: Portfolio,
    rpc_url: String,
    wallet_address: Address,
    private_key: String,
    telegram_token: String,
    telegram_chat_id: String,
    http_client: Client,
}

impl PortfolioManager {
    pub async fn new() -> Result<Self> {
        dotenv::dotenv().ok();

        let rpc_url = env::var("MONAD_RPC_URL").context("MONAD_RPC_URL not set")?;
        let private_key = env::var("PRIVATE_KEY").context("PRIVATE_KEY not set")?;
        let telegram_token = env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
        let telegram_chat_id = env::var("TELEGRAM_CHAT_ID").unwrap_or_default();

        // Get wallet address from private key
        let signer: PrivateKeySigner = private_key.parse()?;
        let wallet_address = signer.address();

        // Load portfolio
        let portfolio = Self::load_portfolio()?;

        Ok(Self {
            portfolio,
            rpc_url,
            wallet_address,
            private_key,
            telegram_token,
            telegram_chat_id,
            http_client: Client::new(),
        })
    }

    fn load_portfolio() -> Result<Portfolio> {
        let path = "portfolio.json";
        if std::path::Path::new(path).exists() {
            let data = fs::read_to_string(path)?;
            Ok(serde_json::from_str(&data)?)
        } else {
            Ok(Portfolio::default())
        }
    }

    fn save_portfolio(&self) -> Result<()> {
        let data = serde_json::to_string_pretty(&self.portfolio)?;
        fs::write("portfolio.json", data)?;
        Ok(())
    }

    async fn send_telegram(&self, message: &str) {
        if self.telegram_token.is_empty() || self.telegram_chat_id.is_empty() {
            return;
        }

        let url = format!(
            "https://api.telegram.org/bot{}/sendMessage",
            self.telegram_token
        );

        let _ = self
            .http_client
            .post(&url)
            .json(&serde_json::json!({
                "chat_id": self.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML"
            }))
            .send()
            .await;
    }

    /// Dodaje nową pozycję do portfolio
    pub async fn add_position(
        &mut self,
        token_address: &str,
        token_name: &str,
        token_symbol: &str,
        amount_mon: f64,
        tokens_held: f64,
        buy_price: f64,
        buy_tx: &str,
        strategy: Strategy,
    ) -> Result<()> {
        let token = token_address.to_lowercase();

        let position = PortfolioPosition {
            token_address: token.clone(),
            token_name: token_name.to_string(),
            token_symbol: token_symbol.to_string(),
            buy_price_mon: buy_price,
            amount_mon,
            tokens_held,
            buy_timestamp: Utc::now().to_rfc3339(),
            buy_tx: buy_tx.to_string(),
            current_price_mon: buy_price,
            current_value_mon: amount_mon,
            pnl_mon: 0.0,
            pnl_percent: 0.0,
            last_update: Utc::now().to_rfc3339(),
            strategy: strategy.clone(),
            take_profit_percent: match strategy {
                Strategy::Hold => 200.0,  // 3x
                Strategy::Swing => 50.0,  // 1.5x
                Strategy::Scalp => 20.0,  // 1.2x
                Strategy::Moon => 1000.0, // 11x
                Strategy::Emergency => 0.0,
            },
            stop_loss_percent: match strategy {
                Strategy::Hold => -40.0,
                Strategy::Swing => -25.0,
                Strategy::Scalp => -10.0,
                Strategy::Moon => -50.0,
                Strategy::Emergency => -100.0,
            },
            trailing_stop_percent: 25.0,
            ath_price: buy_price,
            notes: String::new(),
            tags: vec![],
            auto_sell: true,
            partial_sells: vec![
                PartialSell {
                    at_percent: 50.0,
                    sell_percent: 25.0,
                    executed: false,
                    executed_at: None,
                },
                PartialSell {
                    at_percent: 100.0,
                    sell_percent: 25.0,
                    executed: false,
                    executed_at: None,
                },
            ],
        };

        self.portfolio.positions.insert(token.clone(), position);
        self.portfolio.total_invested += amount_mon;
        self.save_portfolio()?;

        let msg = format!(
            "📊 <b>PORTFOLIO: New Position</b>\n\n\
            🪙 {} ({})\n\
            💰 Invested: {:.2} MON\n\
            📋 Strategy: {:?}\n\
            🎯 TP: +{:.0}%\n\
            🛑 SL: {:.0}%",
            token_name,
            token_symbol,
            amount_mon,
            strategy,
            match strategy {
                Strategy::Hold => 200.0,
                Strategy::Swing => 50.0,
                Strategy::Scalp => 20.0,
                Strategy::Moon => 1000.0,
                Strategy::Emergency => 0.0,
            },
            match strategy {
                Strategy::Hold => -40.0,
                Strategy::Swing => -25.0,
                Strategy::Scalp => -10.0,
                Strategy::Moon => -50.0,
                Strategy::Emergency => -100.0,
            }
        );
        self.send_telegram(&msg).await;

        println!(
            "✅ Added {} to portfolio with {:?} strategy",
            token_name, strategy
        );
        Ok(())
    }

    /// Aktualizuje cenę tokena
    async fn update_token_price(&mut self, token_address: &str) -> Result<f64> {
        let token = token_address.to_lowercase();

        // For now just update from stored data
        // TODO: Add actual price fetching from NAD.FUN API

        if let Some(pos) = self.portfolio.positions.get_mut(&token) {
            // Placeholder: assume price didn't change much
            let price = pos.buy_price_mon;

            pos.current_price_mon = price;
            pos.current_value_mon = pos.tokens_held * price;
            pos.pnl_mon = pos.current_value_mon - pos.amount_mon;
            pos.pnl_percent = if pos.amount_mon > 0.0 {
                (pos.pnl_mon / pos.amount_mon) * 100.0
            } else {
                0.0
            };

            // Update ATH
            if price > pos.ath_price {
                pos.ath_price = price;
            }

            pos.last_update = Utc::now().to_rfc3339();
        }

        self.save_portfolio()?;
        Ok(0.0)
    }

    /// Sprawdza wszystkie pozycje i wykonuje auto-akcje
    pub async fn check_positions(&mut self) -> Result<()> {
        let mut actions: Vec<(String, String)> = vec![]; // (token, action)

        for (token, pos) in &self.portfolio.positions {
            if !pos.auto_sell {
                continue;
            }

            // Check take profit
            if pos.pnl_percent >= pos.take_profit_percent {
                actions.push((token.clone(), "TAKE_PROFIT".to_string()));
            }
            // Check stop loss
            else if pos.pnl_percent <= pos.stop_loss_percent {
                actions.push((token.clone(), "STOP_LOSS".to_string()));
            }
            // Check trailing stop
            else if pos.ath_price > pos.buy_price_mon * 1.2 {
                let drop_from_ath =
                    ((pos.ath_price - pos.current_price_mon) / pos.ath_price) * 100.0;
                if drop_from_ath >= pos.trailing_stop_percent {
                    actions.push((token.clone(), "TRAILING_STOP".to_string()));
                }
            }

            // Check partial sells
            for (i, partial) in pos.partial_sells.iter().enumerate() {
                if !partial.executed && pos.pnl_percent >= partial.at_percent {
                    actions.push((token.clone(), format!("PARTIAL_SELL_{}", i)));
                }
            }
        }

        // Execute actions
        for (token, action) in actions {
            match action.as_str() {
                "TAKE_PROFIT" => {
                    println!("🎯 TAKE PROFIT: {}", token);
                    self.sell_position(&token, 100.0).await?;
                }
                "STOP_LOSS" => {
                    println!("🛑 STOP LOSS: {}", token);
                    self.sell_position(&token, 100.0).await?;
                }
                "TRAILING_STOP" => {
                    println!("📉 TRAILING STOP: {}", token);
                    self.sell_position(&token, 100.0).await?;
                }
                action if action.starts_with("PARTIAL_SELL_") => {
                    let idx: usize = action.replace("PARTIAL_SELL_", "").parse().unwrap_or(0);
                    if let Some(pos) = self.portfolio.positions.get(&token) {
                        let sell_pct = pos
                            .partial_sells
                            .get(idx)
                            .map(|p| p.sell_percent)
                            .unwrap_or(0.0);
                        println!("📊 PARTIAL SELL {}%: {}", sell_pct, token);
                        self.sell_position(&token, sell_pct).await?;
                    }
                }
                _ => {}
            }
        }

        Ok(())
    }

    /// Sprzedaje pozycję (całość lub część)
    pub async fn sell_position(&mut self, token_address: &str, percent: f64) -> Result<()> {
        let token = token_address.to_lowercase();

        let pos = match self.portfolio.positions.get(&token) {
            Some(p) => p.clone(),
            None => {
                println!("⚠️ Position not found: {}", token);
                return Ok(());
            }
        };

        let sell_amount = pos.tokens_held * (percent / 100.0);

        println!(
            "💰 Selling {:.2}% of {} ({:.4} tokens)",
            percent, pos.token_name, sell_amount
        );

        // Build sell transaction using alloy
        let router: Address = Address::from_str(NADFUN_ROUTER)?;
        let token_addr: Address = Address::from_str(&token)?;

        // Build call data using sol! macro
        let amount_wei = U256::from((sell_amount * 1e18) as u128);
        let min_out = U256::ZERO;

        let call = sellCall {
            token: token_addr,
            amount: amount_wei,
            minOut: min_out,
        };
        let call_data = call.abi_encode();

        // Create provider and signer
        let signer: PrivateKeySigner = self.private_key.parse()?;
        let wallet = EthereumWallet::from(signer);

        let provider = ProviderBuilder::new()
            .wallet(wallet)
            .on_http(self.rpc_url.parse()?);

        // Build and send transaction
        let tx = alloy::rpc::types::TransactionRequest::default()
            .to(router)
            .input(call_data.into());

        match provider.send_transaction(tx).await {
            Ok(pending) => {
                println!("📤 Sell TX sent: {:?}", pending.tx_hash());

                // Store values before removing
                let token_name = pos.token_name.clone();
                let pnl_mon = pos.pnl_mon;
                let pnl_percent = pos.pnl_percent;
                let current_value = pos.current_value_mon;
                let tokens_remaining = pos.tokens_held - sell_amount;

                // Update position
                if percent >= 99.0 {
                    // Full sell - remove position
                    self.portfolio.positions.remove(&token);

                    let msg = format!(
                        "💰 <b>PORTFOLIO: Position Closed</b>\n\n\
                        🪙 {}\n\
                        📈 P&L: {:+.2} MON ({:+.1}%)\n\
                        💵 Returned: ~{:.2} MON",
                        token_name, pnl_mon, pnl_percent, current_value
                    );
                    self.send_telegram(&msg).await;
                } else {
                    // Partial sell - update position
                    if let Some(p) = self.portfolio.positions.get_mut(&token) {
                        p.tokens_held -= sell_amount;
                        p.current_value_mon = p.tokens_held * p.current_price_mon;

                        // Mark partial sell as executed
                        for partial in &mut p.partial_sells {
                            if !partial.executed && partial.sell_percent == percent {
                                partial.executed = true;
                                partial.executed_at = Some(Utc::now().to_rfc3339());
                                break;
                            }
                        }
                    }

                    let msg = format!(
                        "📊 <b>PORTFOLIO: Partial Sell</b>\n\n\
                        🪙 {} - Sold {:.0}%\n\
                        📈 Current P&L: {:+.2} MON ({:+.1}%)\n\
                        🎒 Remaining: {:.4} tokens",
                        token_name, percent, pnl_mon, pnl_percent, tokens_remaining
                    );
                    self.send_telegram(&msg).await;
                }

                self.save_portfolio()?;
            }
            Err(e) => {
                println!("❌ Sell failed: {}", e);
            }
        }

        Ok(())
    }

    /// Zmienia strategię pozycji
    pub fn set_strategy(&mut self, token_address: &str, strategy: Strategy) -> Result<()> {
        let token = token_address.to_lowercase();

        let token_name: String;

        if let Some(pos) = self.portfolio.positions.get_mut(&token) {
            pos.strategy = strategy.clone();
            token_name = pos.token_name.clone();

            // Update TP/SL based on strategy
            match strategy {
                Strategy::Hold => {
                    pos.take_profit_percent = 200.0;
                    pos.stop_loss_percent = -40.0;
                }
                Strategy::Swing => {
                    pos.take_profit_percent = 50.0;
                    pos.stop_loss_percent = -25.0;
                }
                Strategy::Scalp => {
                    pos.take_profit_percent = 20.0;
                    pos.stop_loss_percent = -10.0;
                }
                Strategy::Moon => {
                    pos.take_profit_percent = 1000.0;
                    pos.stop_loss_percent = -50.0;
                }
                Strategy::Emergency => {
                    // Sell ASAP
                    pos.auto_sell = true;
                    pos.take_profit_percent = 0.0;
                    pos.stop_loss_percent = -100.0;
                }
            }
        } else {
            return Ok(());
        }

        self.save_portfolio()?;
        println!("✅ Strategy updated to {:?} for {}", strategy, token_name);

        Ok(())
    }

    /// Wyświetla portfolio
    pub fn print_portfolio(&self) {
        println!("\n╔══════════════════════════════════════════════════════════════════╗");
        println!("║  📊 PORTFOLIO MANAGER - Monad Trading Bot                        ║");
        println!("╚══════════════════════════════════════════════════════════════════╝\n");

        if self.portfolio.positions.is_empty() {
            println!("📭 No positions in portfolio\n");
            return;
        }

        let mut total_value = 0.0;
        let mut total_pnl = 0.0;

        println!("┌────────────────────┬──────────┬───────────┬───────────┬──────────┐");
        println!("│ Token              │ Strategy │ Value MON │ P&L %     │ P&L MON  │");
        println!("├────────────────────┼──────────┼───────────┼───────────┼──────────┤");

        for (_, pos) in &self.portfolio.positions {
            let strategy_str = match pos.strategy {
                Strategy::Hold => "HOLD",
                Strategy::Swing => "SWING",
                Strategy::Scalp => "SCALP",
                Strategy::Moon => "MOON",
                Strategy::Emergency => "EMERG",
            };

            let pnl_emoji = if pos.pnl_percent > 0.0 {
                "🟢"
            } else {
                "🔴"
            };

            println!(
                "│ {:18} │ {:8} │ {:9.2} │ {} {:+6.1}% │ {:+8.2} │",
                &pos.token_name[..pos.token_name.len().min(18)],
                strategy_str,
                pos.current_value_mon,
                pnl_emoji,
                pos.pnl_percent,
                pos.pnl_mon
            );

            total_value += pos.current_value_mon;
            total_pnl += pos.pnl_mon;
        }

        println!("├────────────────────┴──────────┼───────────┼───────────┼──────────┤");
        println!(
            "│ TOTAL ({} positions)          │ {:9.2} │           │ {:+8.2} │",
            self.portfolio.positions.len(),
            total_value,
            total_pnl
        );
        println!("└───────────────────────────────┴───────────┴───────────┴──────────┘\n");
    }

    /// Główna pętla
    pub async fn run(&mut self) -> Result<()> {
        println!("\n╔══════════════════════════════════════════════════════════════════╗");
        println!("║  📊 PORTFOLIO MANAGER v1.0 - Long-term Position Management       ║");
        println!("╚══════════════════════════════════════════════════════════════════╝");
        println!("💰 Wallet: {:?}", self.wallet_address);
        println!("📋 Positions: {}", self.portfolio.positions.len());
        println!(
            "⏱️ Check interval: {}s",
            self.portfolio.config.check_interval_secs
        );
        println!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n");

        self.send_telegram(
            "📊 <b>PORTFOLIO MANAGER</b> started!\n\nMonitoring long-term positions...",
        )
        .await;

        let mut check_interval = interval(TokioDuration::from_secs(
            self.portfolio.config.check_interval_secs,
        ));

        loop {
            check_interval.tick().await;

            println!(
                "\n[{}] 🔄 Checking portfolio...",
                Utc::now().format("%H:%M:%S")
            );

            // Update all prices
            let tokens: Vec<String> = self.portfolio.positions.keys().cloned().collect();
            for token in tokens {
                if let Err(e) = self.update_token_price(&token).await {
                    println!("⚠️ Failed to update {}: {}", token, e);
                }
            }

            // Check for actions
            if let Err(e) = self.check_positions().await {
                println!("⚠️ Check error: {}", e);
            }

            // Print summary
            self.print_portfolio();
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    println!("🚀 Starting Portfolio Manager...\n");

    let mut manager = PortfolioManager::new().await?;

    // Check for CLI commands
    let args: Vec<String> = env::args().collect();

    if args.len() > 1 {
        match args[1].as_str() {
            "add" if args.len() >= 5 => {
                // ./portfolio_manager add <token> <name> <amount> [strategy]
                let token = &args[2];
                let name = &args[3];
                let amount: f64 = args[4].parse().unwrap_or(0.0);
                let strategy = if args.len() > 5 {
                    match args[5].to_lowercase().as_str() {
                        "hold" => Strategy::Hold,
                        "swing" => Strategy::Swing,
                        "scalp" => Strategy::Scalp,
                        "moon" => Strategy::Moon,
                        _ => Strategy::Swing,
                    }
                } else {
                    Strategy::Swing
                };

                manager
                    .add_position(token, name, name, amount, 0.0, amount, "", strategy)
                    .await?;

                return Ok(());
            }
            "sell" if args.len() >= 3 => {
                // ./portfolio_manager sell <token> [percent]
                let token = &args[2];
                let percent: f64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(100.0);
                manager.sell_position(token, percent).await?;
                return Ok(());
            }
            "strategy" if args.len() >= 4 => {
                // ./portfolio_manager strategy <token> <strategy>
                let token = &args[2];
                let strategy = match args[3].to_lowercase().as_str() {
                    "hold" => Strategy::Hold,
                    "swing" => Strategy::Swing,
                    "scalp" => Strategy::Scalp,
                    "moon" => Strategy::Moon,
                    "emergency" => Strategy::Emergency,
                    _ => Strategy::Swing,
                };
                manager.set_strategy(token, strategy)?;
                return Ok(());
            }
            "list" | "show" => {
                manager.print_portfolio();
                return Ok(());
            }
            "help" | "--help" | "-h" => {
                println!("📊 Portfolio Manager - Commands:\n");
                println!("  ./portfolio_manager              - Run monitoring loop");
                println!("  ./portfolio_manager list         - Show all positions");
                println!("  ./portfolio_manager add <token> <name> <amount> [strategy]");
                println!("  ./portfolio_manager sell <token> [percent]");
                println!(
                    "  ./portfolio_manager strategy <token> <hold|swing|scalp|moon|emergency>"
                );
                println!("\nStrategies:");
                println!("  hold   - Long-term (TP: 200%, SL: -40%)");
                println!("  swing  - Medium-term (TP: 50%, SL: -25%)");
                println!("  scalp  - Short-term (TP: 20%, SL: -10%)");
                println!("  moon   - Diamond hands (TP: 1000%, SL: -50%)");
                return Ok(());
            }
            _ => {
                println!("Unknown command. Use --help for usage.");
                return Ok(());
            }
        }
    }

    // Default: run monitoring loop
    manager.run().await
}
