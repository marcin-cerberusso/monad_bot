use alloy::{
    consensus::Transaction as _,
    network::EthereumWallet,
    primitives::{Address, B256, U256},
    providers::{Provider, ProviderBuilder, WsConnect},
    rpc::types::BlockTransactionsKind,
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use anyhow::Result;
use dotenv::dotenv;
use std::{
    collections::{HashMap, HashSet},
    env,
    str::FromStr,
    time::{SystemTime, UNIX_EPOCH},
};

use futures::StreamExt;
// Removed redis - using file-based blocklist instead
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::json;

// Tracing
use tracing::{debug, error, info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

// ═══════════════════════════════════════════════════════════════════════════════
// 🔥 GOD MODE v3: WEBSOCKETS + CREATOR ANALYSIS + DEXSCREENER 🔥
// ═══════════════════════════════════════════════════════════════════════════════
// - WebSockets (wss://) dla minimalnego opóźnienia
// - Analiza Twórcy (Creator Reputation)
// - DexScreener Check
// - Whale Mode (Dynamic Amount)
// ═══════════════════════════════════════════════════════════════════════════════

// NAD.FUN v3 BondingCurveRouter - buy function with BuyParams struct
sol! {
    #[derive(Debug)]
    struct BuyParams {
        uint256 amountOutMin;
        address token;
        address to;
        uint256 deadline;
    }

    #[derive(Debug)]
    function buy(BuyParams params) external payable;
}

// Lens quote
sol! {
    #[derive(Debug)]
    function getAmountOut(address token, uint256 amountIn, bool isBuy) external view returns (address router, uint256 amountOut);
}

// NAD.FUN Lens for on-chain quotes
const NADFUN_LENS: &str = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea";
const DEFAULT_SLIPPAGE_BPS: u128 = 200; // 2%
const DEFAULT_MIN_LIQ_USD: f64 = 3000.0;
const DEFAULT_LIQ_PCT: f64 = 0.01; // max 1% liq per buy (approx, assuming MON ~ USD)
const DEFAULT_BUNDLE_MIN_COUNT: u32 = 3;
const DEFAULT_MAX_GAS_GWEI: u64 = 120;

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 DEXSCREENER TYPES
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Deserialize)]
struct DexScreenerResponse {
    pairs: Option<Vec<DexPair>>,
}

#[derive(Debug, Deserialize)]
struct DexPair {
    #[serde(rename = "chainId")]
    chain_id: Option<String>,
    liquidity: Option<Liquidity>,
    volume: Option<Volume>,
    #[serde(rename = "txns")]
    transactions: Option<Transactions>,
    #[serde(rename = "priceChange")]
    price_change: Option<PriceChange>,
}

