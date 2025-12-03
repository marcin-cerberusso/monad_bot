use alloy::{
    network::EthereumWallet,
    primitives::{Address, U256},
    providers::{Provider, ProviderBuilder, WalletProvider},
    rpc::types::TransactionRequest,
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use anyhow::{Context, Result};
use chrono::Local;
use dotenv::dotenv;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    env, fs,
    str::FromStr,
    time::{Duration, SystemTime},
};
use url::Url;

// ═══════════════════════════════════════════════════════════════════════════════
// 📉 POSITION MANAGER v4.0 - MORALIS API + ROUTER FALLBACK
// ═══════════════════════════════════════════════════════════════════════════════
// - Sprawdza cenę przez Moralis API (primary)
// - Fallback: Router getAmountsOut
// - Trailing Stop Loss
// - Hard Stop Loss
// - Moonbag Secure
// ═══════════════════════════════════════════════════════════════════════════════

// NAD.FUN v3 ABI - sell function with SellParams struct
sol! {
    #[derive(Debug)]
    struct SellParams {
        uint256 amountIn;
        uint256 amountOutMin;
        address token;
        address to;
        uint256 deadline;
    }

    #[derive(Debug)]
    function sell(SellParams params) external;

    // ERC20 interface for token operations
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);

    // NAD.FUN Lens - price queries
    // Returns (router address, amountOut)
    function getAmountOut(address token, uint256 amountIn, bool isBuy) external view returns (address router, uint256 amountOut);
}

// NAD.FUN Lens contract address
const LENS_ADDRESS: &str = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea";

#[derive(Debug, Serialize, Deserialize, Clone)]
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
    // Jesse-style multi-level take profit tracking
    #[serde(default)]
    tp_level_1_taken: bool, // 50% profit - sell 30%
    #[serde(default)]
    tp_level_2_taken: bool, // 100% profit - sell 30%
    #[serde(default)]
    tp_level_3_taken: bool, // 200% profit - sell remaining (moonbag)
    // Whale exit detection - if the whale we followed sells, flag it
    #[serde(default)]
    whale_exited: bool,
    #[serde(default)]
    whale_exit_time: Option<u64>,
}

