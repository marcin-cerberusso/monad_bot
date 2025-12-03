// ═══════════════════════════════════════════════════════════════════════════════
// 💰 TAKE PROFITS v2 - Smart Profit Taking with Safety Guards
// ═══════════════════════════════════════════════════════════════════════════════
// Fixes:
// - Uses real-time price, not just ATH
// - Slippage protection (min 2% of expected output)
// - Liquidity check before selling
// - Dynamic sell percentage based on profit level
// - Pending nonce to avoid conflicts
// - Gas price check - defer if too high
// ═══════════════════════════════════════════════════════════════════════════════

use alloy::{
    network::{EthereumWallet, TransactionBuilder},
    primitives::{Address, U256},
    providers::{Provider, ProviderBuilder, WalletProvider},
    rpc::types::TransactionRequest,
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use chrono::Local;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;

// NAD.FUN Router & Lens
const ROUTER: &str = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22";
const LENS: &str = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea";

// Default safety constants (overridable via ENV)
const DEFAULT_MAX_SLIPPAGE_BPS: u128 = 200; // 2%
const DEFAULT_MIN_PROFIT_PCT: f64 = 25.0;
const DEFAULT_MAX_ATH_DROP_PCT: f64 = 15.0;
const DEFAULT_MAX_GAS_GWEI: u64 = 100;
const DEFAULT_MIN_LIQUIDITY_MON: f64 = 50.0;

// ═══════════════════════════════════════════════════════════════════════════════
// 🛡️ RISK AGENT INTEGRATION
// ═══════════════════════════════════════════════════════════════════════════════

/// Check if token is blocked by risk agent
async fn is_token_blocked(token: &str) -> bool {
    // Try to connect to Dragonfly/Redis and check risk:blocked:{token}
    let redis_url = match env::var("DRAGONFLY_URL") {
        Ok(url) => url,
        Err(_) => return false, // No Redis = no blocking
    };

    let client = match redis::Client::open(redis_url) {
        Ok(c) => c,
        Err(_) => return false,
    };

    let mut conn = match client.get_multiplexed_async_connection().await {
        Ok(c) => c,
        Err(_) => return false,
    };

    let key = format!("risk:blocked:{}", token);
    let result: Result<Option<String>, _> =
        redis::cmd("GET").arg(&key).query_async(&mut conn).await;

    result.ok().flatten().is_some()
}

/// Publish sell event to message bus
async fn publish_sell_event(token: &str, amount_mon: f64, reason: &str) {
    let redis_url = match env::var("DRAGONFLY_URL") {
        Ok(url) => url,
        Err(_) => return,
    };

    let client = match redis::Client::open(redis_url) {
        Ok(c) => c,
        Err(_) => return,
    };

    let mut conn = match client.get_multiplexed_async_connection().await {
        Ok(c) => c,
        Err(_) => return,
    };

    let event = serde_json::json!({
        "type": "trade_executed",
        "sender": "take_profits",
        "payload": {
            "action": "sell",
            "token": token,
            "amount_mon": amount_mon,
            "reason": reason,
            "timestamp": chrono::Utc::now().to_rfc3339()
        }
    });

    let _ = redis::cmd("PUBLISH")
        .arg("agent_swarm:all")
        .arg(event.to_string())
        .query_async::<()>(&mut conn)
        .await;
}

#[derive(Debug, Clone, Copy)]
struct TakeProfitConfig {
    max_slippage_bps: u128,
    min_profit_pct: f64,
    max_ath_drop_pct: f64,
    max_gas_gwei: u64,
    min_liquidity_mon: f64,
}

impl TakeProfitConfig {
    fn from_env() -> Self {
        let max_slippage_bps = env::var("TP_MAX_SLIPPAGE_BPS")
            .ok()
            .and_then(|v| v.parse::<u128>().ok())
            .filter(|v| *v > 0 && *v <= 10_000)
            .unwrap_or(DEFAULT_MAX_SLIPPAGE_BPS);

        let min_profit_pct = env::var("TP_MIN_PROFIT_PCT")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|v| *v >= 0.0)
            .unwrap_or(DEFAULT_MIN_PROFIT_PCT);

        let max_ath_drop_pct = env::var("TP_MAX_ATH_DROP_PCT")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|v| *v >= 0.0)
            .unwrap_or(DEFAULT_MAX_ATH_DROP_PCT);

        let max_gas_gwei = env::var("TP_MAX_GAS_GWEI")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .filter(|v| *v > 0)
            .unwrap_or(DEFAULT_MAX_GAS_GWEI);

        let min_liquidity_mon = env::var("TP_MIN_LIQUIDITY_MON")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|v| *v >= 0.0)
            .unwrap_or(DEFAULT_MIN_LIQUIDITY_MON);

        Self {
            max_slippage_bps,
            min_profit_pct,
            max_ath_drop_pct,
            max_gas_gwei,
            min_liquidity_mon,
        }
    }
}