#[derive(Debug, Deserialize)]
struct PriceChange {
    h1: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct Liquidity {
    usd: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct Volume {
    h24: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct Transactions {
    h24: Option<TxCount>,
}

#[derive(Debug, Deserialize)]
struct TxCount {
    buys: Option<u32>,
    sells: Option<u32>,
}

/// Check file-based blocklist (Python blocklist.py)
async fn is_risk_blocked(token: &str) -> bool {
    // Read blocked_tokens.json directly
    let blocklist_path = std::path::Path::new("blocked_tokens.json");
    if !blocklist_path.exists() {
        return false;
    }
    
    if let Ok(data) = std::fs::read_to_string(blocklist_path) {
        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&data) {
            if let Some(blocked) = json.get("blocked").and_then(|b| b.as_object()) {
                let token_lower = token.to_lowercase();
                if let Some(entry) = blocked.get(&token_lower) {
                    // Check expiry
                    if let Some(expires_at) = entry.get("expires_at").and_then(|e| e.as_i64()) {
                        let now = std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .unwrap()
                            .as_secs() as i64;
                        if expires_at > now {
                            let reason = entry.get("reason").and_then(|r| r.as_str()).unwrap_or("unknown");
                            info!(token = %&token_lower[..12.min(token_lower.len())], reason, "🚫 Token in blocklist");
                            return true;
                        }
                    }
                }
            }
        }
    }
    false
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🧠 CREATOR REPUTATION TRACKER
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct CreatorStats {
    tokens_created: u32,
    successful_tokens: u32,
    rugged_tokens: u32,
}

fn load_creator_db() -> HashMap<String, CreatorStats> {
    if let Ok(data) = std::fs::read_to_string("creators.json") {
        serde_json::from_str(&data).unwrap_or_default()
    } else {
        HashMap::new()
    }
}

fn save_creator_db(db: &HashMap<String, CreatorStats>) {
    if let Ok(json) = serde_json::to_string_pretty(db) {
        let _ = std::fs::write("creators.json", json);
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🌐 GECKOTERMINAL API - TRENDING & SOCIAL SIGNALS
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Deserialize)]
struct GeckoResponse {
    data: Option<Vec<GeckoPool>>,
}

#[derive(Debug, Deserialize)]
struct GeckoPool {
    id: Option<String>,
    attributes: Option<GeckoAttributes>,
}

#[derive(Debug, Deserialize)]
struct GeckoAttributes {
    name: Option<String>,
    #[serde(rename = "base_token_price_usd")]
    price_usd: Option<String>,
    #[serde(rename = "volume_usd")]
    volume: Option<GeckoVolume>,
    #[serde(rename = "price_change_percentage")]
    price_change: Option<GeckoPriceChange>,
}

#[derive(Debug, Deserialize)]
struct GeckoVolume {
    h24: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GeckoPriceChange {
    h1: Option<String>,
    h24: Option<String>,
}

/// Check if token is in GeckoTerminal trending pools (social signal!)
async fn is_token_trending(client: &Client, token_address: &str) -> (bool, i8) {
    // Returns: (is_trending, bonus_score)
    let token_lower = token_address.to_lowercase();

    // Check trending pools
    let url = "https://api.geckoterminal.com/api/v2/networks/monad/trending_pools";
    if let Ok(resp) = client.get(url).send().await {
        if let Ok(data) = resp.json::<GeckoResponse>().await {
            if let Some(pools) = data.data {
                for (i, pool) in pools.iter().enumerate() {
                    if let Some(id) = &pool.id {
                        if id.to_lowercase().contains(&token_lower) {
                            let bonus = match i {
                                0 => 25,     // #1 trending = huge bonus
                                1..=2 => 20, // Top 3
                                3..=4 => 15, // Top 5
                                5..=9 => 10, // Top 10
                                _ => 5,      // In trending at all
                            };
                            info!(position = i + 1, bonus, "🔥 TOKEN IS TRENDING!");
                            return (true, bonus);
                        }
                    }
                }
            }
        }
    }

    // Check new pools (recently created = potential)
    let url = "https://api.geckoterminal.com/api/v2/networks/monad/new_pools";
    if let Ok(resp) = client.get(url).send().await {
        if let Ok(data) = resp.json::<GeckoResponse>().await {
            if let Some(pools) = data.data {
                for pool in pools.iter().take(10) {
                    if let Some(id) = &pool.id {
                        if id.to_lowercase().contains(&token_lower) {
                            info!("✨ Token in NEW POOLS - fresh opportunity!");
                            return (true, 8); // Small bonus for being new
                        }
                    }
                }
            }
        }
    }

    (false, 0)
}

/// Get overall market sentiment from top trending pools
async fn get_market_sentiment(client: &Client) -> (String, i8) {
    // Returns: (sentiment_text, score_modifier)
    let url = "https://api.geckoterminal.com/api/v2/networks/monad/trending_pools";

    if let Ok(resp) = client.get(url).send().await {
        if let Ok(data) = resp.json::<GeckoResponse>().await {
            if let Some(pools) = data.data {
                let mut bullish = 0;
                let mut bearish = 0;

                for pool in pools.iter().take(10) {
                    if let Some(attr) = &pool.attributes {
                        if let Some(pc) = &attr.price_change {
                            if let Some(h1) = &pc.h1 {
                                if let Ok(change) = h1.parse::<f64>() {
                                    if change > 5.0 {
                                        bullish += 1;
                                    } else if change < -5.0 {
                                        bearish += 1;
                                    }
                                }
                            }
                        }
                    }
                }

                if bullish > bearish * 2 {
                    return ("🟢 BULLISH MARKET".to_string(), 10);
                } else if bearish > bullish * 2 {
                    return ("🔴 BEARISH MARKET".to_string(), -15);
                } else {
                    return ("🟡 NEUTRAL".to_string(), 0);
                }
            }
        }
    }

    ("⚪ UNKNOWN".to_string(), 0)
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🔍 DEXSCREENER API
// ═══════════════════════════════════════════════════════════════════════════════

async fn get_token_info(client: &Client, token_address: &str) -> Option<DexPair> {
    let url = format!(
        "https://api.dexscreener.com/latest/dex/tokens/{}",
        token_address
    );
    match client.get(&url).send().await {
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
        Err(e) => warn!(?e, "❌ DexScreener Error"),
    }
    None
}

/// Fetch on-chain quote from NAD.FUN Lens (isBuy=true)
async fn fetch_lens_quote(
    client: &Client,
    rpc_url: &str,
    token: Address,
    amount_in: U256,
) -> Option<U256> {
    let call = getAmountOutCall {
        token,
        amountIn: amount_in,
        isBuy: true,
    };
    let calldata = hex::encode(call.abi_encode());

    let body = json!({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{
            "to": NADFUN_LENS,
            "data": format!("0x{}", calldata)
        }, "latest"],
        "id": 1
    });

    if let Ok(resp) = client.post(rpc_url).json(&body).send().await {
        if let Ok(val) = resp.json::<serde_json::Value>().await {
            if let Some(result) = val.get("result").and_then(|r| r.as_str()) {
                if result.len() >= 130 {
                    let bytes = hex::decode(&result[2..]).unwrap_or_default();
                    if bytes.len() >= 64 {
                        let amount_out = U256::from_be_slice(&bytes[32..64]);
                        return Some(amount_out);
                    }
                }
            }
        }
    }
    None
}

async fn analyze_token_quality(client: &Client, token_address: &str) -> (u8, f64, f64, String) {
    // Returns: (score, liquidity_usd, price_change_1h, reason_string)

    // 1. Check social signals FIRST (trending = big bonus!)
    let (is_trending, trending_bonus) = is_token_trending(client, token_address).await;

    // 2. Check market sentiment
    let (market_sentiment, market_modifier) = get_market_sentiment(client).await;

    if let Some(pair) = get_token_info(client, token_address).await {
        let liquidity = pair.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
        let volume_24h = pair.volume.as_ref().and_then(|v| v.h24).unwrap_or(0.0);
        let price_change_1h = pair.price_change.as_ref().and_then(|p| p.h1).unwrap_or(0.0);
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

        let mut score = 50u8;
        let mut reasons = Vec::new();

        // Liquidity scoring
        if liquidity > 10000.0 {
            score += 15;
            reasons.push(format!("🌊 Liq ${:.0}k", liquidity / 1000.0));
        } else if liquidity > 5000.0 {
            score += 10;
            reasons.push(format!("💧 Liq ${:.0}k", liquidity / 1000.0));
        } else if liquidity > 1000.0 {
            score += 5;
            reasons.push(format!("💦 Liq ${:.0}", liquidity));
        } else if liquidity > 0.0 {
            score = score.saturating_sub(10);
            reasons.push("⚠️ Low liq".to_string());
        }

        // Volume scoring
        if volume_24h > 50000.0 {
            score += 15;
            reasons.push(format!("📈 Vol ${:.0}k", volume_24h / 1000.0));
        } else if volume_24h > 10000.0 {
            score += 8;
            reasons.push(format!("📊 Vol ${:.0}k", volume_24h / 1000.0));
        }

        // Buy/sell ratio
        let ratio = buys as f64 / sells.max(1) as f64;
        if ratio > 3.0 {
            score += 15;
            reasons.push(format!("🔥 {:.1}x buyers", ratio));
        } else if ratio > 1.5 {
            score += 8;
            reasons.push(format!("📗 {:.1}x buyers", ratio));
        } else if ratio < 0.5 {
            score = score.saturating_sub(15);
            reasons.push("📕 Heavy selling".to_string());
        }

        // Price change penalty (already pumped)
        if price_change_1h > 100.0 {
            score = score.saturating_sub(20);
            reasons.push(format!("🚨 +{:.0}% pumped!", price_change_1h));
        } else if price_change_1h > 50.0 {
            score = score.saturating_sub(10);
            reasons.push(format!("⚠️ +{:.0}% pump", price_change_1h));
        } else if price_change_1h < -30.0 {
            score = score.saturating_sub(15);
            reasons.push(format!("🔻 {:.0}% dump", price_change_1h));
        }

        // 🌐 SOCIAL SIGNALS - trending bonus!
        if is_trending {
            score = score.saturating_add(trending_bonus as u8);
            reasons.push(format!("🔥 TRENDING +{}", trending_bonus));
        }

        // 📊 Market sentiment modifier
        if market_modifier != 0 {
            if market_modifier > 0 {
                score = score.saturating_add(market_modifier as u8);
            } else {
                score = score.saturating_sub((-market_modifier) as u8);
            }
            reasons.push(market_sentiment.clone());
        }

        return (
            score.min(100),
            liquidity,
            price_change_1h,
            reasons.join(" | "),
        );
    }
    (45, 0.0, 0.0, "❓ No DEX data yet".to_string())
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📋 HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

fn calculate_whale_amount(score: u8, min_mon: f64, max_mon: f64) -> f64 {
    if score >= 85 {
        max_mon
    } else if score >= 75 {
        min_mon + (max_mon - min_mon) * 0.6
    } else if score >= 65 {
        min_mon + (max_mon - min_mon) * 0.3
    } else if score >= 55 {
        min_mon
    } else if score >= 40 {
        min_mon * 0.5
    }
    // Shrimp Mode
    else {
        0.0
    }
}

fn decode_create_token_params(input: &[u8]) -> Option<(String, String)> {
    if input.len() < 100 {
        return None;
    }
    let mut found_strings = Vec::new();
    let mut current_string = String::new();
    for &byte in input.iter().skip(4) {
        if byte >= 32 && byte <= 126 {
            current_string.push(byte as char);
        } else {
            if current_string.len() >= 2 && current_string.len() <= 30 {
                if current_string
                    .chars()
                    .all(|c| c.is_alphanumeric() || c == ' ' || c == '-' || c == '_')
                {
                    found_strings.push(current_string.clone());
                }
            }
            current_string.clear();
        }
    }
    if current_string.len() >= 2 {
        found_strings.push(current_string);
    }

    if found_strings.len() >= 2 {
        Some((found_strings[0].clone(), found_strings[1].clone()))
    } else if found_strings.len() == 1 {
        Some((found_strings[0].clone(), found_strings[0].clone()))
    } else {
        None
    }
}

fn passes_blacklist(name: &str, symbol: &str) -> bool {
    let blacklist = ["test", "scam", "rug", "honeypot", "fake", "airdrop", "free"];
    let name_lower = name.to_lowercase();
    let symbol_lower = symbol.to_lowercase();
    !blacklist
        .iter()
        .any(|word| name_lower.contains(word) || symbol_lower.contains(word))
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 MAIN
// ═══════════════════════════════════════════════════════════════════════════════

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();

    // Initialize tracing
    tracing_subscriber::registry()
        .with(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .with(
            tracing_subscriber::fmt::layer()
                .with_target(true)
                .with_timer(tracing_subscriber::fmt::time::ChronoLocal::rfc_3339())
                .with_ansi(true),
        )
        .init();

    info!("╔═══════════════════════════════════════════════════════════════╗");
    info!("║  🔥 GOD MODE v3: WEBSOCKETS + CREATOR ANALYSIS 🔥            ║");
    info!("╚═══════════════════════════════════════════════════════════════╝");

    let ws_url_str = env::var("MONAD_WS_URL").expect("Brak MONAD_WS_URL");
    let private_key = env::var("PRIVATE_KEY").expect("Brak PRIVATE_KEY");

    let ws_connect = WsConnect::new(ws_url_str);
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer.clone());
    let bot_address = signer.address();

    // Config
    let whale_min = env::var("WHALE_MIN_AMOUNT_MON")
        .unwrap_or("5.0".to_string())
        .parse::<f64>()
        .unwrap();
    let whale_max = env::var("WHALE_MAX_AMOUNT_MON")
        .unwrap_or("50.0".to_string())
        .parse::<f64>()
        .unwrap();
    let min_score = env::var("MIN_BUY_SCORE")
        .unwrap_or("40".to_string())
        .parse::<u8>()
        .unwrap();
    let min_liquidity = env::var("SNIPER_MIN_LIQ_USD")
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(DEFAULT_MIN_LIQ_USD);
    let liq_pct = env::var("SNIPER_LIQ_PCT")
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(DEFAULT_LIQ_PCT);
    let max_price_pump = env::var("MAX_PRICE_PUMP_1H")
        .unwrap_or("100".to_string())
        .parse::<f64>()
        .unwrap();
    let wait_for_dex_sec = env::var("WAIT_FOR_DEX_SEC")
        .unwrap_or("10".to_string())
        .parse::<u64>()
        .unwrap();
    let router_str = env::var("ROUTER_ADDRESS")
        .unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let router_address = Address::from_str(&router_str)?;
    let slippage_bps = env::var("SNIPER_SLIPPAGE_BPS")
        .ok()
        .and_then(|v| v.parse::<u128>().ok())
        .unwrap_or(DEFAULT_SLIPPAGE_BPS);
    let bundle_min_count = env::var("SNIPER_BUNDLE_MIN_COUNT")
        .ok()
        .and_then(|v| v.parse::<u32>().ok())
        .unwrap_or(DEFAULT_BUNDLE_MIN_COUNT);
    let max_gas_gwei = env::var("SNIPER_MAX_GAS_GWEI")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(DEFAULT_MAX_GAS_GWEI);
    let rpc_http_url = env::var("MONAD_RPC_URL").expect("Brak MONAD_RPC_URL");
    let dev_wallets: HashSet<Address> = env::var("DEV_WALLETS")
        .unwrap_or_default()
        .split(',')
        .filter_map(|s| Address::from_str(s.trim()).ok())
        .collect();

    info!(wallet = %bot_address, whale_min, whale_max, min_score, min_liquidity, liq_pct, max_price_pump, wait_for_dex_sec, slippage_bps, bundle_min_count, max_gas_gwei, dev_wallets = dev_wallets.len(), "Configuration loaded");
    info!("🔌 Connecting to WebSocket...");

    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_ws(ws_connect)
        .await?;

    info!("✅ WebSocket Connected!");

    let http_client = Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()?;
    let mut processed_txs = HashSet::new();
    let mut creator_db = load_creator_db();

    // Stats
    let mut tokens_seen = 0u32;
    let mut tokens_bought = 0u32;
    let mut tokens_skipped = 0u32;

    // Subscribe to new blocks
    let sub = provider.subscribe_blocks().await?;
    let mut stream = sub.into_stream();

    info!("🎧 Listening for new blocks...");
    info!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

    while let Some(header) = stream.next().await {
        let block_number = header.number;

        // Fetch full block to get transactions
        if let Ok(Some(block)) = provider
            .get_block_by_number(block_number.into(), BlockTransactionsKind::Full)
            .await
        {
            // Bundle detection per block
            let mut bundle_stats: HashMap<String, HashSet<Address>> = HashMap::new();

            if let Some(txs) = block.transactions.as_transactions() {
                for tx in txs {
                    let tx_hash = tx.inner.tx_hash();
                    if processed_txs.contains(tx_hash) {
                        continue;
                    }
                    processed_txs.insert(*tx_hash);

                    if let Some(to) = tx.to() {
                        if to == router_address {
                            let input = tx.input();
                            // Check for createTokenAndBuy (selector ba12cd8d)
                            if input.len() >= 4 && hex::encode(&input[0..4]) == "ba12cd8d" {
                                tokens_seen += 1;
                                let creator = tx.from;
                                let creator_str = format!("{:?}", creator);

                                info!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                info!(tokens_seen, block_number, creator = %&creator_str[..12], "🚀 NEW TOKEN detected");

                                if let Some((name, symbol)) = decode_create_token_params(input) {
                                    info!(name = %name, symbol = %symbol, "📝 Token info");

                                    // Dev wallet block
                                    if dev_wallets.contains(&creator) {
                                        warn!(creator = %&creator_str[..12], "🚫 DEV WALLET token - skipping");
                                        tokens_skipped += 1;
                                        continue;
                                    }

                                    // Update creator stats
                                    {
                                        let stats =
                                            creator_db.entry(creator_str.clone()).or_default();
                                        stats.tokens_created += 1;
                                        save_creator_db(&creator_db);
                                    }

                                    // 1. Blacklist check
                                    if !passes_blacklist(&name, &symbol) {
                                        warn!(name = %name, "🚫 BLACKLISTED NAME! Skipping.");
                                        tokens_skipped += 1;
                                        continue;
                                    }

                                    // 2. Creator reputation check
                                    let creator_stats =
                                        creator_db.get(&creator_str).cloned().unwrap_or_default();
                                    if creator_stats.rugged_tokens > 0 {
                                        warn!(
                                            rugged_tokens = creator_stats.rugged_tokens,
                                            "⚠️ KNOWN RUGGER! Skipping."
                                        );
                                        tokens_skipped += 1;
                                        continue;
                                    }

                                    // 3. Wait for DEX listing and receipt
                                    info!(wait_for_dex_sec, "⏳ Waiting for DEX listing...");
                                    tokio::time::sleep(std::time::Duration::from_secs(
                                        wait_for_dex_sec,
                                    ))
                                    .await;

                                    // 4. Get token address from receipt (CurveCreate event)
                                    // NAD.FUN v3 - no tokenId needed, just token address
                                    let mut token_addr = None;

                                    for attempt in 0..20 {
                                        // Try 20 times (10 seconds)
                                        if let Ok(Some(receipt)) =
                                            provider.get_transaction_receipt(*tx_hash).await
                                        {
                                            for log in receipt.inner.logs() {
                                                if let Some(topic0) = log.topics().first() {
                                                    // CurveCreate event - token address in topic2
                                                    if topic0 == &B256::from_str("0xd37e3f4f651fe74251701614dbeac478f5a0d29068e87bbe44e5026d166abca9").unwrap() {
                                                        if let Some(topic2) = log.topics().get(2) {
                                                            token_addr = Some(Address::from_slice(&topic2[12..32]));
                                                            break;
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        if token_addr.is_some() {
                                            debug!(attempt = attempt + 1, "✅ Receipt found");
                                            break;
                                        }
                                        tokio::time::sleep(std::time::Duration::from_millis(500))
                                            .await;
                                    }

                                    if let Some(addr) = token_addr {
                                        let token_addr_str = format!("{:?}", addr);

                                        info!(token = %&token_addr_str[..12], "📍 Token address found");

                                        // Anti-bundle per block: skip if many buyers hit same token
                                        let buyers = bundle_stats
                                            .entry(token_addr_str.clone())
                                            .or_insert_with(HashSet::new);
                                        buyers.insert(tx.from);
                                        if buyers.len() as u32 >= bundle_min_count {
                                            warn!(
                                                token = %&token_addr_str[..12],
                                                count = buyers.len(),
                                                "🚫 SKIP: Bundle cluster detected in block"
                                            );
                                            tokens_skipped += 1;
                                            continue;
                                        }

                                        // Risk block check
                                        if is_risk_blocked(&token_addr_str).await {
                                            warn!(token = %&token_addr_str[..12], "🚫 RISK BLOCKED - skipping buy");
                                            tokens_skipped += 1;
                                            continue;
                                        }

                                        // 5. DexScreener analysis with enhanced data
                                        debug!("🔍 Checking DexScreener...");
                                        let (dex_score, liquidity_usd, price_pump_1h, dex_reason) =
                                            analyze_token_quality(&http_client, &token_addr_str)
                                                .await;

                                        // 5a. Liquidity filter
                                        if liquidity_usd > 0.0 && liquidity_usd < min_liquidity {
                                            warn!(
                                                liquidity_usd,
                                                min_liquidity, "❌ SKIP: Low liquidity"
                                            );
                                            tokens_skipped += 1;
                                            continue;
                                        }

                                        // 5b. Already pumped filter
                                        if price_pump_1h > max_price_pump {
                                            warn!(
                                                price_pump_1h,
                                                max_price_pump, "❌ SKIP: Already pumped"
                                            );
                                            tokens_skipped += 1;
                                            continue;
                                        }

                                        // 5c. Heavy dump filter
                                        if price_pump_1h < -40.0 {
                                            warn!(
                                                price_pump_1h,
                                                "❌ SKIP: Dumping - avoid falling knife"
                                            );
                                            tokens_skipped += 1;
                                            continue;
                                        }

                                        // 6. Calculate final score with bonuses/penalties
                                        let creator_bonus: i16 =
                                            if creator_stats.successful_tokens > 2 {
                                                15
                                            } else if creator_stats.successful_tokens > 0 {
                                                5
                                            } else {
                                                0
                                            };
                                        let newbie_penalty: i16 =
                                            if creator_stats.tokens_created == 0 {
                                                -5
                                            } else {
                                                0
                                            };
                                        let liquidity_bonus: i16 =
                                            if liquidity_usd > 5000.0 { 10 } else { 0 };

                                        let final_score = ((dex_score as i16)
                                            + creator_bonus
                                            + newbie_penalty
                                            + liquidity_bonus)
                                            .max(0)
                                            .min(100)
                                            as u8;

                                        let emoji = if final_score >= 80 {
                                            "🔥🔥🔥"
                                        } else if final_score >= 65 {
                                            "⚡⚡"
                                        } else if final_score >= 50 {
                                            "✨"
                                        } else {
                                            "💩"
                                        };

                                        info!(final_score, dex_reason = %dex_reason, "{} Quality score", emoji);

                                        // 7. Calculate buy amount
                                        let amount_mon = calculate_whale_amount(
                                            final_score,
                                            whale_min,
                                            whale_max,
                                        )
                                        .min(liquidity_usd * liq_pct) // cap by liq
                                        .min(1.0); // hard cap

                                        if amount_mon > 0.0 && final_score >= min_score {
                                            // Gas guard
                                            let gas_price =
                                                provider.get_gas_price().await.unwrap_or_default();
                                            let gas_gwei = gas_price / 1_000_000_000;
                                            if gas_gwei > max_gas_gwei.into() {
                                                warn!(
                                                    gas = gas_gwei,
                                                    max = max_gas_gwei,
                                                    "⛽ Gas too high, skip buy"
                                                );
                                                tokens_skipped += 1;
                                                continue;
                                            }

                                            info!(amount_mon, "🐟 Executing buy");

                                            let amount_wei =
                                                U256::from((amount_mon * 1e18) as u128);
                                            // Quote via Lens for slippage protection
                                            let min_out = if let Some(quote) = fetch_lens_quote(
                                                &http_client,
                                                &rpc_http_url,
                                                addr,
                                                amount_wei,
                                            )
                                            .await
                                            {
                                                quote.saturating_mul(U256::from(
                                                    10_000u128 - slippage_bps,
                                                )) / U256::from(10_000u128)
                                            } else {
                                                warn!("⚠️ No Lens quote, skipping trade");
                                                tokens_skipped += 1;
                                                continue;
                                            };

                                            let deadline = U256::from(
                                                SystemTime::now()
                                                    .duration_since(UNIX_EPOCH)
                                                    .unwrap()
                                                    .as_secs()
                                                    + 120,
                                            );

                                            // ═══════════════════════════════════════════════════════════════
                                            // 🔥 NAD.FUN V3 ABI - buy(BuyParams) using sol! macro
                                            // Correct ABI encoding with tuple struct
                                            // ═══════════════════════════════════════════════════════════════
                                            let buy_params = BuyParams {
                                                amountOutMin: min_out,
                                                token: addr,
                                                to: bot_address,
                                                deadline,
                                            };
                                            let buy_call = buyCall { params: buy_params };
                                            let calldata = buy_call.abi_encode();

                                            debug!(calldata_prefix = %format!("0x{}...", hex::encode(&calldata[..20.min(calldata.len())])), "📝 Transaction calldata");

                                            let tx_req =
                                                alloy::rpc::types::TransactionRequest::default()
                                                    .to(router_address)
                                                    .value(amount_wei)
                                                    .input(calldata.into())
                                                    .gas_limit(500_000) // 500k wystarczy, 8M to przesada
                                                    .max_priority_fee_per_gas(1_000_000_000_000);

                                            match provider.send_transaction(tx_req).await {
                                                Ok(pending) => {
                                                    let tx_hash = pending.tx_hash();
                                                    info!(?tx_hash, "✅ BUY SENT");
                                                    tokens_bought += 1;

                                                    // CRITICAL: Wait for receipt and CHECK STATUS!
                                                    debug!("⏳ Waiting for receipt...");
                                                    match tokio::time::timeout(
                                                        std::time::Duration::from_secs(30),
                                                        pending.get_receipt(),
                                                    )
                                                    .await
                                                    {
                                                        Ok(Ok(receipt)) => {
                                                            if receipt.status() {
                                                                info!(
                                                                    gas_used = receipt.gas_used,
                                                                    "🎉 SUCCESS!"
                                                                );

                                                                // Save position
                                                                let position = serde_json::json!({
                                                                    "token_address": token_addr_str,
                                                                    "token_name": format!("{} ({})", name, symbol),
                                                                    "amount_mon": amount_mon,
                                                                    "entry_price_mon": amount_mon,
                                                                    "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
                                                                    "score": final_score
                                                                });

                                                                let path = "positions.json";
                                                                let mut positions: serde_json::Value = std::fs::read_to_string(path)
                                                                    .ok()
                                                                    .and_then(|s| serde_json::from_str(&s).ok())
                                                                    .unwrap_or(serde_json::json!({}));

                                                                positions[token_addr_str] =
                                                                    position;
                                                                let _ = std::fs::write(
                                                                    path,
                                                                    serde_json::to_string_pretty(
                                                                        &positions,
                                                                    )
                                                                    .unwrap(),
                                                                );
                                                            } else {
                                                                error!(
                                                                    gas_used = receipt.gas_used,
                                                                    "❌ TX FAILED! (wasted MON!)"
                                                                );
                                                                tokens_skipped += 1;
                                                            }
                                                        }
                                                        Ok(Err(e)) => {
                                                            error!(?e, "❌ Receipt error");
                                                            tokens_skipped += 1;
                                                        }
                                                        Err(_) => {
                                                            error!("⏱️ Receipt timeout (30s)");
                                                            tokens_skipped += 1;
                                                        }
                                                    }
                                                }
                                                Err(e) => {
                                                    error!(?e, "❌ TX send error");
                                                    tokens_skipped += 1;
                                                }
                                            }
                                        } else {
                                            warn!(
                                                final_score,
                                                min_score, "💩 Score below minimum. Skipping."
                                            );
                                            tokens_skipped += 1;
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // Cleanup old processed txs
        if processed_txs.len() > 1000 {
            processed_txs.clear();
        }
    }

    Ok(())
}