fn default_name() -> String {
    "Unknown Token".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct FullConfig {
    api_keys: ApiKeys,
    settings: Settings,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ApiKeys {
    moralis: String,
    #[serde(default)]
    gemini: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Settings {
    trailing_stop_pct: f64,
    hard_stop_loss_pct: f64,
    take_profit_pct: f64,
    moonbag_portion: f64,
}

impl Settings {
    /// Load settings from ENV with fallback to defaults
    fn from_env() -> Self {
        let trailing_stop_pct = env::var("PM_TRAILING_STOP_PCT")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(20.0);

        let hard_stop_loss_pct = env::var("PM_HARD_STOP_LOSS_PCT")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(-20.0);

        let take_profit_pct = env::var("PM_TAKE_PROFIT_PCT")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(100.0);

        let moonbag_portion = env::var("PM_MOONBAG_PORTION")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(0.3);

        Self {
            trailing_stop_pct,
            hard_stop_loss_pct,
            take_profit_pct,
            moonbag_portion,
        }
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self::from_env()
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📱 TELEGRAM NOTIFICATIONS
// ═══════════════════════════════════════════════════════════════════════════════

async fn send_telegram(client: &Client, token: &str, chat_id: &str, message: &str) -> Result<()> {
    if token.is_empty() || chat_id.is_empty() {
        return Ok(());
    }

    let url = format!("https://api.telegram.org/bot{}/sendMessage", token);
    let _ = client
        .post(&url)
        .json(&serde_json::json!({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }))
        .timeout(Duration::from_secs(10))
        .send()
        .await;
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🌐 MORALIS API
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Deserialize)]
struct MoralisTokenPrice {
    #[serde(rename = "usdPrice")]
    usd_price: Option<f64>,
    #[serde(rename = "nativePrice")]
    native_price: Option<NativePrice>,
}

#[derive(Debug, Deserialize)]
struct NativePrice {
    value: String,
    decimals: u32,
}

async fn get_moralis_price(
    client: &Client,
    token_address: &str,
    api_key: &str,
    chain: &str,
) -> Option<f64> {
    if api_key.is_empty() {
        return None;
    }

    // Moralis API endpoint for token price
    let url = format!(
        "https://deep-index.moralis.io/api/v2.2/erc20/{}/price?chain={}&include=percent_change",
        token_address, chain
    );

    match client
        .get(&url)
        .header("X-API-Key", api_key)
        .header("Accept", "application/json")
        .timeout(Duration::from_secs(5))
        .send()
        .await
    {
        Ok(response) => {
            if response.status().is_success() {
                if let Ok(data) = response.json::<MoralisTokenPrice>().await {
                    return data.usd_price;
                }
            }
            None
        }
        Err(_) => None,
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 DEXSCREENER API (backup)
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Deserialize)]
struct DexScreenerResponse {
    pairs: Option<Vec<DexPair>>,
}

#[derive(Debug, Deserialize)]
struct DexPair {
    #[serde(rename = "priceUsd")]
    price_usd: Option<String>,
    #[serde(rename = "priceNative")]
    price_native: Option<String>,
}

async fn get_dexscreener_price(client: &Client, token_address: &str) -> Option<f64> {
    let url = format!(
        "https://api.dexscreener.com/latest/dex/tokens/{}",
        token_address
    );

    match client
        .get(&url)
        .timeout(Duration::from_secs(5))
        .send()
        .await
    {
        Ok(response) => {
            if let Ok(data) = response.json::<DexScreenerResponse>().await {
                if let Some(pairs) = data.pairs {
                    if let Some(pair) = pairs.first() {
                        if let Some(price_str) = &pair.price_native {
                            return price_str.parse().ok();
                        }
                        if let Some(price_str) = &pair.price_usd {
                            // Convert USD to MON (approx 1 MON = $0.03)
                            if let Ok(usd) = price_str.parse::<f64>() {
                                return Some(usd / 0.03);
                            }
                        }
                    }
                }
            }
            None
        }
        Err(_) => None,
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🔧 HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

fn load_config() -> (Settings, String) {
    if let Ok(content) = fs::read_to_string("config.json") {
        if let Ok(config) = serde_json::from_str::<FullConfig>(&content) {
            return (config.settings, config.api_keys.moralis);
        }
    }
    (Settings::default(), String::new())
}

fn log(msg: &str) {
    use std::io::Write;
    println!("[{}] {}", Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
    let _ = std::io::stdout().flush(); // Force flush for nohup
}

fn wei_to_mon(wei: U256) -> f64 {
    let s = wei.to_string();
    s.parse::<f64>().unwrap_or(0.0) / 1e18
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🎯 JESSE-STYLE RISK MANAGEMENT FUNCTIONS
// ═══════════════════════════════════════════════════════════════════════════════

/// Calculate position size based on risk percentage (inspired by Jesse's risk_to_qty)
/// capital: total capital in MON
/// risk_per_capital: percentage of capital to risk (e.g., 0.02 = 2%)
/// entry_price: price per token in MON
/// stop_loss_price: stop loss price in MON
fn risk_to_qty(capital: f64, risk_per_capital: f64, entry_price: f64, stop_loss_price: f64) -> f64 {
    let risk_per_qty = (entry_price - stop_loss_price).abs();
    if risk_per_qty == 0.0 {
        return 0.0;
    }
    let risk_amount = capital * risk_per_capital;
    risk_amount / risk_per_qty
}

/// Limit stop-loss to maximum allowed risk percentage (inspired by Jesse's limit_stop_loss)
fn limit_stop_loss(entry_price: f64, stop_price: f64, max_allowed_risk_pct: f64) -> f64 {
    let risk = (entry_price - stop_price).abs();
    let max_allowed_risk = entry_price * (max_allowed_risk_pct / 100.0);
    let limited_risk = risk.min(max_allowed_risk);
    entry_price - limited_risk // For long positions
}

/// Dynamic stop-loss structure - loads per-token stop losses from AI agent
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct DynamicStops {
    stops: HashMap<String, f64>,
}

impl DynamicStops {
    fn load() -> Self {
        if let Ok(content) = fs::read_to_string("dynamic_stops.json") {
            serde_json::from_str(&content).unwrap_or_default()
        } else {
            Self::default()
        }
    }

    fn get_stop_for_token(&self, token_name: &str) -> Option<f64> {
        // Try exact match first
        if let Some(stop) = self.stops.get(token_name) {
            return Some(*stop);
        }
        // Try partial match
        for (name, stop) in &self.stops {
            if token_name.to_lowercase().contains(&name.to_lowercase())
                || name.to_lowercase().contains(&token_name.to_lowercase())
            {
                return Some(*stop);
            }
        }
        None
    }
}

/// Multi-level take profit structure (inspired by Jesse's multiple take-profit points)
#[derive(Debug, Clone)]
struct TakeProfitLevels {
    levels: Vec<(f64, f64)>, // (percentage_of_position, profit_target_pct)
}

impl Default for TakeProfitLevels {
    fn default() -> Self {
        TakeProfitLevels {
            levels: vec![
                (0.30, 50.0),  // Sell 30% at +50% profit
                (0.30, 100.0), // Sell 30% at +100% profit (2x)
                (0.40, 200.0), // Sell remaining 40% at +200% (3x) - moonbag
            ],
        }
    }
}

// Retry helper z timeout
async fn retry_with_timeout<F, Fut, T>(
    mut f: F,
    max_retries: u32,
    timeout_secs: u64,
    operation_name: &str,
) -> Option<T>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<T>>,
{
    for attempt in 1..=max_retries {
        match tokio::time::timeout(Duration::from_secs(timeout_secs), f()).await {
            Ok(Ok(result)) => return Some(result),
            Ok(Err(e)) => {
                log(&format!(
                    "   ⚠️  {} attempt {}/{} failed: {:?}",
                    operation_name, attempt, max_retries, e
                ));
                if attempt < max_retries {
                    tokio::time::sleep(Duration::from_millis(1000 * attempt as u64)).await;
                }
            }
            Err(_) => {
                log(&format!(
                    "   ⏱️  {} attempt {}/{} timeout ({}s)",
                    operation_name, attempt, max_retries, timeout_secs
                ));
                if attempt < max_retries {
                    tokio::time::sleep(Duration::from_millis(1000 * attempt as u64)).await;
                }
            }
        }
    }
    None
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 MAIN
// ═══════════════════════════════════════════════════════════════════════════════

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();
    log("╔═══════════════════════════════════════════════════════════════╗");
    log("║  📉 POSITION MANAGER v4.0 - MORALIS + AUTO SELL 📉           ║");
    log("╚═══════════════════════════════════════════════════════════════╝");

    let (settings, moralis_api_key) = load_config();

    if !moralis_api_key.is_empty() {
        log("✅ Moralis API Key loaded");
    } else {
        log("⚠️  Moralis API Key missing - using Router fallback");
    }

    log(&format!(
        "⚙️  Trailing: {}% | Hard SL: {}% | TP: {}%",
        settings.trailing_stop_pct, settings.hard_stop_loss_pct, settings.take_profit_pct
    ));

    let rpc_url_str = env::var("MONAD_RPC_URL").context("Brak MONAD_RPC_URL")?;
    let private_key = env::var("PRIVATE_KEY").context("Brak PRIVATE_KEY")?;
    let router_str = env::var("ROUTER_ADDRESS")
        .unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let wmon_str = env::var("WMON_ADDRESS")
        .unwrap_or("0x760AfE86e5de5fa0Ee542fc7B7B713e1c5425701".to_string());

    // 📱 Telegram config
    let tg_token = env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
    let tg_chat_id = env::var("TELEGRAM_CHAT_ID").unwrap_or_default();

    let rpc_url = Url::parse(&rpc_url_str)?;
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer);

    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_http(rpc_url);

    let my_address = provider.wallet().default_signer().address();
    let router_address = Address::from_str(&router_str)?;
    let wmon_address = Address::from_str(&wmon_str)?;

    log(&format!("👤 Wallet: {:?}", my_address));
    log(&format!("📍 Router: {:?}", router_address));
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

    let http_client = Client::new();

    // Monad chain ID for Moralis (check if supported)
    let monad_chain = "0x279f"; // Monad testnet chain ID (10143 in hex)

    let mut last_heartbeat = SystemTime::now();

    loop {
        // Heartbeat co 60s
        if last_heartbeat.elapsed().unwrap_or_default() > Duration::from_secs(60) {
            log("💓 Heartbeat - bot działa...");
            last_heartbeat = SystemTime::now();
        }

        let path = "positions.json";

        // 🎯 Load dynamic stops from AI agent
        let dynamic_stops = DynamicStops::load();

        if let Ok(content) = fs::read_to_string(path) {
            if let Ok(mut positions) = serde_json::from_str::<HashMap<String, Position>>(&content) {
                if positions.is_empty() {
                    log("📊 Brak pozycji do monitorowania");
                    tokio::time::sleep(Duration::from_secs(10)).await;
                    continue;
                }

                log(&format!("📊 Monitoruję {} pozycji...", positions.len()));
                let mut save_needed = false;
                let mut to_remove = Vec::new();

                let position_list: Vec<(String, Position)> = positions
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect();

                for (addr_str, pos) in position_list {
                    let token_address = match Address::from_str(&addr_str) {
                        Ok(addr) => addr,
                        Err(_) => continue,
                    };

                    // 1. Sprawdź balance tokena (z retry i timeout)
                    log("   🔍 Sprawdzam balance...");
                    let balance_selector = hex::decode("70a08231").unwrap();
                    let mut balance_data = balance_selector;
                    balance_data.extend_from_slice(&[0u8; 12]);
                    balance_data.extend_from_slice(my_address.as_slice());

                    let balance_req = TransactionRequest::default()
                        .to(token_address)
                        .input(balance_data.into());

                    let provider_clone = provider.clone();
                    let balance_req_clone = balance_req.clone();

                    let current_balance = match retry_with_timeout(
                        || async {
                            let bytes = provider_clone.call(&balance_req_clone).await?;
                            if bytes.len() >= 32 {
                                Ok(U256::from_be_slice(&bytes[..32]))
                            } else {
                                Ok(U256::ZERO)
                            }
                        },
                        3,  // max 3 retries
                        10, // 10s timeout per attempt
                        "Balance check",
                    )
                    .await
                    {
                        Some(bal) => {
                            log(&format!("   ✅ Balance: {} tokens", wei_to_mon(bal)));
                            bal
                        }
                        None => {
                            log("   ❌ Balance check failed po 3 próbach - pomijam token");
                            continue;
                        }
                    };

                    if current_balance == U256::ZERO {
                        log(&format!(
                            "   ⚠️ {} - Zero balance (już sprzedane?)",
                            pos.token_name
                        ));
                        to_remove.push(addr_str.clone());
                        continue;
                    }

                    let balance_tokens = wei_to_mon(current_balance);

                    // 2. Pobierz cenę - kolejność: LENS (bonding curve) -> Moralis -> DexScreener -> Router
                    let mut token_price_mon: Option<f64> = None;
                    let mut price_source = "unknown";

                    // 🔮 Try NAD.FUN Lens FIRST (for bonding curve tokens)
                    if current_balance > U256::ZERO {
                        let lens_address = Address::from_str(LENS_ADDRESS).unwrap();
                        let lens_call = getAmountOutCall {
                            token: token_address,
                            amountIn: current_balance,
                            isBuy: false, // We want to know how much MON we'd get for selling
                        };
                        let lens_tx = TransactionRequest::default()
                            .to(lens_address)
                            .input(lens_call.abi_encode().into());

                        if let Ok(result) = provider.call(&lens_tx).await {
                            if result.len() >= 64 {
                                // amountOut is at offset 32 (after router address)
                                let amount_out = U256::from_be_slice(&result[32..64]);
                                let mon_value = amount_out.to::<u128>() as f64 / 1e18;
                                if mon_value > 0.0 {
                                    token_price_mon = Some(mon_value);
                                    price_source = "Lens";
                                }
                            }
                        }
                    }

                    // Try Moralis as backup
                    if token_price_mon.is_none() && !moralis_api_key.is_empty() {
                        if let Some(usd_price) = get_moralis_price(
                            &http_client,
                            &addr_str,
                            &moralis_api_key,
                            monad_chain,
                        )
                        .await
                        {
                            // Convert USD to MON value
                            token_price_mon = Some(usd_price / 0.03 * balance_tokens);
                            price_source = "Moralis";
                        }
                    }

                    // Try DexScreener as backup
                    if token_price_mon.is_none() {
                        if let Some(native_price) =
                            get_dexscreener_price(&http_client, &addr_str).await
                        {
                            token_price_mon = Some(native_price * balance_tokens);
                            price_source = "DexScreener";
                        }
                    }

                    // Try Router getAmountsOut as last resort
                    if token_price_mon.is_none() {
                        let amounts_selector = hex::decode("d06ca61f").unwrap();
                        let mut call_data = amounts_selector;
                        call_data.extend_from_slice(&current_balance.to_be_bytes::<32>());
                        call_data.extend_from_slice(&U256::from(64).to_be_bytes::<32>());
                        call_data.extend_from_slice(&U256::from(2).to_be_bytes::<32>());
                        call_data.extend_from_slice(&[0u8; 12]);
                        call_data.extend_from_slice(token_address.as_slice());
                        call_data.extend_from_slice(&[0u8; 12]);
                        call_data.extend_from_slice(wmon_address.as_slice());

                        let amounts_req = TransactionRequest::default()
                            .to(router_address)
                            .input(call_data.into());

                        if let Ok(bytes) = provider.call(&amounts_req).await {
                            if bytes.len() >= 128 {
                                let amount_out = U256::from_be_slice(&bytes[96..128]);
                                token_price_mon = Some(wei_to_mon(amount_out));
                                price_source = "Router";
                            } else if bytes.len() >= 64 {
                                let amount_out = U256::from_be_slice(&bytes[32..64]);
                                token_price_mon = Some(wei_to_mon(amount_out));
                                price_source = "Router";
                            }
                        }
                    }

                    if let Some(current_value) = token_price_mon {
                        let entry = pos.entry_price_mon.max(pos.amount_mon);

                        // Update highest value (ATH)
                        let mut updated_pos = pos.clone();
                        if current_value > updated_pos.highest_value_mon {
                            updated_pos.highest_value_mon = current_value;
                            positions.insert(addr_str.clone(), updated_pos.clone());
                            save_needed = true;
                        }

                        // Calculate PnL
                        let pnl_pct = if entry > 0.0 {
                            ((current_value - entry) / entry) * 100.0
                        } else {
                            0.0
                        };

                        let drop_from_ath = if updated_pos.highest_value_mon > 0.0 {
                            ((updated_pos.highest_value_mon - current_value)
                                / updated_pos.highest_value_mon)
                                * 100.0
                        } else {
                            0.0
                        };

                        let emoji = if pnl_pct > 100.0 {
                            "🔥🔥"
                        } else if pnl_pct > 50.0 {
                            "🔥"
                        } else if pnl_pct > 0.0 {
                            "📈"
                        } else if pnl_pct > -20.0 {
                            "📉"
                        } else {
                            "💀"
                        };

                        log(&format!(
                            "   {} {} | {:.4} MON ({:+.1}%) | ATH drop: {:.1}% [{}]",
                            emoji,
                            pos.token_name,
                            current_value,
                            pnl_pct,
                            drop_from_ath,
                            price_source
                        ));

                        let mut should_sell = false;
                        let mut sell_reason = String::new();
                        let mut sell_amount = current_balance;

                        // 🎯 Check for AI-set dynamic stop-loss first (Jesse-style)
                        let effective_stop = dynamic_stops
                            .get_stop_for_token(&pos.token_name)
                            .unwrap_or(settings.hard_stop_loss_pct);

                        // 💀 DYNAMIC/HARD STOP LOSS
                        if pnl_pct <= effective_stop {
                            should_sell = true;
                            if dynamic_stops.get_stop_for_token(&pos.token_name).is_some() {
                                sell_reason = format!(
                                    "🤖 AI STOP LOSS ({:.1}% <= {}%)",
                                    pnl_pct, effective_stop
                                );
                            } else {
                                sell_reason = format!(
                                    "💀 HARD STOP LOSS ({:.1}% <= {}%)",
                                    pnl_pct, effective_stop
                                );
                            }
                        }
                        // 🚨 WHALE EXIT - The whale we followed is selling! URGENT SELL
                        else if updated_pos.whale_exited {
                            should_sell = true;
                            sell_reason = format!(
                                "🚨 WHALE EXIT! Following whale sold - current PnL: {:.1}%",
                                pnl_pct
                            );
                            // Sell everything - whale knows something we don't
                            sell_amount = U256::from((balance_tokens * 1e18) as u128);
                        }
                        // 📉 TRAILING STOP (aktywny po 20% zysku, drop 10% od ATH = sprzedaj)
                        // Obniżony z 30% do 20% żeby chronić zyski wcześniej
                        else if pnl_pct > 20.0 && drop_from_ath >= 10.0 {
                            should_sell = true;
                            sell_reason = format!(
                                "📉 TRAILING STOP (profit {:.1}%, drop {:.1}% >= 10%)",
                                pnl_pct, drop_from_ath
                            );
                        }
                        // 🛡️ BREAK-EVEN STOP (po +15% zysku, jeśli spadnie do 0% = sprzedaj)
                        else if updated_pos.highest_value_mon > entry * 1.15 && pnl_pct <= 0.0 {
                            should_sell = true;
                            sell_reason = format!(
                                "🛡️ BREAK-EVEN STOP (was +{:.1}%, now {:.1}%)",
                                (updated_pos.highest_value_mon / entry - 1.0) * 100.0,
                                pnl_pct
                            );
                        }
                        // ═══════════════════════════════════════════════════════════════
                        // 🎯 JESSE-STYLE MULTI-LEVEL TAKE PROFIT
                        // Level 0: +30% profit -> sell 25% (NOWE - szybki profit)
                        // Level 1: +50% profit -> sell 30%
                        // Level 2: +100% profit (2x) -> sell 30%
                        // Level 3: +200% profit (3x) -> sell remaining 40% (or keep as moonbag)
                        // ═══════════════════════════════════════════════════════════════

                        // 💵 LEVEL 0: +30% profit - sell 25% (quick profit!)
                        else if pnl_pct >= 30.0
                            && !updated_pos.moonbag_secured
                            && drop_from_ath < 10.0
                        {
                            // Sprzedaj 25% jeśli mamy 30%+ profit i token wciąż blisko ATH
                            should_sell = true;
                            sell_reason =
                                format!("💵 QUICK TP (+{:.1}% >= 30%) - selling 25%", pnl_pct);
                            let sell_portion = (balance_tokens * 0.25 * 1e18) as u128;
                            sell_amount = U256::from(sell_portion);

                            updated_pos.moonbag_secured = true; // Use this flag for level 0
                            positions.insert(addr_str.clone(), updated_pos.clone());
                            save_needed = true;
                        }
                        // 💰 LEVEL 1: +50% profit - sell 30%
                        else if pnl_pct >= 50.0 && !updated_pos.tp_level_1_taken {
                            should_sell = true;
                            sell_reason =
                                format!("💰 TP LEVEL 1 (+{:.1}% >= 50%) - selling 30%", pnl_pct);
                            let sell_portion = (balance_tokens * 0.30 * 1e18) as u128;
                            sell_amount = U256::from(sell_portion);

                            updated_pos.tp_level_1_taken = true;
                            positions.insert(addr_str.clone(), updated_pos.clone());
                            save_needed = true;
                        }
                        // 💎 LEVEL 2: +100% profit (2x) - sell 30%
                        else if pnl_pct >= 100.0
                            && updated_pos.tp_level_1_taken
                            && !updated_pos.tp_level_2_taken
                        {
                            should_sell = true;
                            sell_reason =
                                format!("💎 TP LEVEL 2 (+{:.1}% >= 100%) - selling 30%", pnl_pct);
                            let sell_portion = (balance_tokens * 0.30 * 1e18) as u128;
                            sell_amount = U256::from(sell_portion);

                            updated_pos.tp_level_2_taken = true;
                            positions.insert(addr_str.clone(), updated_pos.clone());
                            save_needed = true;
                        }
                        // 🚀 LEVEL 3: +200% profit (3x) - sell 50% of remaining, keep moonbag
                        else if pnl_pct >= 200.0
                            && updated_pos.tp_level_2_taken
                            && !updated_pos.tp_level_3_taken
                        {
                            should_sell = true;
                            sell_reason = format!(
                                "🚀 TP LEVEL 3 (+{:.1}% >= 200%) - selling 50%, keeping moonbag",
                                pnl_pct
                            );
                            let sell_portion = (balance_tokens * 0.50 * 1e18) as u128;
                            sell_amount = U256::from(sell_portion);

                            updated_pos.tp_level_3_taken = true;
                            updated_pos.moonbag_secured = true;
                            positions.insert(addr_str.clone(), updated_pos.clone());
                            save_needed = true;
                        }
                        // 🌙 MOONBAG EXIT: +500% profit (6x) - sell everything, massive gains
                        else if pnl_pct >= 500.0 && updated_pos.tp_level_3_taken {
                            should_sell = true;
                            sell_reason = format!(
                                "🌙 MOONBAG EXIT (+{:.1}% >= 500%) - taking full profit!",
                                pnl_pct
                            );
                        }

                        if should_sell {
                            log(&format!("   🚨 {} -> SELLING!", sell_reason));

                            let now = SystemTime::now()
                                .duration_since(SystemTime::UNIX_EPOCH)
                                .unwrap()
                                .as_secs();
                            let deadline = U256::from(now + 300);

                            // First: Approve tokens for router
                            log(&format!("   🔑 Approving tokens for router..."));
                            let approve_call = approveCall {
                                spender: router_address,
                                amount: sell_amount,
                            };
                            let approve_tx = TransactionRequest::default()
                                .to(token_address)
                                .input(approve_call.abi_encode().into())
                                .gas_limit(100_000);

                            match provider.send_transaction(approve_tx).await {
                                Ok(pending) => {
                                    if let Err(e) = pending.get_receipt().await {
                                        log(&format!("   ⚠️ Approve receipt error: {:?}", e));
                                    }
                                }
                                Err(e) => {
                                    log(&format!("   ❌ Approve failed: {:?}", e));
                                    continue;
                                }
                            }

                            log(&format!(
                                "   🔧 Using Router.sell() - Amount: {}, Token: {:?}",
                                sell_amount, token_address
                            ));

                            // NAD.FUN v3 ABI - sell(SellParams)
                            let sell_params = SellParams {
                                amountIn: sell_amount,
                                amountOutMin: U256::from(1), // 1 wei minimum
                                token: token_address,
                                to: my_address,
                                deadline,
                            };
                            let sell_call = sellCall {
                                params: sell_params,
                            };

                            let tx = TransactionRequest::default()
                                .to(router_address)
                                .input(sell_call.abi_encode().into())
                                .gas_limit(500_000)
                                .max_priority_fee_per_gas(100_000_000_000);

                            match provider.send_transaction(tx).await {
                                Ok(pending) => {
                                    log("   ⏳ Wysyłam transakcję...");
                                    match pending.get_receipt().await {
                                        Ok(receipt) => {
                                            log(&format!(
                                                "   ✅ SPRZEDANE! Hash: {:?}",
                                                receipt.transaction_hash
                                            ));

                                            // 📜 Save to trade history
                                            let trade_record = serde_json::json!({
                                                "token_address": addr_str,
                                                "token_name": pos.token_name,
                                                "entry_mon": pos.amount_mon,
                                                "exit_mon": token_price_mon.unwrap_or(0.0),
                                                "pnl": token_price_mon.unwrap_or(0.0) - pos.amount_mon,
                                                "pnl_pct": pnl_pct,
                                                "reason": sell_reason,
                                                "timestamp": SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs(),
                                                "tx_hash": format!("{:?}", receipt.transaction_hash)
                                            });

                                            let history_path = "trades_history.json";
                                            let mut history: Vec<serde_json::Value> =
                                                fs::read_to_string(history_path)
                                                    .ok()
                                                    .and_then(|s| serde_json::from_str(&s).ok())
                                                    .unwrap_or_default();
                                            history.push(trade_record);
                                            let _ = fs::write(
                                                history_path,
                                                serde_json::to_string_pretty(&history).unwrap(),
                                            );

                                            // 📱 Telegram alert: Sold position
                                            if !tg_token.is_empty() && !tg_chat_id.is_empty() {
                                                let msg = format!(
                                                    "💰 SOLD {}\n📊 P&L: {:.1}%\n💵 Entry: {:.1} MON\n📍 Reason: {}\n🔗 TX: {:?}",
                                                    pos.token_name,
                                                    pnl_pct,
                                                    pos.amount_mon,
                                                    sell_reason,
                                                    receipt.transaction_hash
                                                );
                                                let _ = send_telegram(
                                                    &http_client,
                                                    &tg_token,
                                                    &tg_chat_id,
                                                    &msg,
                                                )
                                                .await;
                                            }

                                            if sell_amount == current_balance {
                                                to_remove.push(addr_str.clone());
                                            }
                                        }
                                        Err(e) => log(&format!("   ❌ Receipt error: {:?}", e)),
                                    }
                                }
                                Err(e) => log(&format!("   ❌ TX error: {:?}", e)),
                            }
                        }
                    } else {
                        log(&format!(
                            "   ⚠️ {} - Nie mogę pobrać ceny (czekam...)",
                            pos.token_name
                        ));
                    }
                }

                for addr in to_remove {
                    positions.remove(&addr);
                    save_needed = true;
                }

                if save_needed {
                    let _ = fs::write(path, serde_json::to_string_pretty(&positions).unwrap());
                }
            }
        } else {
            log("📊 Brak pliku positions.json - czekam na zakupy...");
        }

        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}
