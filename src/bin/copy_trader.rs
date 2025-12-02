use alloy::{
    consensus::Transaction as _,
    network::EthereumWallet,
    primitives::{Address, U256},
    providers::{Provider, ProviderBuilder, WalletProvider},
    rpc::types::BlockTransactionsKind,
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
    thread,
    time::{Duration, SystemTime},
};
use url::Url;

// ═══════════════════════════════════════════════════════════════════════════════
// 🐳 GOD MODE COPY TRADER v5.0 - CONFIG.JSON EDITION 🐳
// ═══════════════════════════════════════════════════════════════════════════════
// - Czyta whale'ów z config.json (hot-reload!)
// - Kopiuje ich zakupy automatycznie
// - Position Manager zajmuje się trailing stop
// ═══════════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════════
// 📋 CONFIG STRUCTURES
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Config {
    whales: Vec<WhaleConfig>,
    settings: Settings,
    blacklist: Blacklist,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct WhaleConfig {
    address: String,
    name: String,
    copy_percentage: f64,
    enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Settings {
    min_buy_amount_mon: f64,
    max_buy_amount_mon: f64,
    min_target_value_mon: f64,
    cooldown_seconds: u64,
    trailing_stop_pct: f64,
    hard_stop_loss_pct: f64,
    take_profit_pct: f64,
    moonbag_portion: f64,
    // Entry filters
    #[serde(default = "default_min_liquidity")]
    min_liquidity_usd: f64,
    #[serde(default = "default_min_quality_score")]
    min_quality_score: u8,
    #[serde(default = "default_max_creator_rugs")]
    max_creator_rugs: u32,
    #[serde(default = "default_enable_quality_filter")]
    enable_quality_filter: bool,
}

fn default_min_liquidity() -> f64 {
    500.0
}
fn default_min_quality_score() -> u8 {
    40
}
fn default_max_creator_rugs() -> u32 {
    2
}
fn default_enable_quality_filter() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Blacklist {
    tokens: Vec<String>,
    creators: Vec<String>,
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 DEXSCREENER API STRUCTURES
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Deserialize)]
struct DexScreenerResponse {
    pairs: Option<Vec<DexPair>>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct DexPair {
    chain_id: Option<String>,
    price_usd: Option<String>,
    liquidity: Option<Liquidity>,
    volume: Option<Volume>,
    transactions: Option<Transactions>,
    price_change: Option<PriceChange>,
    base_token: Option<BaseToken>,
}

#[derive(Debug, Deserialize)]
struct Liquidity {
    usd: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct Volume {
    h24: Option<f64>,
    h6: Option<f64>,
    h1: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct Transactions {
    h24: Option<TxCounts>,
    h6: Option<TxCounts>,
    h1: Option<TxCounts>,
}

#[derive(Debug, Deserialize)]
struct TxCounts {
    buys: Option<u32>,
    sells: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct PriceChange {
    h24: Option<f64>,
    h6: Option<f64>,
    h1: Option<f64>,
    m5: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct BaseToken {
    name: Option<String>,
    symbol: Option<String>,
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🏆 CREATOR REPUTATION TRACKING
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct CreatorStats {
    tokens_created: u32,
    successful_tokens: u32,
    rugged_tokens: u32,
    last_seen: u64,
}

fn load_creator_db() -> HashMap<String, CreatorStats> {
    if let Ok(data) = fs::read_to_string("creators.json") {
        serde_json::from_str(&data).unwrap_or_default()
    } else {
        HashMap::new()
    }
}

fn save_creator_db(db: &HashMap<String, CreatorStats>) {
    if let Ok(json) = serde_json::to_string_pretty(db) {
        let _ = fs::write("creators.json", json);
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🔍 TOKEN QUALITY ANALYSIS
// ═══════════════════════════════════════════════════════════════════════════════

async fn get_token_info(client: &Client, token_address: &str) -> Option<DexPair> {
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
        Ok(resp) => {
            if let Ok(data) = resp.json::<DexScreenerResponse>().await {
                if let Some(pairs) = data.pairs {
                    return pairs
                        .into_iter()
                        .filter(|p| p.chain_id.as_deref() == Some("monad"))
                        .max_by(|a, b| {
                            let liq_a = a.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
                            let liq_b = b.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
                            liq_a.partial_cmp(&liq_b).unwrap()
                        });
                }
            }
        }
        Err(_) => {}
    }
    None
}

#[derive(Debug)]
struct TokenQuality {
    score: u8,
    liquidity_usd: f64,
    volume_24h: f64,
    buy_sell_ratio: f64,
    price_change_1h: f64,
    reasons: Vec<String>,
    token_name: String,
}

async fn analyze_token_quality(client: &Client, token_address: &str) -> TokenQuality {
    let mut quality = TokenQuality {
        score: 50,
        liquidity_usd: 0.0,
        volume_24h: 0.0,
        buy_sell_ratio: 1.0,
        price_change_1h: 0.0,
        reasons: Vec::new(),
        token_name: String::new(),
    };

    if let Some(pair) = get_token_info(client, token_address).await {
        quality.liquidity_usd = pair.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
        quality.volume_24h = pair.volume.as_ref().and_then(|v| v.h24).unwrap_or(0.0);
        quality.price_change_1h = pair.price_change.as_ref().and_then(|p| p.h1).unwrap_or(0.0);
        quality.token_name = pair
            .base_token
            .as_ref()
            .and_then(|t| t.name.clone())
            .unwrap_or_else(|| format!("Token_{}", &token_address[..8]));

        let buys = pair
            .transactions
            .as_ref()
            .and_then(|t| t.h24.as_ref())
            .and_then(|t| t.buys)
            .unwrap_or(0);
        let sells = pair
            .transactions
            .as_ref()
            .and_then(|t| t.h24.as_ref())
            .and_then(|t| t.sells)
            .unwrap_or(1);
        quality.buy_sell_ratio = buys as f64 / sells.max(1) as f64;

        // Scoring logic
        // Liquidity scoring
        if quality.liquidity_usd > 10000.0 {
            quality.score += 15;
            quality
                .reasons
                .push(format!("🌊 Liq ${:.0}k", quality.liquidity_usd / 1000.0));
        } else if quality.liquidity_usd > 5000.0 {
            quality.score += 10;
            quality
                .reasons
                .push(format!("💧 Liq ${:.0}k", quality.liquidity_usd / 1000.0));
        } else if quality.liquidity_usd > 1000.0 {
            quality.score += 5;
            quality
                .reasons
                .push(format!("💦 Liq ${:.0}", quality.liquidity_usd));
        } else if quality.liquidity_usd > 0.0 {
            quality.score = quality.score.saturating_sub(10);
            quality.reasons.push("⚠️ Low liq".to_string());
        }

        // Volume scoring
        if quality.volume_24h > 50000.0 {
            quality.score += 15;
            quality
                .reasons
                .push(format!("📈 Vol ${:.0}k", quality.volume_24h / 1000.0));
        } else if quality.volume_24h > 10000.0 {
            quality.score += 10;
            quality
                .reasons
                .push(format!("📊 Vol ${:.0}k", quality.volume_24h / 1000.0));
        }

        // Buy/sell ratio scoring
        if quality.buy_sell_ratio > 3.0 {
            quality.score += 15;
            quality
                .reasons
                .push(format!("🔥 Bullish {:.1}x", quality.buy_sell_ratio));
        } else if quality.buy_sell_ratio > 1.5 {
            quality.score += 8;
            quality
                .reasons
                .push(format!("📗 Buying {:.1}x", quality.buy_sell_ratio));
        } else if quality.buy_sell_ratio < 0.5 {
            quality.score = quality.score.saturating_sub(15);
            quality.reasons.push("📕 Heavy selling".to_string());
        }

        // Price change scoring
        if quality.price_change_1h > 50.0 {
            quality.score = quality.score.saturating_sub(10);
            quality.reasons.push("⚠️ Pumped already".to_string());
        } else if quality.price_change_1h > 20.0 {
            quality.score += 5;
            quality.reasons.push("🚀 Momentum".to_string());
        } else if quality.price_change_1h < -30.0 {
            quality.score = quality.score.saturating_sub(20);
            quality.reasons.push("🔻 Dumping".to_string());
        }

        quality.score = quality.score.min(100);
    } else {
        quality.reasons.push("❓ New token (no data)".to_string());
    }

    quality
}

/// Calculate dynamic buy amount based on quality score
fn calculate_smart_amount(quality_score: u8, min_mon: f64, max_mon: f64) -> f64 {
    if quality_score >= 85 {
        max_mon
    } else if quality_score >= 75 {
        min_mon + (max_mon - min_mon) * 0.7
    } else if quality_score >= 65 {
        min_mon + (max_mon - min_mon) * 0.4
    } else if quality_score >= 55 {
        min_mon
    } else if quality_score >= 40 {
        min_mon * 0.5
    } else {
        0.0
    }
}

fn load_config() -> Result<Config> {
    let config_str = fs::read_to_string("config.json").context("Nie można odczytać config.json")?;
    let config: Config =
        serde_json::from_str(&config_str).context("Błąd parsowania config.json")?;
    Ok(config)
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 POSITION TRACKING
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Serialize, Deserialize, Clone)]
struct Position {
    token_address: String,
    token_name: String,
    amount_mon: f64,
    entry_price_mon: f64,
    peak_price_mon: f64,
    timestamp: u64,
    trailing_active: bool,
    partial_sold: bool,
    copied_from: String,
}

fn save_positions(positions: &HashMap<String, Position>) -> Result<()> {
    let json = serde_json::to_string_pretty(positions)?;
    fs::write("positions.json", json)?;
    Ok(())
}

fn load_positions() -> HashMap<String, Position> {
    if let Ok(data) = fs::read_to_string("positions.json") {
        serde_json::from_str(&data).unwrap_or_default()
    } else {
        HashMap::new()
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📋 HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

fn log(msg: &str) {
    println!("[{}] {}", Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
}

fn wei_to_mon(wei: U256) -> f64 {
    wei.to_string().parse::<f64>().unwrap_or(0.0) / 1e18
}

sol! {
    struct BuyParams {
        uint256 amountOutMin;
        address token;
        address to;
        uint256 deadline;
    }
    function buy(BuyParams params) external payable;

    struct SellParams {
        address token;
        uint256 amount;
        uint256 amountOutMin;
        address to;
        uint256 deadline;
    }
    function sell(SellParams params) external;
}

sol! {
    #[sol(rpc)]
    interface IERC20 {
        function balanceOf(address account) external view returns (uint256);
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 MAIN
// ═══════════════════════════════════════════════════════════════════════════════

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();

    log("╔═══════════════════════════════════════════════════════════════╗");
    log("║  🐳 GOD MODE COPY TRADER v5.0 - CONFIG.JSON EDITION 🐳       ║");
    log("╚═══════════════════════════════════════════════════════════════╝");

    // Load config
    let mut config = load_config()?;
    let mut last_config_check = SystemTime::now();

    log(&format!(
        "📋 Loaded {} whales from config.json",
        config.whales.len()
    ));
    for whale in &config.whales {
        if whale.enabled {
            log(&format!(
                "   🐳 {} - {} ({}%)",
                whale.name,
                &whale.address[..12],
                whale.copy_percentage
            ));
        }
    }
    log(&format!(
        "⚙️  Min: {} MON | Max: {} MON | Cooldown: {}s",
        config.settings.min_buy_amount_mon,
        config.settings.max_buy_amount_mon,
        config.settings.cooldown_seconds
    ));

    let rpc_url_str = env::var("MONAD_RPC_URL").context("Brak MONAD_RPC_URL")?;
    let private_key = env::var("PRIVATE_KEY").context("Brak PRIVATE_KEY")?;

    let rpc_url = Url::parse(&rpc_url_str)?;
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer);

    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_http(rpc_url);

    let my_address = provider.wallet().default_signer().address();
    let chain_id = provider.get_chain_id().await?;

    log(&format!("🔗 Chain ID: {}", chain_id));
    log(&format!("👤 Bot Wallet: {:?}", my_address));

    let router_str = env::var("ROUTER_ADDRESS")
        .unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let router_address = Address::from_str(&router_str).context("Nieprawidłowy ROUTER_ADDRESS")?;
    log(&format!("📍 Router: {:?}", router_address));

    // HTTP client for DexScreener API
    let http_client = Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .context("Failed to create HTTP client")?;

    // Creator reputation database
    let mut creator_db = load_creator_db();
    log(&format!(
        "📊 Creator DB: {} creators tracked",
        creator_db.len()
    ));

    // Entry filter settings
    log(&format!(
        "🔍 Entry Filters: min_liq=${} | min_score={} | quality_filter={}",
        config.settings.min_liquidity_usd,
        config.settings.min_quality_score,
        config.settings.enable_quality_filter
    ));

    let mut positions = load_positions();
    let mut last_buy_time: HashMap<String, u64> = HashMap::new();

    log(&format!("📊 Loaded {} positions", positions.len()));
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

    let mut last_block_number = loop {
        match provider.get_block_number().await {
            Ok(n) => break n,
            Err(e) => {
                log(&format!("❌ Start error: {:?}", e));
                thread::sleep(Duration::from_secs(5));
            }
        }
    };

    log(&format!("📦 Starting from block: {}", last_block_number));

    loop {
        // Hot-reload config every 30 seconds
        if last_config_check.elapsed().unwrap_or_default() > Duration::from_secs(30) {
            if let Ok(new_config) = load_config() {
                if new_config.whales.len() != config.whales.len() {
                    log(&format!(
                        "🔄 Config reloaded! {} whales",
                        new_config.whales.len()
                    ));
                }
                config = new_config;
            }
            last_config_check = SystemTime::now();
        }

        // Get enabled whale addresses
        let target_wallets: Vec<Address> = config
            .whales
            .iter()
            .filter(|w| w.enabled)
            .filter_map(|w| Address::from_str(&w.address).ok())
            .collect();

        match provider.get_block_number().await {
            Ok(current_block_number) => {
                if current_block_number > last_block_number {
                    match provider
                        .get_block_by_number(
                            current_block_number.into(),
                            BlockTransactionsKind::Full,
                        )
                        .await
                    {
                        Ok(Some(block)) => {
                            if let Some(txs) = block.transactions.as_transactions() {
                                for tx in txs {
                                    let tx_from = tx.from;

                                    // Check if this tx is from one of our whales
                                    if !target_wallets.contains(&tx_from) {
                                        continue;
                                    }

                                    // Find which whale this is
                                    let whale = config.whales.iter().find(|w| {
                                        Address::from_str(&w.address).ok() == Some(tx_from)
                                    });

                                    let whale_name = whale
                                        .map(|w| w.name.clone())
                                        .unwrap_or_else(|| format!("{:?}", tx_from));
                                    let copy_pct =
                                        whale.map(|w| w.copy_percentage).unwrap_or(100.0);

                                    if let Some(to_address) = tx.to() {
                                        if to_address == router_address {
                                            let value_mon = wei_to_mon(tx.inner.value());

                                            // Try to decode BUY
                                            if let Ok(decoded_buy) =
                                                buyCall::abi_decode(tx.inner.input(), true)
                                            {
                                                let params = decoded_buy.params;
                                                let token_str = format!("{:?}", params.token);

                                                // Check minimum value
                                                if value_mon < config.settings.min_target_value_mon
                                                {
                                                    continue;
                                                }

                                                // Check blacklist
                                                if config.blacklist.tokens.iter().any(|t| {
                                                    token_str
                                                        .to_lowercase()
                                                        .contains(&t.to_lowercase())
                                                }) {
                                                    log(&format!(
                                                        "🚫 Token {} is blacklisted",
                                                        &token_str[..12]
                                                    ));
                                                    continue;
                                                }

                                                // Cooldown check
                                                let now = SystemTime::now()
                                                    .duration_since(SystemTime::UNIX_EPOCH)
                                                    .unwrap()
                                                    .as_secs();
                                                if let Some(&last_time) =
                                                    last_buy_time.get(&token_str)
                                                {
                                                    if now - last_time
                                                        < config.settings.cooldown_seconds
                                                    {
                                                        log(&format!(
                                                            "⏳ Cooldown for {} ({}s)",
                                                            &token_str[..12],
                                                            config.settings.cooldown_seconds
                                                        ));
                                                        continue;
                                                    }
                                                }

                                                log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                                log(&format!(
                                                    "🚨 WHALE BUY DETECTED! 🐳 {}",
                                                    whale_name
                                                ));
                                                log(&format!("   💎 Token: {}", &token_str[..16]));
                                                log(&format!(
                                                    "   💰 Whale spent: {:.2} MON",
                                                    value_mon
                                                ));

                                                // ═══════════════════════════════════════════════════════════════
                                                // 🔍 QUALITY FILTER - Check token before buying
                                                // ═══════════════════════════════════════════════════════════════
                                                let quality =
                                                    analyze_token_quality(&http_client, &token_str)
                                                        .await;

                                                log(&format!(
                                                    "   📊 Quality Score: {}/100 | {}",
                                                    quality.score,
                                                    quality.reasons.join(" | ")
                                                ));

                                                if config.settings.enable_quality_filter {
                                                    // Check minimum liquidity
                                                    if quality.liquidity_usd > 0.0
                                                        && quality.liquidity_usd
                                                            < config.settings.min_liquidity_usd
                                                    {
                                                        log(&format!("   ❌ SKIPPED: Liquidity ${:.0} < ${:.0} min", 
                                                            quality.liquidity_usd, config.settings.min_liquidity_usd));
                                                        continue;
                                                    }

                                                    // Check minimum quality score
                                                    if quality.score
                                                        < config.settings.min_quality_score
                                                    {
                                                        log(&format!(
                                                            "   ❌ SKIPPED: Score {} < {} min",
                                                            quality.score,
                                                            config.settings.min_quality_score
                                                        ));
                                                        continue;
                                                    }

                                                    // Check for heavy dumping
                                                    if quality.price_change_1h < -40.0 {
                                                        log(&format!(
                                                            "   ❌ SKIPPED: Dumping {:.1}% in 1h",
                                                            quality.price_change_1h
                                                        ));
                                                        continue;
                                                    }
                                                }

                                                // Calculate smart buy amount based on quality
                                                let base_amount = value_mon * copy_pct / 100.0;
                                                let quality_adjusted = calculate_smart_amount(
                                                    quality.score,
                                                    config.settings.min_buy_amount_mon,
                                                    config.settings.max_buy_amount_mon,
                                                );
                                                let calculated_amount = base_amount
                                                    .min(quality_adjusted)
                                                    .min(config.settings.max_buy_amount_mon)
                                                    .max(if quality.score >= 40 {
                                                        config.settings.min_buy_amount_mon * 0.5
                                                    } else {
                                                        0.0
                                                    });

                                                if calculated_amount < 0.01 {
                                                    log("   ❌ SKIPPED: Quality too low for any position");
                                                    continue;
                                                }

                                                let emoji = if calculated_amount
                                                    >= config.settings.max_buy_amount_mon * 0.8
                                                {
                                                    "🐳"
                                                } else if calculated_amount
                                                    >= config.settings.max_buy_amount_mon * 0.5
                                                {
                                                    "🦈"
                                                } else {
                                                    "🐟"
                                                };

                                                log(&format!(
                                                    "   {} Copying: {:.2} MON ({}%)",
                                                    emoji, calculated_amount, copy_pct
                                                ));

                                                let buy_value_wei =
                                                    U256::from((calculated_amount * 1e18) as u128);
                                                let deadline = U256::from(now + 300);

                                                let buy_params = BuyParams {
                                                    amountOutMin: U256::from(1),
                                                    token: params.token,
                                                    to: my_address,
                                                    deadline,
                                                };

                                                let buy_call = buyCall { params: buy_params };
                                                let calldata = buy_call.abi_encode();

                                                let tx_request =
                                                    alloy::rpc::types::TransactionRequest::default(
                                                    )
                                                    .to(router_address)
                                                    .value(buy_value_wei)
                                                    .input(calldata.into())
                                                    .gas_limit(8_000_000)
                                                    .max_priority_fee_per_gas(500_000_000_000);

                                                match provider.send_transaction(tx_request).await {
                                                    Ok(pending_tx) => {
                                                        log("   ⏳ Sending...");
                                                        match pending_tx.get_receipt().await {
                                                            Ok(receipt) => {
                                                                log(&format!(
                                                                    "   ✅ BOUGHT! Hash: {:?}",
                                                                    receipt.transaction_hash
                                                                ));

                                                                // Save position with quality data
                                                                let token_name = if quality
                                                                    .token_name
                                                                    .is_empty()
                                                                {
                                                                    format!(
                                                                        "Copy_{}",
                                                                        &token_str[..8]
                                                                    )
                                                                } else {
                                                                    quality.token_name.clone()
                                                                };
                                                                positions.insert(
                                                                    token_str.clone(),
                                                                    Position {
                                                                        token_address: token_str
                                                                            .clone(),
                                                                        token_name,
                                                                        amount_mon:
                                                                            calculated_amount,
                                                                        entry_price_mon:
                                                                            calculated_amount,
                                                                        peak_price_mon:
                                                                            calculated_amount,
                                                                        timestamp: now,
                                                                        trailing_active: false,
                                                                        partial_sold: false,
                                                                        copied_from: whale_name
                                                                            .clone(),
                                                                    },
                                                                );
                                                                last_buy_time
                                                                    .insert(token_str.clone(), now);

                                                                let _ = save_positions(&positions);
                                                                log(&format!(
                                                                    "   📊 Positions: {}",
                                                                    positions.len()
                                                                ));
                                                            }
                                                            Err(e) => log(&format!(
                                                                "   ❌ Receipt error: {:?}",
                                                                e
                                                            )),
                                                        }
                                                    }
                                                    Err(e) => {
                                                        log(&format!("   ❌ TX error: {:?}", e))
                                                    }
                                                }
                                            }
                                            // Try to decode SELL (copy sells too!)
                                            else if let Ok(decoded_sell) =
                                                sellCall::abi_decode(tx.inner.input(), true)
                                            {
                                                let params = decoded_sell.params;
                                                let token_str = format!("{:?}", params.token);

                                                if let Some(_position) = positions.get(&token_str) {
                                                    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                                    log(&format!(
                                                        "🚨 WHALE SELL DETECTED! 🐳 {}",
                                                        whale_name
                                                    ));
                                                    log(&format!(
                                                        "   💎 Token: {}",
                                                        &token_str[..16]
                                                    ));

                                                    // Check our balance
                                                    let token_contract =
                                                        IERC20::new(params.token, provider.clone());
                                                    let my_balance = match token_contract
                                                        .balanceOf(my_address)
                                                        .call()
                                                        .await
                                                    {
                                                        Ok(bal) => bal._0,
                                                        Err(e) => {
                                                            log(&format!(
                                                                "   ❌ Failed to get balance: {:?}",
                                                                e
                                                            ));
                                                            continue;
                                                        }
                                                    };

                                                    if my_balance == U256::ZERO {
                                                        log("   ⚠️ We have 0 tokens. Nothing to sell.");
                                                        continue;
                                                    }

                                                    log(&format!("   ⚡ Auto-selling our position! Amount: {}", my_balance));

                                                    let now = SystemTime::now()
                                                        .duration_since(SystemTime::UNIX_EPOCH)
                                                        .unwrap()
                                                        .as_secs();
                                                    let deadline = U256::from(now + 300);

                                                    let sell_params = SellParams {
                                                        token: params.token,
                                                        amount: my_balance,
                                                        amountOutMin: U256::from(1),
                                                        to: my_address,
                                                        deadline,
                                                    };

                                                    let sell_call = sellCall {
                                                        params: sell_params,
                                                    };
                                                    let calldata = sell_call.abi_encode();

                                                    let tx_request = alloy::rpc::types::TransactionRequest::default()
                                                        .to(router_address)
                                                        .input(calldata.into())
                                                        .gas_limit(8_000_000)
                                                        .max_priority_fee_per_gas(500_000_000_000);

                                                    match provider
                                                        .send_transaction(tx_request)
                                                        .await
                                                    {
                                                        Ok(pending_tx) => {
                                                            log("   ⏳ Selling...");
                                                            match pending_tx.get_receipt().await {
                                                                Ok(receipt) => {
                                                                    log(&format!(
                                                                        "   ✅ SOLD! Hash: {:?}",
                                                                        receipt.transaction_hash
                                                                    ));
                                                                    positions.remove(&token_str);
                                                                    let _ =
                                                                        save_positions(&positions);
                                                                }
                                                                Err(e) => log(&format!(
                                                                    "   ❌ Error: {:?}",
                                                                    e
                                                                )),
                                                            }
                                                        }
                                                        Err(e) => {
                                                            log(&format!("   ❌ TX error: {:?}", e))
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            last_block_number = current_block_number;
                        }
                        Ok(None) => {}
                        Err(e) => log(&format!("❌ Block error: {:?}", e)),
                    }
                }
            }
            Err(e) => {
                log(&format!("❌ RPC error: {:?}", e));
                thread::sleep(Duration::from_secs(10));
            }
        }

        thread::sleep(Duration::from_millis(200));
    }
}