// ABI
sol! {
    #[derive(Debug)]
    struct SellParams {
        uint256 amountIn;
        uint256 amountOutMin;
        address token;
        address to;
        uint256 deadline;
    }

    function sell(SellParams params) external;
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);

    // NAD.FUN Lens - get real-time price
    function getAmountOut(address token, uint256 amountIn, bool isBuy) external view returns (address router, uint256 amountOut);
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Position {
    token_address: String,
    #[serde(default = "default_name")]
    token_name: String,
    #[serde(alias = "buy_price_mon")]
    amount_mon: f64,
    #[serde(default)]
    entry_price_mon: f64,
    #[serde(default)]
    highest_value_mon: f64,
    #[serde(alias = "buy_time")]
    timestamp: u64,
    #[serde(default)]
    moonbag_secured: bool,
    #[serde(default)]
    tp_level_1_taken: bool,
    #[serde(default)]
    tp_level_2_taken: bool,
    #[serde(default)]
    tp_level_3_taken: bool,
    #[serde(default)]
    whale_exited: bool,
}

fn default_name() -> String {
    "Unknown".to_string()
}

fn log(msg: &str) {
    println!("[{}] {}", Local::now().format("%H:%M:%S"), msg);
}

fn wei_to_mon(wei: U256) -> f64 {
    wei.to::<u128>() as f64 / 1e18
}

/// Calculate dynamic sell percentage based on profit level
/// Higher profit = sell more
fn calculate_sell_percentage(current_pnl_pct: f64, ath_drop_pct: f64) -> f64 {
    // Base: 30% at 30% profit
    // Scale up: 50% at 50%, 70% at 100%, 80% at 200%

    // If dropping from ATH significantly, sell more aggressively
    if ath_drop_pct >= 10.0 {
        return 0.70; // Trailing stop triggered - sell 70%
    }

    if current_pnl_pct >= 200.0 {
        0.80 // 200%+ profit: sell 80%
    } else if current_pnl_pct >= 100.0 {
        0.70 // 100%+ profit: sell 70%
    } else if current_pnl_pct >= 50.0 {
        0.50 // 50%+ profit: sell 50%
    } else if current_pnl_pct >= 30.0 {
        0.30 // 30%+ profit: sell 30%
    } else {
        0.0 // Don't sell below 30%
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    dotenv::dotenv().ok();

    println!("═══════════════════════════════════════════════════════════════");
    println!("💰 TAKE PROFITS v2 - Smart Profit Taking with Safety Guards");
    println!("═══════════════════════════════════════════════════════════════");

    // Load config from ENV
    let cfg = TakeProfitConfig::from_env();

    log(&format!(
        "📋 Config: min_profit={:.0}%, trailing={:.0}%, slippage={:.1}%, max_gas={}gwei, min_liq={:.0}MON",
        cfg.min_profit_pct,
        cfg.max_ath_drop_pct,
        (cfg.max_slippage_bps as f64) / 100.0,
        cfg.max_gas_gwei,
        cfg.min_liquidity_mon
    ));

    // Local bindings for readability
    let min_profit_pct = cfg.min_profit_pct;
    let max_ath_drop_pct = cfg.max_ath_drop_pct;
    let max_slippage_bps = cfg.max_slippage_bps;
    let max_gas_gwei = cfg.max_gas_gwei;
    let min_liquidity_mon = cfg.min_liquidity_mon;

    // Setup
    let rpc_url = env::var("MONAD_RPC_URL").expect("MONAD_RPC_URL required");
    let private_key = env::var("PRIVATE_KEY").expect("PRIVATE_KEY required");

    let signer: PrivateKeySigner = private_key.parse()?;
    let wallet = EthereumWallet::from(signer.clone());
    let rpc: url::Url = rpc_url.parse()?;
    let provider = ProviderBuilder::new().wallet(wallet.clone()).on_http(rpc);

    let my_address = provider.default_signer_address();
    let balance = provider.get_balance(my_address).await?;

    log(&format!("👤 Wallet: {:?}", my_address));
    log(&format!("💵 Balance: {:.2} MON", wei_to_mon(balance)));

    // Check gas price
    let gas_price = provider.get_gas_price().await?;
    let gas_gwei_val = gas_price / 1_000_000_000;
    log(&format!("⛽ Gas: {} gwei", gas_gwei_val));

    if gas_gwei_val > max_gas_gwei.into() {
        log(&format!(
            "❌ Gas too high (>{} gwei), deferring sales",
            max_gas_gwei
        ));
        return Ok(());
    }
    println!();

    // Load positions
    let positions_file = "positions.json";
    let positions: HashMap<String, Position> = match fs::read_to_string(positions_file) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => {
            log("❌ No positions.json found");
            return Ok(());
        }
    };

    let router_addr: Address = ROUTER.parse()?;
    let lens_addr: Address = LENS.parse()?;

    let mut profitable: Vec<(String, Position, f64, f64, f64)> = Vec::new(); // addr, pos, current_pnl, ath_drop, sell_pct
    let mut updated_positions = positions.clone();

    // Analyze each position with REAL-TIME price
    for (addr, pos) in &positions {
        if pos.moonbag_secured && pos.tp_level_1_taken {
            continue; // Already took profits
        }

        let token_addr: Address = match addr.parse() {
            Ok(a) => a,
            Err(_) => continue,
        };

        // Get current token balance
        let balance_call = balanceOfCall {
            account: my_address,
        };
        let balance_result = match provider
            .call(
                &TransactionRequest::default()
                    .to(token_addr)
                    .input(balance_call.abi_encode().into()),
            )
            .await
        {
            Ok(r) => r,
            Err(_) => continue,
        };

        let token_balance = U256::from_be_slice(&balance_result);
        if token_balance == U256::ZERO {
            continue;
        }

        // Risk agent blocklist (Redis key risk:blocked:{token})
        if is_token_blocked(addr).await {
            log(&format!(
                "🚫 {} blocked by risk agent, skipping",
                pos.token_name
            ));
            continue;
        }

        // Get REAL-TIME price via Lens
        let get_price_call = getAmountOutCall {
            token: token_addr,
            amountIn: token_balance,
            isBuy: false, // We're selling
        };

        let price_result = match provider
            .call(
                &TransactionRequest::default()
                    .to(lens_addr)
                    .input(get_price_call.abi_encode().into()),
            )
            .await
        {
            Ok(r) => r,
            Err(e) => {
                log(&format!("⚠️ {} - No price data: {:?}", pos.token_name, e));
                continue;
            }
        };

        // Decode: (address router, uint256 amountOut)
        if price_result.len() < 64 {
            continue;
        }
        let amount_out = U256::from_be_slice(&price_result[32..64]);
        let current_value_mon = wei_to_mon(amount_out);

        // Check minimum liquidity
        if current_value_mon < min_liquidity_mon {
            log(&format!(
                "⚠️ {} - Low liquidity ({:.2} MON)",
                pos.token_name, current_value_mon
            ));
            continue;
        }

        let entry = if pos.entry_price_mon > 0.0 {
            pos.entry_price_mon
        } else {
            pos.amount_mon
        };
        let ath = pos.highest_value_mon.max(current_value_mon);

        // Calculate CURRENT PnL (not ATH-based!)
        let current_pnl_pct = if entry > 0.0 {
            ((current_value_mon - entry) / entry) * 100.0
        } else {
            0.0
        };

        // Calculate drop from ATH
        let ath_drop_pct = if ath > 0.0 {
            ((ath - current_value_mon) / ath) * 100.0
        } else {
            0.0
        };

        // Update ATH if new high
        if current_value_mon > ath {
            if let Some(p) = updated_positions.get_mut(addr) {
                p.highest_value_mon = current_value_mon;
            }
        }

        // Decision logic:
        // 1. Current profit must be >= min_profit_pct (real profit, not ATH)
        // 2. OR drop from ATH >= max_ath_drop_pct (trailing stop)

        let should_take_profit = current_pnl_pct >= min_profit_pct;
        let trailing_stop_triggered = current_pnl_pct > 0.0 && ath_drop_pct >= max_ath_drop_pct;

        if should_take_profit || trailing_stop_triggered {
            let sell_pct = calculate_sell_percentage(current_pnl_pct, ath_drop_pct);
            if sell_pct > 0.0 {
                profitable.push((
                    addr.clone(),
                    pos.clone(),
                    current_pnl_pct,
                    ath_drop_pct,
                    sell_pct,
                ));
            }
        }
    }

    if profitable.is_empty() {
        log(&format!("❌ No positions meeting criteria:"));
        log(&format!("   - Current profit >= {:.0}%", min_profit_pct));
        log(&format!(
            "   - OR drop from ATH >= {:.0}% (trailing stop)",
            max_ath_drop_pct
        ));
        return Ok(());
    }

    // Sort by PnL descending
    profitable.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap());

    log(&format!(
        "🎯 Found {} positions to take profit:",
        profitable.len()
    ));
    for (_addr, pos, pnl, drop, sell_pct) in &profitable {
        let reason = if *drop >= max_ath_drop_pct {
            "TRAILING"
        } else {
            "PROFIT"
        };
        log(&format!(
            "   • {} | +{:.1}% (ATH drop: {:.1}%) | {} | Sell {:.0}%",
            pos.token_name,
            pnl,
            drop,
            reason,
            sell_pct * 100.0
        ));
    }
    println!();

    // Execute sells with safety guards
    for (token_addr_str, pos, current_pnl, ath_drop, sell_pct) in &profitable {
        let token_addr: Address = token_addr_str.parse()?;

        log(&format!(
            "🔄 Processing {} (+{:.1}%, ATH drop {:.1}%)...",
            pos.token_name, current_pnl, ath_drop
        ));

        // Get fresh balance
        let balance_call = balanceOfCall {
            account: my_address,
        };
        let balance_result = provider
            .call(
                &TransactionRequest::default()
                    .to(token_addr)
                    .input(balance_call.abi_encode().into()),
            )
            .await?;

        let token_balance = U256::from_be_slice(&balance_result);
        if token_balance == U256::ZERO {
            log("   ⚠️ No balance, skipping");
            continue;
        }

        // 🛡️ Check if token is blocked by risk agent
        if is_token_blocked(token_addr_str).await {
            log(&format!(
                "   🚫 Token {} blocked by risk agent, skipping sell",
                pos.token_name
            ));
            continue;
        }

        let sell_amount = token_balance * U256::from((sell_pct * 100.0) as u64) / U256::from(100);
        log(&format!(
            "   💰 Balance: {} tokens, selling {:.0}%",
            wei_to_mon(token_balance),
            sell_pct * 100.0
        ));

        // Get expected output for slippage calculation
        let get_price_call = getAmountOutCall {
            token: token_addr,
            amountIn: sell_amount,
            isBuy: false,
        };

        let price_result = provider
            .call(
                &TransactionRequest::default()
                    .to(lens_addr)
                    .input(get_price_call.abi_encode().into()),
            )
            .await?;

        if price_result.len() < 64 {
            log("   ⚠️ Bad price response, skipping");
            continue;
        }
        let expected_out = U256::from_be_slice(&price_result[32..64]);
        let expected_mon = wei_to_mon(expected_out);

        // Calculate minimum output with slippage protection
        // min_out = expected_out * (1 - max_slippage_bps/10_000)
        let min_out = expected_out.saturating_mul(U256::from(10_000u128 - max_slippage_bps))
            / U256::from(10_000u128);

        log(&format!(
            "   📊 Expected: {:.4} MON, Min: {:.4} MON ({:.2}% slippage)",
            expected_mon,
            wei_to_mon(min_out),
            (max_slippage_bps as f64) / 100.0
        ));

        if expected_mon < 0.01 {
            log("   ⚠️ Output too small, skipping");
            continue;
        }

        // Approve with explicit nonce management (avoid reuse)
        let approve_call = approveCall {
            spender: router_addr,
            amount: sell_amount,
        };

        // Use base nonce and increment for the two txs
        let base_nonce = provider.get_transaction_count(my_address).await?;

        let approve_tx = TransactionRequest::default()
            .to(token_addr)
            .input(approve_call.abi_encode().into())
            .max_fee_per_gas(gas_price + gas_price / 2)
            .max_priority_fee_per_gas(gas_price / 10)
            .nonce(base_nonce)
            .gas_limit(100_000)
            .with_chain_id(143); // Monad mainnet

        match provider.send_transaction(approve_tx).await {
            Ok(pending) => {
                let _ = pending.watch().await;
                log("   🔐 Approved");
            }
            Err(e) => {
                log(&format!("   ❌ Approve failed: {:?}", e));
                continue;
            }
        }

        // Sell with slippage protection
        let deadline = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            + 120; // 2 minute deadline

        let sell_params = SellParams {
            amountIn: sell_amount,
            amountOutMin: min_out, // SLIPPAGE PROTECTION - not zero!
            token: token_addr,
            to: my_address,
            deadline: U256::from(deadline),
        };

        let sell_call = sellCall {
            params: sell_params,
        };

        let sell_nonce = base_nonce + 1;
        let sell_tx = TransactionRequest::default()
            .to(router_addr)
            .input(sell_call.abi_encode().into())
            .max_fee_per_gas(gas_price + gas_price / 2)
            .max_priority_fee_per_gas(gas_price / 10)
            .nonce(sell_nonce)
            .gas_limit(300_000)
            .with_chain_id(143);

        match provider.send_transaction(sell_tx).await {
            Ok(pending) => {
                match pending.get_receipt().await {
                    Ok(receipt) => {
                        if receipt.status() {
                            log(&format!("   ✅ SOLD! TX: {:?}", receipt.transaction_hash));

                            // Publish sell event to message bus
                            let reason = if *ath_drop >= max_ath_drop_pct {
                                "trailing_stop"
                            } else {
                                "take_profit"
                            };
                            publish_sell_event(token_addr_str, expected_mon, reason).await;

                            // Only mark as taken AFTER successful sell
                            if let Some(p) = updated_positions.get_mut(token_addr_str) {
                                if *sell_pct >= 0.70 {
                                    p.moonbag_secured = true;
                                }
                                if *current_pnl >= 30.0 {
                                    p.tp_level_1_taken = true;
                                }
                                if *current_pnl >= 50.0 {
                                    p.tp_level_2_taken = true;
                                }
                            }
                        } else {
                            log(&format!(
                                "   ❌ TX failed (slippage?): {:?}",
                                receipt.transaction_hash
                            ));
                        }
                    }
                    Err(e) => {
                        log(&format!("   ❌ Receipt error: {:?}", e));
                    }
                }
            }
            Err(e) => {
                log(&format!("   ❌ Sell failed: {:?}", e));
            }
        }
    }

    // Save updated positions
    fs::write(
        positions_file,
        serde_json::to_string_pretty(&updated_positions)?,
    )?;
    log("📝 Saved positions.json");

    println!();
    println!("═══════════════════════════════════════════════════════════════");
    let new_balance = provider.get_balance(my_address).await?;
    log(&format!(
        "💵 New Balance: {:.2} MON (+{:.2} MON)",
        wei_to_mon(new_balance),
        wei_to_mon(new_balance) - wei_to_mon(balance)
    ));
    log("✅ Take profits v2 complete!");

    Ok(())
}
