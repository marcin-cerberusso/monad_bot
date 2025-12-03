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
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;
use tokio::time::timeout;

use futures::StreamExt;
use redis::AsyncCommands;
use reqwest::Client;
use serde::Deserialize;
use serde_json::json;

// Tracing
use tracing::{debug, error, info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

// ═══════════════════════════════════════════════════════════════════════════════
// 📱 TELEGRAM ALERTS
// ═══════════════════════════════════════════════════════════════════════════════
async fn send_telegram(client: &Client, token: &str, chat_id: &str, message: &str) {
    if token.is_empty() || chat_id.is_empty() {
        return;
    }
    let url = format!("https://api.telegram.org/bot{}/sendMessage", token);
    let _ = client
        .post(&url)
        .form(&[
            ("chat_id", chat_id),
            ("text", message),
            ("parse_mode", "HTML"),
        ])
        .send()
        .await;
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🐳 WHALE FOLLOWER v1 - Follow Smart Money on NAD.FUN
// ═══════════════════════════════════════════════════════════════════════════════
// Instead of buying at CREATE, this bot:
// 1. Monitors all buy() calls on NAD.FUN Router
// 2. When someone buys >X MON worth of a token, bot analyzes it
// 3. If token has good liquidity + metrics, bot follows the whale
//
// This catches tokens AFTER they have proven traction, not at creation
// ═══════════════════════════════════════════════════════════════════════════════

// NAD.FUN v3 BondingCurveRouter - buy function
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

    // sell() function for whale exit detection
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
}

// DexScreener types
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
    h6: Option<f64>,
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

// ═══════════════════════════════════════════════════════════════════════════════
// 🔍 TOKEN ANALYSIS
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

async fn analyze_token(
    client: &Client,
    token_address: &str,
    whale_buy_mon: f64,
) -> (u8, f64, f64, String) {
    // Returns: (score, liquidity_usd, price_change_1h, reason)

    if let Some(pair) = get_token_info(client, token_address).await {
        let liquidity = pair.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
        let volume_24h = pair.volume.as_ref().and_then(|v| v.h24).unwrap_or(0.0);
        let price_change_1h = pair.price_change.as_ref().and_then(|p| p.h1).unwrap_or(0.0);
        let price_change_6h = pair.price_change.as_ref().and_then(|p| p.h6).unwrap_or(0.0);
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

        // Liquidity - MUST have real liquidity
        if liquidity > 5000.0 {
            score += 20;
            reasons.push(format!("🌊 ${:.0}k liq", liquidity / 1000.0));
        } else if liquidity > 2000.0 {
            score += 10;
            reasons.push(format!("💧 ${:.0}k liq", liquidity / 1000.0));
        } else if liquidity < 500.0 {
            return (
                30,
                liquidity,
                price_change_1h,
                "❌ Too low liquidity".to_string(),
            );
        }

        // Volume shows real activity
        if volume_24h > 20000.0 {
            score += 15;
            reasons.push(format!("📈 ${:.0}k vol", volume_24h / 1000.0));
        } else if volume_24h > 5000.0 {
            score += 8;
        }

        // Buy/sell ratio - buyers winning?
        let ratio = buys as f64 / sells.max(1) as f64;
        if ratio > 2.0 {
            score += 15;
            reasons.push(format!("🔥 {:.1}x more buyers", ratio));
        } else if ratio > 1.2 {
            score += 5;
        } else if ratio < 0.5 {
            score = score.saturating_sub(20);
            reasons.push("📕 Heavy selling!".to_string());
        }

        // Price momentum - we want early, not FOMO tops
        if price_change_1h > 200.0 {
            // Too late - already pumped
            return (
                25,
                liquidity,
                price_change_1h,
                format!("🚫 Already +{:.0}% pumped", price_change_1h),
            );
        } else if price_change_1h > 50.0 {
            // Hot but risky
            score = score.saturating_sub(10);
            reasons.push(format!("⚠️ +{:.0}% hot", price_change_1h));
        } else if price_change_1h > 10.0 {
            // Good momentum
            score += 10;
            reasons.push(format!("📗 +{:.0}% momentum", price_change_1h));
        } else if price_change_1h < -20.0 {
            // Dumping
            score = score.saturating_sub(15);
            reasons.push(format!("🔻 {:.0}% dump", price_change_1h));
        }

        // 6h trend - longer term health
        if price_change_6h > 100.0 && price_change_1h < 10.0 {
            // Consolidating after pump - risky
            score = score.saturating_sub(10);
            reasons.push("⚠️ Post-pump consolidation".to_string());
        }

        return (
            score.min(100),
            liquidity,
            price_change_1h,
            reasons.join(" | "),
        );
    }

    // No DEX data = very new token
    // If whale buy is HUGE, trust the whale!
    if whale_buy_mon >= 500.0 {
        return (
            70,
            0.0,
            0.0,
            format!("🐳 MEGA WHALE {:.0} MON - trusting!", whale_buy_mon),
        );
    } else if whale_buy_mon >= 100.0 {
        return (
            60,
            0.0,
            0.0,
            format!("🐳 Big whale {:.0} MON - some trust", whale_buy_mon),
        );
    } else if whale_buy_mon >= 50.0 {
        return (
            50,
            0.0,
            0.0,
            format!("🐟 Medium buy {:.0} MON", whale_buy_mon),
        );
    }

    (40, 0.0, 0.0, "❓ No DEX data, small buy".to_string())
}

// Decode buy() params to extract token address
fn decode_buy_params(input: &[u8]) -> Option<Address> {
    // buy((uint256,address,address,uint256))
    // Selector: 0x6df9e92b (4 bytes)
    // Then: amountOutMin (32 bytes), token (32 bytes - address padded), to (32 bytes), deadline (32 bytes)

    if input.len() < 4 + 32 + 32 {
        return None;
    }

    // Token address is at offset 4 + 32 = 36, in last 20 bytes of the 32-byte slot
    let token_bytes = &input[4 + 32 + 12..4 + 32 + 32]; // Skip padding, get 20 bytes
    Some(Address::from_slice(token_bytes))
}

// Decode sell() params to extract token address and amount
// sell((uint256 amountIn, uint256 amountOutMin, address token, address to, uint256 deadline))
// Selector: 0x41503f2d
fn decode_sell_params(input: &[u8]) -> Option<(Address, U256)> {
    // Selector (4) + amountIn (32) + amountOutMin (32) + token (32)
    if input.len() < 4 + 32 + 32 + 32 {
        return None;
    }

    // amountIn at offset 4
    let amount_in = U256::from_be_slice(&input[4..4 + 32]);

    // Token address at offset 4 + 32 + 32 = 68, in last 20 bytes
    let token_bytes = &input[4 + 32 + 32 + 12..4 + 32 + 32 + 32];
    let token = Address::from_slice(token_bytes);

    Some((token, amount_in))
}

/// Track which whales bought which tokens (for exit detection)
#[derive(Debug, Clone)]
struct WhalePosition {
    whale: Address,
    token: Address,
    buy_amount_mon: f64,
    timestamp: u64,
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🔒 HONEYPOT DETECTION - Simulate sell via NAD.FUN Lens + DexScreener fallback
// ═══════════════════════════════════════════════════════════════════════════════

sol! {
    #[derive(Debug)]
    function getAmountsOut(uint256 amountIn, address[] calldata path) external view returns (uint256[] memory amounts);

    #[derive(Debug)]
    function balanceOf(address account) external view returns (uint256);

    #[derive(Debug)]
    function allowance(address owner, address spender) external view returns (uint256);

    // NAD.FUN Lens - get real price quote
    #[derive(Debug)]
    function getAmountOut(address token, uint256 amountIn, bool isBuy) external view returns (address router, uint256 amountOut);
}

// NAD.FUN Lens contract for price queries
const NADFUN_LENS: &str = "0x7e78A8DE94f21804F7a17F4E8BF9EC2c872187ea";

// ═══════════════════════════════════════════════════════════════════════════════
// 🔧 HONEYPOT CONFIG (from ENV)
// ═══════════════════════════════════════════════════════════════════════════════
#[derive(Clone, Debug)]
struct HoneypotConfig {
    min_price_mon: f64,     // HONEYPOT_MIN_PRICE_MON (default 0.0001)
    min_liquidity_usd: f64, // HONEYPOT_MIN_LIQ_USD (default 100.0)
    test_amount: f64,       // HONEYPOT_TEST_AMOUNT (default 1.0 = 1e18 tokens)
    api_timeout_secs: u64,  // HONEYPOT_API_TIMEOUT (default 3)
    cache_ttl_secs: u64,    // HONEYPOT_CACHE_TTL (default 120 = 2 min)
    cache_max_size: usize,  // HONEYPOT_CACHE_SIZE (default 100)
}

impl Default for HoneypotConfig {
    fn default() -> Self {
        Self {
            min_price_mon: env::var("HONEYPOT_MIN_PRICE_MON")
                .unwrap_or("0.0001".to_string())
                .parse()
                .unwrap_or(0.0001),
            min_liquidity_usd: env::var("HONEYPOT_MIN_LIQ_USD")
                .unwrap_or("100.0".to_string())
                .parse()
                .unwrap_or(100.0),
            test_amount: env::var("HONEYPOT_TEST_AMOUNT")
                .unwrap_or("1.0".to_string())
                .parse()
                .unwrap_or(1.0),
            api_timeout_secs: env::var("HONEYPOT_API_TIMEOUT")
                .unwrap_or("3".to_string())
                .parse()
                .unwrap_or(3),
            cache_ttl_secs: env::var("HONEYPOT_CACHE_TTL")
                .unwrap_or("120".to_string())
                .parse()
                .unwrap_or(120),
            cache_max_size: env::var("HONEYPOT_CACHE_SIZE")
                .unwrap_or("100".to_string())
                .parse()
                .unwrap_or(100),
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🗄️ LRU CACHE for honeypot results
// ═══════════════════════════════════════════════════════════════════════════════
#[derive(Clone, Debug)]
struct CachedHoneypotResult {
    is_safe: bool,
    sell_tax: f64,
    reason: String,
    cached_at: Instant,
}

struct HoneypotCache {
    cache: HashMap<String, CachedHoneypotResult>,
    ttl: Duration,
    max_size: usize,
}

impl HoneypotCache {
    fn new(ttl_secs: u64, max_size: usize) -> Self {
        Self {
            cache: HashMap::new(),
            ttl: Duration::from_secs(ttl_secs),
            max_size,
        }
    }

    fn get(&self, token: &str) -> Option<(bool, f64, String)> {
        if let Some(cached) = self.cache.get(token) {
            if cached.cached_at.elapsed() < self.ttl {
                debug!(
                    "📦 Cache HIT for {} (age: {:?})",
                    &token[..10],
                    cached.cached_at.elapsed()
                );
                return Some((cached.is_safe, cached.sell_tax, cached.reason.clone()));
            }
        }
        None
    }

    fn set(&mut self, token: String, is_safe: bool, sell_tax: f64, reason: String) {
        // Evict oldest if at capacity
        if self.cache.len() >= self.max_size {
            if let Some(oldest_key) = self
                .cache
                .iter()
                .min_by_key(|(_, v)| v.cached_at)
                .map(|(k, _)| k.clone())
            {
                self.cache.remove(&oldest_key);
            }
        }
        self.cache.insert(
            token,
            CachedHoneypotResult {
                is_safe,
                sell_tax,
                reason,
                cached_at: Instant::now(),
            },
        );
    }
}

/// Check if token is blocked by risk agent (low liquidity, honeypot, etc.)
async fn is_token_blocked(token: &str) -> bool {
    let redis_url = match std::env::var("DRAGONFLY_URL") {
        Ok(url) => url,
        Err(_) => return false,
    };

    if let Ok(client) = redis::Client::open(redis_url) {
        if let Ok(mut conn) = client.get_multiplexed_async_connection().await {
            let key = format!("risk:blocked:{}", token);
            let result: Result<Option<String>, _> =
                redis::cmd("GET").arg(&key).query_async(&mut conn).await;
            return result.ok().flatten().is_some();
        }
    }
    false
}

/// Block a token in risk agent (e.g., after honeypot detection)
async fn block_token(token: &str, reason: &str, ttl_secs: u64) {
    let redis_url = match std::env::var("DRAGONFLY_URL") {
        Ok(url) => url,
        Err(_) => return,
    };

    let token_owned = token.to_string();
    let reason_owned = reason.to_string();

    let _ = tokio::spawn(async move {
        if let Ok(client) = redis::Client::open(redis_url) {
            if let Ok(mut conn) = client.get_multiplexed_async_connection().await {
                let key = format!("risk:blocked:{}", token_owned);
                let _ = redis::cmd("SETEX")
                    .arg(&key)
                    .arg(ttl_secs)
                    .arg(&reason_owned)
                    .query_async::<()>(&mut conn)
                    .await;
            }
        }
    });
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🎯 BUNDLE CLUSTER DETECTION
// Detects coordinated buying (scam bundlers like Vortex)
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Clone, Debug)]
struct BundleConfig {
    enabled: bool,                 // BUNDLE_DETECTION_ENABLED (default true)
    max_same_block_buys: usize,    // Max buys in same block before flagging (default 5)
    max_holder_concentration: f64, // Max % held by top holders (default 80%)
    min_unique_buyers: usize,      // Min unique buyers before trusting (default 3)
    check_timeout_secs: u64,       // Timeout for RPC calls (default 5)
}

impl Default for BundleConfig {
    fn default() -> Self {
        Self {
            enabled: env::var("BUNDLE_DETECTION_ENABLED")
                .unwrap_or("true".to_string())
                .parse()
                .unwrap_or(true),
            max_same_block_buys: env::var("BUNDLE_MAX_SAME_BLOCK")
                .unwrap_or("5".to_string())
                .parse()
                .unwrap_or(5),
            max_holder_concentration: env::var("BUNDLE_MAX_CONCENTRATION")
                .unwrap_or("80.0".to_string())
                .parse()
                .unwrap_or(80.0),
            min_unique_buyers: env::var("BUNDLE_MIN_BUYERS")
                .unwrap_or("3".to_string())
                .parse()
                .unwrap_or(3),
            check_timeout_secs: env::var("BUNDLE_CHECK_TIMEOUT")
                .unwrap_or("5".to_string())
                .parse()
                .unwrap_or(5),
        }
    }
}

/// Cache for bundle check results (avoid repeated checks)
struct BundleCache {
    cache: HashMap<String, (bool, String, Instant)>, // token -> (is_bundled, reason, timestamp)
    ttl: Duration,
    max_size: usize,
}

impl BundleCache {
    fn new(ttl_secs: u64, max_size: usize) -> Self {
        Self {
            cache: HashMap::new(),
            ttl: Duration::from_secs(ttl_secs),
            max_size,
        }
    }

    fn get(&self, token: &str) -> Option<(bool, String)> {
        if let Some((is_bundled, reason, cached_at)) = self.cache.get(token) {
            if cached_at.elapsed() < self.ttl {
                return Some((*is_bundled, reason.clone()));
            }
        }
        None
    }

    fn set(&mut self, token: String, is_bundled: bool, reason: String) {
        if self.cache.len() >= self.max_size {
            // Evict oldest
            if let Some(oldest) = self
                .cache
                .iter()
                .min_by_key(|(_, (_, _, t))| *t)
                .map(|(k, _)| k.clone())
            {
                self.cache.remove(&oldest);
            }
        }
        self.cache
            .insert(token, (is_bundled, reason, Instant::now()));
    }
}

/// Check if token has bundle cluster (coordinated buying)
/// Returns: (is_bundled, reason)
async fn check_bundle_cluster(
    http_client: &Client,
    rpc_url: &str,
    token_address: Address,
    router_address: Address,
    config: &BundleConfig,
    cache: &Arc<Mutex<BundleCache>>,
) -> (bool, String) {
    if !config.enabled {
        return (false, "Bundle detection disabled".to_string());
    }

    let token_str = format!("{:?}", token_address);

    // Check cache first
    {
        let cache_guard = cache.lock().await;
        if let Some((is_bundled, reason)) = cache_guard.get(&token_str) {
            debug!(
                "📦 Bundle cache HIT for {}: bundled={}",
                &token_str[..10],
                is_bundled
            );
            return (is_bundled, reason);
        }
    }

    // Use simplified bundle detection based on same-block analysis
    // We already track buyers in current block in the main loop
    let result = timeout(
        Duration::from_secs(config.check_timeout_secs),
        check_bundle_via_api(http_client, &token_str, config),
    )
    .await;

    let (is_bundled, reason) = match result {
        Ok(res) => res,
        Err(_) => {
            warn!("⏰ Bundle check timeout - allowing token");
            (false, "Timeout - skipping check".to_string())
        }
    };

    // Cache result
    {
        let mut cache_guard = cache.lock().await;
        cache_guard.set(token_str, is_bundled, reason.clone());
    }

    (is_bundled, reason)
}

/// Check bundle pattern via DexScreener API (holder distribution)
/// Returns: (is_bundled, reason)
async fn check_bundle_via_api(
    http_client: &Client,
    token: &str,
    config: &BundleConfig,
) -> (bool, String) {
    // Use DexScreener to check liquidity and trading pattern
    let url = format!("https://api.dexscreener.com/latest/dex/tokens/{}", token);

    let resp = match http_client.get(&url).send().await {
        Ok(r) => r,
        Err(_) => return (false, "API error - skipping check".to_string()),
    };

    let json: serde_json::Value = match resp.json().await {
        Ok(j) => j,
        Err(_) => return (false, "JSON parse error - skipping check".to_string()),
    };

    // Get pair data
    let pairs = match json.get("pairs").and_then(|p| p.as_array()) {
        Some(p) if !p.is_empty() => p,
        _ => return (false, "No pairs found - new token".to_string()),
    };

    let first_pair = &pairs[0];

    // Check transaction counts
    let txns = first_pair.get("txns").and_then(|t| t.get("h1"));
    if let Some(txn_data) = txns {
        let buys = txn_data.get("buys").and_then(|b| b.as_u64()).unwrap_or(0);
        let sells = txn_data.get("sells").and_then(|s| s.as_u64()).unwrap_or(0);

        // Very few buys = suspicious (bundled launch)
        if buys < config.min_unique_buyers as u64 && buys > 0 {
            let reason = format!(
                "🚨 BUNDLE: Only {} buys in 1h (min: {})",
                buys, config.min_unique_buyers
            );
            warn!("{}", reason);
            return (true, reason);
        }

        // No sells at all but many buys = could be honeypot bundler
        if sells == 0 && buys > 10 {
            let reason = format!("🚨 SUSPICIOUS: {} buys, 0 sells (honeypot pattern)", buys);
            warn!("{}", reason);
            return (true, reason);
        }
    }

    // Check price change (massive pump = bundled)
    if let Some(price_change) = first_pair
        .get("priceChange")
        .and_then(|p| p.get("h1"))
        .and_then(|h| h.as_f64())
    {
        if price_change > 500.0 {
            let reason = format!(
                "🚨 BUNDLE: Price +{:.0}% in 1h (likely coordinated)",
                price_change
            );
            warn!("{}", reason);
            return (true, reason);
        }
    }

    // All checks passed
    (false, format!("✅ Clean trading pattern"))
}

/// Publish honeypot check metrics to Redis/Dragonfly
async fn publish_honeypot_metrics(
    http_client: &Client,
    token: &str,
    is_safe: bool,
    sell_tax: f64,
    amount_out_mon: f64,
    router: &str,
    source: &str,
) {
    let redis_url = match std::env::var("DRAGONFLY_URL") {
        Ok(url) => url,
        Err(_) => return,
    };

    let metrics = serde_json::json!({
        "type": "honeypot_check",
        "token": token,
        "is_safe": is_safe,
        "sell_tax": sell_tax,
        "amount_out_mon": amount_out_mon,
        "router": router,
        "source": source,
        "timestamp": chrono::Utc::now().to_rfc3339()
    });

    // Fire and forget - don't block on metrics
    let _ = tokio::spawn(async move {
        if let Ok(client) = redis::Client::open(redis_url) {
            if let Ok(mut conn) = client.get_multiplexed_async_connection().await {
                let _ = redis::cmd("PUBLISH")
                    .arg("agent_swarm:metrics")
                    .arg(metrics.to_string())
                    .query_async::<()>(&mut conn)
                    .await;
            }
        }
    });
}

/// Set temporary risk block for a token (Redis key: risk:blocked:{token})
async fn set_risk_block(token: String) {
    let redis_url = match std::env::var("DRAGONFLY_URL") {
        Ok(url) => url,
        Err(_) => return,
    };

    let ttl_secs = std::env::var("DEV_BLOCK_TTL_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(1800); // default 30 min

    let _ = tokio::spawn(async move {
        if let Ok(client) = redis::Client::open(redis_url) {
            if let Ok(mut conn) = client.get_multiplexed_async_connection().await {
                let _ = conn
                    .set_ex::<_, _, ()>(format!("risk:blocked:{}", token), "1", ttl_secs)
                    .await;
            }
        }
    });
}

/// Publish honeypot ERROR to dedicated error channel for dashboard
async fn publish_honeypot_error(token: &str, error_type: &str, details: &str) {
    let redis_url = match std::env::var("DRAGONFLY_URL") {
        Ok(url) => url,
        Err(_) => return,
    };

    let error_data = serde_json::json!({
        "type": "honeypot_error",
        "token": token,
        "error_type": error_type,
        "details": details,
        "timestamp": chrono::Utc::now().to_rfc3339()
    });

    let _ = tokio::spawn(async move {
        if let Ok(client) = redis::Client::open(redis_url) {
            if let Ok(mut conn) = client.get_multiplexed_async_connection().await {
                let _ = redis::cmd("PUBLISH")
                    .arg("agent_swarm:errors")
                    .arg(error_data.to_string())
                    .query_async::<()>(&mut conn)
                    .await;
            }
        }
    });
}

/// Fallback: Check token liquidity via DexScreener API (with timeout)
async fn check_dexscreener_liquidity_with_timeout(
    http_client: &Client,
    token: &str,
    timeout_secs: u64,
) -> Option<f64> {
    let url = format!("https://api.dexscreener.com/latest/dex/tokens/{}", token);

    let fut = async {
        match http_client.get(&url).send().await {
            Ok(resp) => {
                if let Ok(json) = resp.json::<serde_json::Value>().await {
                    if let Some(pairs) = json.get("pairs").and_then(|p| p.as_array()) {
                        if let Some(first_pair) = pairs.first() {
                            if let Some(liquidity) = first_pair
                                .get("liquidity")
                                .and_then(|l| l.get("usd"))
                                .and_then(|u| u.as_f64())
                            {
                                debug!("📊 DexScreener liquidity: ${:.2}", liquidity);
                                return Some(liquidity);
                            }
                        }
                    }
                }
                None
            }
            Err(_) => None,
        }
    };

    match timeout(Duration::from_secs(timeout_secs), fut).await {
        Ok(result) => result,
        Err(_) => {
            warn!("⏰ DexScreener liquidity timeout after {}s", timeout_secs);
            None
        }
    }
}

/// Fallback: estimate MON output using DexScreener priceNative (with timeout + retry)
async fn fetch_dexscreener_price_mon_with_timeout(
    http_client: &Client,
    token: &str,
    token_amount: f64,
    timeout_secs: u64,
) -> Option<f64> {
    let url = format!("https://api.dexscreener.com/latest/dex/tokens/{}", token);

    // Try up to 2 times with backoff
    for attempt in 0..2 {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }

        let fut = async {
            if let Ok(resp) = http_client.get(&url).send().await {
                if let Ok(val) = resp.json::<serde_json::Value>().await {
                    if let Some(price_native) = val
                        .get("pairs")
                        .and_then(|p| p.get(0))
                        .and_then(|p| p.get("priceNative"))
                        .and_then(|p| p.as_str())
                        .and_then(|s| s.parse::<f64>().ok())
                    {
                        return Some(price_native * token_amount);
                    }
                }
            }
            None
        };

        match timeout(Duration::from_secs(timeout_secs), fut).await {
            Ok(Some(result)) => return Some(result),
            Ok(None) => continue,
            Err(_) => {
                warn!("⏰ DexScreener price timeout (attempt {})", attempt + 1);
                continue;
            }
        }
    }
    None
}

/// Check if token is a honeypot by simulating sell via NAD.FUN Lens
/// Returns: (is_safe, sell_tax_percent, reason)
/// Now with: timeout, retry, ENV config, error channel publishing
async fn check_honeypot_with_cache(
    http_client: &Client,
    rpc_url: &str,
    token_address: Address,
    config: &HoneypotConfig,
    cache: &Arc<Mutex<HoneypotCache>>,
) -> (bool, f64, String) {
    let token_str = format!("{:?}", token_address);

    // 1. Check cache first
    {
        let cache_guard = cache.lock().await;
        if let Some(cached) = cache_guard.get(&token_str) {
            return cached;
        }
    }

    // Log config being used
    debug!(
        "🔧 Honeypot check: min_price={} MON, min_liq=${}, test_amt={}, timeout={}s",
        config.min_price_mon, config.min_liquidity_usd, config.test_amount, config.api_timeout_secs
    );

    let test_amount = U256::from((config.test_amount * 1e18) as u128);

    // Use NAD.FUN Lens getAmountOut(token, amount, isBuy=false) for sell simulation
    let get_amount_call = getAmountOutCall {
        token: token_address,
        amountIn: test_amount,
        isBuy: false, // Simulating SELL
    };
    let calldata = hex::encode(get_amount_call.abi_encode());

    // Make eth_call via HTTP to NAD.FUN Lens with timeout
    let request_body = serde_json::json!({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{
            "to": NADFUN_LENS,
            "data": format!("0x{}", calldata)
        }, "latest"],
        "id": 1
    });

    let lens_timeout = Duration::from_secs(config.api_timeout_secs);

    let lens_result = timeout(
        lens_timeout,
        http_client.post(rpc_url).json(&request_body).send(),
    )
    .await;

    let result = match lens_result {
        Ok(Ok(resp)) => {
            if let Ok(json) = resp.json::<serde_json::Value>().await {
                parse_lens_response(http_client, &token_str, &json, test_amount, config).await
            } else {
                publish_honeypot_error(&token_str, "parse_error", "Failed to parse Lens JSON")
                    .await;
                (false, 100.0, "⚠️ Invalid Lens response".to_string())
            }
        }
        Ok(Err(e)) => {
            // HTTP error - try DexScreener fallback
            debug!("📊 Lens HTTP error, trying DexScreener fallback: {}", e);
            dexscreener_fallback(http_client, &token_str, config).await
        }
        Err(_) => {
            // Timeout - try DexScreener fallback
            warn!(
                "⏰ Lens timeout after {}s, trying DexScreener",
                config.api_timeout_secs
            );
            publish_honeypot_error(
                &token_str,
                "lens_timeout",
                &format!("Timeout after {}s", config.api_timeout_secs),
            )
            .await;
            dexscreener_fallback(http_client, &token_str, config).await
        }
    };

    // 2. Cache the result
    {
        let mut cache_guard = cache.lock().await;
        cache_guard.set(token_str, result.0, result.1, result.2.clone());
    }

    result
}

/// Parse Lens response and handle all cases
async fn parse_lens_response(
    http_client: &Client,
    token_str: &str,
    json: &serde_json::Value,
    test_amount: U256,
    config: &HoneypotConfig,
) -> (bool, f64, String) {
    if let Some(result) = json.get("result").and_then(|r| r.as_str()) {
        // Result is empty or too short = try DexScreener fallback
        if result.len() < 130 {
            debug!("📊 Lens returned short result, trying DexScreener fallback");
            return dexscreener_fallback(http_client, token_str, config).await;
        }

        // Decode result - (address router, uint256 amountOut)
        let result_bytes = hex::decode(&result[2..]).unwrap_or_default();

        if result_bytes.len() >= 64 {
            let router_bytes = &result_bytes[12..32];
            let router_addr = Address::from_slice(router_bytes);
            let router_str = format!("{:?}", router_addr);

            let amount_out = U256::from_be_slice(&result_bytes[32..64]);
            let output_val = amount_out.to::<u128>() as f64;
            let amount_out_mon = output_val / 1e18;

            // 🔍 DEBUG LOG: Show router and amount
            info!(
                "📊 Lens: router={} amountOut={:.6} MON token={}",
                &router_str[..router_str.len().min(10)],
                amount_out_mon,
                &token_str[..token_str.len().min(10)]
            );

            if output_val == 0.0 {
                // Fallback to DexScreener price on zero output
                let token_amount = test_amount.to::<u128>() as f64 / 1e18;
                if let Some(est_out_mon) = fetch_dexscreener_price_mon_with_timeout(
                    http_client,
                    token_str,
                    token_amount,
                    config.api_timeout_secs,
                )
                .await
                {
                    publish_honeypot_metrics(
                        http_client,
                        token_str,
                        true,
                        0.0,
                        est_out_mon,
                        "dexscreener",
                        &router_str,
                    )
                    .await;
                    return (
                        true,
                        0.0,
                        format!("✅ DexScreener price fallback: {:.4} MON", est_out_mon),
                    );
                }

                publish_honeypot_error(
                    token_str,
                    "zero_output",
                    "Lens returned zero, DexScreener failed",
                )
                .await;
                publish_honeypot_metrics(
                    http_client,
                    token_str,
                    false,
                    100.0,
                    0.0,
                    &router_str,
                    "lens",
                )
                .await;
                return (false, 100.0, "❌ HONEYPOT: Zero output".to_string());
            }

            // Check if we get at least min_price_mon back
            if amount_out_mon < config.min_price_mon {
                publish_honeypot_error(
                    token_str,
                    "low_value",
                    &format!("Only {:.6} MON < {}", amount_out_mon, config.min_price_mon),
                )
                .await;
                publish_honeypot_metrics(
                    http_client,
                    token_str,
                    false,
                    95.0,
                    amount_out_mon,
                    &router_str,
                    "lens",
                )
                .await;
                return (
                    false,
                    95.0,
                    format!("⚠️ Very low sell value: {:.6} MON", amount_out_mon),
                );
            }

            // Token is sellable on NAD.FUN!
            publish_honeypot_metrics(
                http_client,
                token_str,
                true,
                0.0,
                amount_out_mon,
                &router_str,
                "lens",
            )
            .await;
            return (
                true,
                0.0,
                format!("✅ Sellable for {:.4} MON", amount_out_mon),
            );
        }
    }

    // Check for error
    if let Some(error) = json.get("error") {
        let err_str = format!("{:?}", error);
        if err_str.contains("revert") {
            publish_honeypot_error(token_str, "revert", &err_str[..err_str.len().min(100)]).await;
            publish_honeypot_metrics(http_client, token_str, false, 100.0, 0.0, "none", "lens")
                .await;
            return (false, 100.0, "❌ HONEYPOT: Sell reverts".to_string());
        }
        publish_honeypot_error(token_str, "lens_error", &err_str[..err_str.len().min(100)]).await;
        publish_honeypot_metrics(http_client, token_str, false, 100.0, 0.0, "none", "lens").await;
        return (
            false,
            100.0,
            format!("⚠️ Lens error: {}", &err_str[..err_str.len().min(50)]),
        );
    }

    publish_honeypot_error(token_str, "invalid_response", "Could not parse Lens result").await;
    publish_honeypot_metrics(http_client, token_str, false, 100.0, 0.0, "none", "lens").await;
    (false, 100.0, "⚠️ Invalid response".to_string())
}

/// DexScreener fallback when Lens fails
async fn dexscreener_fallback(
    http_client: &Client,
    token_str: &str,
    config: &HoneypotConfig,
) -> (bool, f64, String) {
    // Try price-based check first
    if let Some(est_out_mon) = fetch_dexscreener_price_mon_with_timeout(
        http_client,
        token_str,
        config.test_amount,
        config.api_timeout_secs,
    )
    .await
    {
        if est_out_mon >= config.min_price_mon {
            publish_honeypot_metrics(
                http_client,
                token_str,
                true,
                0.0,
                est_out_mon,
                "dexscreener",
                "dexscreener",
            )
            .await;
            return (
                true,
                0.0,
                format!("✅ DexScreener price: {:.4} MON", est_out_mon),
            );
        }
    }

    // Secondary: check liquidity signal
    if let Some(liquidity_usd) =
        check_dexscreener_liquidity_with_timeout(http_client, token_str, config.api_timeout_secs)
            .await
    {
        if liquidity_usd >= config.min_liquidity_usd {
            publish_honeypot_metrics(
                http_client,
                token_str,
                true,
                0.0,
                0.0,
                "dexscreener",
                "dexscreener",
            )
            .await;
            return (
                true,
                0.0,
                format!("✅ DexScreener liquidity: ${:.0}", liquidity_usd),
            );
        }
    }

    publish_honeypot_error(
        token_str,
        "no_liquidity",
        "Both Lens and DexScreener failed",
    )
    .await;
    publish_honeypot_metrics(
        http_client,
        token_str,
        false,
        100.0,
        0.0,
        "none",
        "fallback",
    )
    .await;
    (false, 100.0, "❌ No liquidity found".to_string())
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 MAIN
// ═══════════════════════════════════════════════════════════════════════════════

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();

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
    info!("║  🐳 WHALE FOLLOWER v2 - Honeypot Detection + Whale Exit 🐳  ║");
    info!("╚═══════════════════════════════════════════════════════════════╝");

    let ws_url_str = env::var("MONAD_WS_URL").expect("Missing MONAD_WS_URL");
    let http_rpc_url = env::var("MONAD_RPC_URL")
        .unwrap_or("https://monad-mainnet.g.alchemy.com/v2/FPgsxxE5R86qHQ200z04i".to_string());
    let private_key = env::var("PRIVATE_KEY").expect("Missing PRIVATE_KEY");

    let ws_connect = WsConnect::new(ws_url_str);
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer.clone());
    let bot_address = signer.address();

    // Config - SAFER DEFAULTS!
    let min_whale_buy_mon = env::var("MIN_WHALE_BUY_MON")
        .unwrap_or("300.0".to_string())
        .parse::<f64>()
        .unwrap();
    let follow_amount_mon = env::var("FOLLOW_AMOUNT_MON")
        .unwrap_or("10.0".to_string())
        .parse::<f64>()
        .unwrap();
    let bundle_cluster_min_count = env::var("BUNDLE_CLUSTER_MIN_COUNT")
        .unwrap_or("3".to_string())
        .parse::<u32>()
        .unwrap_or(3);
    let dev_wallets: HashSet<Address> = env::var("DEV_WALLETS")
        .unwrap_or_default()
        .split(',')
        .filter_map(|s| Address::from_str(s.trim()).ok())
        .collect();
    let min_score = env::var("MIN_BUY_SCORE")
        .unwrap_or("70".to_string())
        .parse::<u8>()
        .unwrap();
    let min_liquidity = env::var("MIN_LIQUIDITY_USD")
        .unwrap_or("2000".to_string())
        .parse::<f64>()
        .unwrap();
    let max_pump_1h = env::var("MAX_PRICE_PUMP_1H")
        .unwrap_or("50".to_string())
        .parse::<f64>()
        .unwrap();
    let max_open_positions = env::var("MAX_OPEN_POSITIONS")
        .unwrap_or("5".to_string())
        .parse::<usize>()
        .unwrap();
    let min_wallet_balance_mon = env::var("MIN_WALLET_BALANCE_MON")
        .unwrap_or("100.0".to_string())
        .parse::<f64>()
        .unwrap();

    let router_str = env::var("ROUTER_ADDRESS")
        .unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let router_address = Address::from_str(&router_str)?;

    // WMON address for honeypot detection
    let wmon_address = Address::from_str("0x760AfE86e5de5fa0Ee542fc7B7B713e1c5425701")?;

    // NAD.FUN Lens contract for price/honeypot checks
    let lens_router = Address::from_str(NADFUN_LENS)?;

    // 🔧 Honeypot config from ENV
    let honeypot_config = HoneypotConfig::default();
    info!(
        "🍯 Honeypot config: min_price={} MON, min_liq=${}, test_amt={}, timeout={}s, cache_ttl={}s, cache_size={}",
        honeypot_config.min_price_mon,
        honeypot_config.min_liquidity_usd,
        honeypot_config.test_amount,
        honeypot_config.api_timeout_secs,
        honeypot_config.cache_ttl_secs,
        honeypot_config.cache_max_size
    );

    // 🗄️ Honeypot cache
    let honeypot_cache = Arc::new(Mutex::new(HoneypotCache::new(
        honeypot_config.cache_ttl_secs,
        honeypot_config.cache_max_size,
    )));

    // 🎯 Bundle detection config from ENV
    let bundle_config = BundleConfig::default();
    info!(
        "🎯 Bundle detection: enabled={}, max_same_block={}, max_concentration={}%, min_buyers={}, timeout={}s",
        bundle_config.enabled,
        bundle_config.max_same_block_buys,
        bundle_config.max_holder_concentration,
        bundle_config.min_unique_buyers,
        bundle_config.check_timeout_secs
    );

    // 🗄️ Bundle cache (5 min TTL, 50 tokens max)
    let bundle_cache = Arc::new(Mutex::new(BundleCache::new(300, 50)));

    // 📱 Telegram config
    let tg_token = env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
    let tg_chat_id = env::var("TELEGRAM_CHAT_ID").unwrap_or_default();

    // 🌟 TRUSTED WALLETS - get 500+ MON treatment (14 MON position)
    let trusted_wallets: HashSet<Address> = vec![
        "0x97B36E9D28C23fa665C947C222E3FffAfF284023", // Dev Wallet 7131
        "0x8D34e0165DFB2d70a5B5890e100581e0884F2EAb", // Trusted #2
        "0xe58982D5B56c07CDb18A04FC4429E658E6002d85", // Trusted #3
        "0x4a7906ab22fD2C4cC744D1e9b8bEa26a3F6FaDDA", // Trusted #4
        "0x7d9471511a6c027e978adaf02014c3d2f40a0571", // Mega Whale 72k MON
        "0xe826dd0be78361417809520d292009d7c9d303e9", // Whale 20k MON
        "0x3B427018CD3836Bb0D73A859bCcf591Ef8fe148B", // Whale 5.9k MON
    ]
    .into_iter()
    .filter_map(|s| Address::from_str(s).ok())
    .collect();

    // buy() selector: 0x6df9e92b
    let buy_selector = "6df9e92b";
    // sell() selector: 0x41503f2d
    let sell_selector = "41503f2d";

    info!(wallet = %bot_address, min_whale_buy_mon, follow_amount_mon, min_score, min_liquidity, max_open_positions, trusted_count = trusted_wallets.len(), "📋 Config loaded");
    info!(
        "🛡️ Safety: Max {} positions, Min {} MON wallet balance",
        max_open_positions, min_wallet_balance_mon
    );
    info!(
        "🛡️ Anti-bundle: min {} buys per block triggers skip",
        bundle_cluster_min_count
    );
    info!(
        dev_wallets = dev_wallets.len(),
        "🛡️ Dev wallet monitor enabled"
    );
    info!("🔌 Connecting to WebSocket...");

    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_ws(ws_connect)
        .await?;

    info!("✅ WebSocket Connected! Watching for whale buys on NAD.FUN...");

    let http_client = Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()?;
    let mut processed_txs = HashSet::<B256>::new();
    let mut followed_tokens = HashSet::<String>::new(); // Don't buy same token twice

    // Track whale positions for exit detection
    // Key: (whale_address, token_address) -> buy_amount_mon
    let mut whale_positions: HashMap<(String, String), f64> = HashMap::new();

    // Stats
    let mut whales_seen = 0u32;
    let mut tokens_followed = 0u32;
    let mut tokens_skipped = 0u32;

    let sub = provider.subscribe_blocks().await?;
    let mut stream = sub.into_stream();

    info!(
        "🎧 Listening for whale buys (>{} MON)...",
        min_whale_buy_mon
    );
    info!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

    while let Some(header) = stream.next().await {
        let block_number = header.number;

        if let Ok(Some(block)) = provider
            .get_block_by_number(block_number.into(), BlockTransactionsKind::Full)
            .await
        {
            // Reset bundle stats per block
            let mut bundle_stats: HashMap<String, (HashSet<Address>, f64)> = HashMap::new();

            if let Some(txs) = block.transactions.as_transactions() {
                for tx in txs {
                    let tx_hash = tx.inner.tx_hash();
                    if processed_txs.contains(tx_hash) {
                        continue;
                    }
                    processed_txs.insert(*tx_hash);

                    // Only look at NAD.FUN router transactions
                    if let Some(to) = tx.to() {
                        if to != router_address {
                            continue;
                        }

                        let input = tx.input();
                        let value_wei = tx.value();
                        let value_mon = value_wei.to::<u128>() as f64 / 1e18;
                        let buyer = tx.from;

                        // Skip our own transactions
                        if buyer == bot_address {
                            continue;
                        }

                        // ═══════════════════════════════════════════════════════════════
                        // 🚨 WHALE EXIT DETECTION - Track when whales we followed sell
                        // ═══════════════════════════════════════════════════════════════
                        if input.len() >= 4 && hex::encode(&input[0..4]) == sell_selector {
                            if let Some((token_addr, _amount_in)) = decode_sell_params(input) {
                                let seller_str = format!("{:?}", buyer);
                                let token_str = format!("{:?}", token_addr);

                                // Dev wallet selling?
                                if dev_wallets.contains(&buyer) {
                                    warn!(
                                        dev = %&seller_str[..12],
                                        token = %&token_str[..12],
                                        "🚨 DEV SELL detected"
                                    );
                                    // Set risk:blocked:{token} in Redis (best effort)
                                    let _ = tokio::spawn(set_risk_block(token_str.clone()));
                                    // Telegram alert
                                    if !tg_token.is_empty() && !tg_chat_id.is_empty() {
                                        let msg = format!(
                                            "🚨 DEV SELL!\n📍 Token: {}...{}\n👾 Dev: {}...{}\n⚠️ Consider exiting",
                                            &token_str[..8],
                                            &token_str[token_str.len() - 4..],
                                            &seller_str[..8],
                                            &seller_str[seller_str.len() - 4..]
                                        );
                                        let _ = send_telegram(
                                            &http_client,
                                            &tg_token,
                                            &tg_chat_id,
                                            &msg,
                                        )
                                        .await;
                                    }
                                }

                                // Check if this whale previously bought this token (and we followed)
                                let key = (seller_str.clone(), token_str.clone());
                                if let Some(original_buy) = whale_positions.get(&key) {
                                    // The whale we followed is selling!
                                    warn!("🚨 WHALE EXIT! {} selling {} (originally bought {:.0} MON)", 
                                        &seller_str[..12], &token_str[..12], original_buy);

                                    // Send Telegram alert about whale exit
                                    if !tg_token.is_empty() && !tg_chat_id.is_empty() {
                                        let msg = format!(
                                            "🚨 WHALE SELLING!\n📍 Token: {}...{}\n🐳 Whale: {}...{}\n💰 Original buy: {:.0} MON\n⚠️ Consider exiting position!",
                                            &token_str[..8], &token_str[token_str.len()-4..],
                                            &seller_str[..8], &seller_str[seller_str.len()-4..],
                                            original_buy
                                        );
                                        let _ = send_telegram(
                                            &http_client,
                                            &tg_token,
                                            &tg_chat_id,
                                            &msg,
                                        )
                                        .await;
                                    }

                                    // Mark in positions.json that whale exited (position_manager can act on this)
                                    if let Ok(content) = std::fs::read_to_string("positions.json") {
                                        if let Ok(mut positions) =
                                            serde_json::from_str::<serde_json::Value>(&content)
                                        {
                                            if let Some(pos) = positions.get_mut(&token_str) {
                                                pos["whale_exited"] = serde_json::json!(true);
                                                pos["whale_exit_time"] =
                                                    serde_json::json!(SystemTime::now()
                                                        .duration_since(UNIX_EPOCH)
                                                        .unwrap()
                                                        .as_secs());
                                                let _ = std::fs::write(
                                                    "positions.json",
                                                    serde_json::to_string_pretty(&positions)
                                                        .unwrap(),
                                                );
                                                info!("📝 Marked whale exit in positions.json");
                                            }
                                        }
                                    }

                                    // Remove from tracking
                                    whale_positions.remove(&key);
                                }
                            }
                            continue; // Don't process sell as buy
                        }

                        // Check for buy() selector
                        if input.len() >= 4 && hex::encode(&input[0..4]) == buy_selector {
                            // Check if trusted wallet (bypass min whale requirement)
                            let is_trusted_buyer = trusted_wallets.contains(&buyer);

                            // Only care about WHALE buys (or trusted wallets)
                            if value_mon < min_whale_buy_mon && !is_trusted_buyer {
                                continue;
                            }

                            whales_seen += 1;

                            // Decode token address from buy params
                            if let Some(token_addr) = decode_buy_params(input) {
                                let token_str = format!("{:?}", token_addr);

                                // 🛡️ Anti-bundle detection per block
                                let entry = bundle_stats
                                    .entry(token_str.clone())
                                    .or_insert_with(|| (HashSet::new(), 0.0));
                                entry.0.insert(buyer);
                                entry.1 += value_mon;
                                let unique_buys = entry.0.len() as u32;
                                if unique_buys >= bundle_cluster_min_count {
                                    warn!(
                                        token = %&token_str[..12],
                                        unique_buys,
                                        total_mon = entry.1,
                                        "🚫 SKIP: Bundle cluster detected in block"
                                    );
                                    tokens_skipped += 1;
                                    continue;
                                }

                                info!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                if is_trusted_buyer {
                                    info!(wallet = %format!("{:?}", buyer)[..12], amount = %format!("{:.2} MON", value_mon), token = %&token_str[..12], "🌟 TRUSTED WALLET BUY!");
                                } else {
                                    info!(whale = %format!("{:?}", buyer)[..12], amount = %format!("{:.2} MON", value_mon), token = %&token_str[..12], "🐳 WHALE BUY DETECTED!");
                                }

                                // 📱 Telegram alert: Whale/Trusted detected
                                if !tg_token.is_empty() && !tg_chat_id.is_empty() {
                                    let whale_emoji =
                                        if is_trusted_buyer { "🌟" } else { "🐳" };
                                    let buyer_label =
                                        if is_trusted_buyer { "TRUSTED" } else { "WHALE" };
                                    let msg = format!(
                                        "{} {} BUY: {:.1} MON\n📍 Token: {}...{}\n👛 Buyer: {}...{}",
                                        whale_emoji, buyer_label, value_mon,
                                        &token_str[..8], &token_str[token_str.len()-4..],
                                        &format!("{:?}", buyer)[..8], &format!("{:?}", buyer)[format!("{:?}", buyer).len()-4..]
                                    );
                                    let _ =
                                        send_telegram(&http_client, &tg_token, &tg_chat_id, &msg)
                                            .await;
                                }

                                // Skip if we already bought this token
                                if followed_tokens.contains(&token_str) {
                                    debug!("Already followed this token, skipping");
                                    continue;
                                }

                                // 🛡️ SAFETY CHECK 1: Max open positions
                                if followed_tokens.len() >= max_open_positions {
                                    warn!(
                                        current = followed_tokens.len(),
                                        max = max_open_positions,
                                        "❌ SKIP: Max positions reached!"
                                    );
                                    continue;
                                }

                                // 🛡️ SAFETY CHECK 2: Minimum balance
                                let wallet_balance = provider
                                    .get_balance(bot_address)
                                    .await
                                    .unwrap_or(U256::ZERO);
                                let balance_mon = wallet_balance.to::<u128>() as f64 / 1e18;
                                if balance_mon < min_wallet_balance_mon {
                                    warn!(
                                        balance = balance_mon,
                                        min = min_wallet_balance_mon,
                                        "❌ SKIP: Balance too low!"
                                    );
                                    continue;
                                }

                                // Analyze token quality
                                let (mut score, liquidity, price_1h, reason) =
                                    analyze_token(&http_client, &token_str, value_mon).await;

                                // 🌟 Trusted wallet bonus
                                if is_trusted_buyer {
                                    score = score.saturating_add(20); // +20 score bonus
                                }

                                let emoji = if is_trusted_buyer {
                                    "🌟"
                                } else if score >= 75 {
                                    "🔥🔥"
                                } else if score >= 60 {
                                    "⚡"
                                } else {
                                    "💩"
                                };
                                info!(score, liquidity, price_1h = %format!("{:.1}%", price_1h), "{} {}", emoji, reason);

                                // Filter checks
                                if score < min_score {
                                    warn!(score, min_score, "❌ SKIP: Score too low");
                                    tokens_skipped += 1;
                                    continue;
                                }

                                if liquidity > 0.0 && liquidity < min_liquidity {
                                    warn!(liquidity, min_liquidity, "❌ SKIP: Low liquidity");
                                    tokens_skipped += 1;
                                    continue;
                                }

                                if price_1h > max_pump_1h {
                                    warn!(
                                        price_1h,
                                        max_pump_1h, "❌ SKIP: Already pumped too much"
                                    );
                                    tokens_skipped += 1;
                                    continue;
                                }

                                // 🛡️ RISK AGENT CHECK - is token blocked?
                                if is_token_blocked(&token_str).await {
                                    warn!("❌ SKIP: Token blocked by risk agent");
                                    tokens_skipped += 1;
                                    continue;
                                }

                                // 🎯 BUNDLE CLUSTER CHECK - Detect coordinated buying (scams)
                                let (is_bundled, bundle_reason) = check_bundle_cluster(
                                    &http_client,
                                    &http_rpc_url,
                                    token_addr,
                                    router_address,
                                    &bundle_config,
                                    &bundle_cache,
                                )
                                .await;

                                if is_bundled {
                                    warn!("❌ SKIP: {}", bundle_reason);
                                    tokens_skipped += 1;

                                    // Block bundled token for 2 hours
                                    block_token(&token_str, &bundle_reason, 7200).await;

                                    // 📱 Telegram alert: Bundle detected
                                    if !tg_token.is_empty() && !tg_chat_id.is_empty() {
                                        let msg = format!(
                                            "🎯 BUNDLE DETECTED!\n📍 Token: {}...{}\n⚠️ {}\n🐳 Whale: {:.0} MON",
                                            &token_str[..8], &token_str[token_str.len()-4..],
                                            bundle_reason, value_mon
                                        );
                                        let _ = send_telegram(
                                            &http_client,
                                            &tg_token,
                                            &tg_chat_id,
                                            &msg,
                                        )
                                        .await;
                                    }
                                    continue;
                                }
                                debug!("🎯 Bundle check: {}", bundle_reason);

                                // 🔒 HONEYPOT CHECK - Critical safety! (with cache + timeout)
                                let (is_safe, sell_tax, honeypot_reason) =
                                    check_honeypot_with_cache(
                                        &http_client,
                                        &http_rpc_url,
                                        token_addr,
                                        &honeypot_config,
                                        &honeypot_cache,
                                    )
                                    .await;

                                info!(is_safe, sell_tax = %format!("{:.1}%", sell_tax), "{}", honeypot_reason);

                                if !is_safe {
                                    warn!("❌ SKIP: Honeypot or high tax detected!");
                                    tokens_skipped += 1;

                                    // 🚫 Block token for 1 hour to avoid repeated checks
                                    block_token(&token_str, &honeypot_reason, 3600).await;

                                    // 📱 Telegram alert: Honeypot detected
                                    if !tg_token.is_empty() && !tg_chat_id.is_empty() {
                                        let msg = format!(
                                            "🚨 HONEYPOT BLOCKED!\n📍 Token: {}...{}\n⚠️ {}\n🐳 Whale: {:.0} MON",
                                            &token_str[..8], &token_str[token_str.len()-4..],
                                            honeypot_reason, value_mon
                                        );
                                        let _ = send_telegram(
                                            &http_client,
                                            &tg_token,
                                            &tg_chat_id,
                                            &msg,
                                        )
                                        .await;
                                    }
                                    continue;
                                }

                                // Warn on high tax but allow if score is very high
                                if sell_tax > 10.0 {
                                    if score < 80 {
                                        warn!(
                                            sell_tax,
                                            "❌ SKIP: Tax >10% and score not high enough"
                                        );
                                        tokens_skipped += 1;
                                        continue;
                                    }
                                    warn!(
                                        sell_tax,
                                        score, "⚠️ High tax but proceeding due to high score"
                                    );
                                }

                                // All checks passed - FOLLOW THE WHALE!
                                // Dynamic position sizing - CONSERVATIVE!
                                let is_trusted = trusted_wallets.contains(&buyer);
                                let actual_follow_amount = if is_trusted {
                                    follow_amount_mon * 1.5 // 🌟 TRUSTED WALLET = 1.5x position (15 MON)
                                } else if value_mon >= 1000.0 {
                                    follow_amount_mon * 2.0 // 1000+ MON whale = 2x position (20 MON)
                                } else if value_mon >= 500.0 {
                                    follow_amount_mon * 1.5 // 500+ MON whale = 1.5x position (15 MON)
                                } else {
                                    follow_amount_mon // Normal = base position (10 MON)
                                };

                                // 🛡️ SAFETY CHECK 3: Don't spend more than 20% of balance
                                let max_spend = balance_mon * 0.20;
                                let actual_follow_amount = actual_follow_amount.min(max_spend);

                                if is_trusted {
                                    info!(
                                        actual_follow_amount,
                                        whale_size = value_mon,
                                        "🌟 FOLLOWING TRUSTED WALLET!"
                                    );
                                } else {
                                    info!(
                                        actual_follow_amount,
                                        whale_size = value_mon,
                                        "✅ FOLLOWING WHALE!"
                                    );
                                }

                                let amount_wei = U256::from((actual_follow_amount * 1e18) as u128);
                                let deadline = U256::from(
                                    SystemTime::now()
                                        .duration_since(UNIX_EPOCH)
                                        .unwrap()
                                        .as_secs()
                                        + 120,
                                );

                                let buy_params = BuyParams {
                                    amountOutMin: U256::ZERO,
                                    token: token_addr,
                                    to: bot_address,
                                    deadline,
                                };
                                let buy_call = buyCall { params: buy_params };
                                let calldata = buy_call.abi_encode();

                                let tx_req = alloy::rpc::types::TransactionRequest::default()
                                    .to(router_address)
                                    .value(amount_wei)
                                    .input(calldata.into())
                                    .gas_limit(500_000)
                                    .max_priority_fee_per_gas(1_000_000_000_000);

                                match provider.send_transaction(tx_req).await {
                                    Ok(pending) => {
                                        let buy_tx_hash = *pending.tx_hash();
                                        info!(?buy_tx_hash, "📤 BUY TX SENT");

                                        match tokio::time::timeout(
                                            std::time::Duration::from_secs(30),
                                            pending.get_receipt(),
                                        )
                                        .await
                                        {
                                            Ok(Ok(receipt)) => {
                                                if receipt.status() {
                                                    info!(
                                                        gas = receipt.gas_used,
                                                        "🎉 SUCCESS! Followed whale!"
                                                    );
                                                    tokens_followed += 1;
                                                    followed_tokens.insert(token_str.clone());

                                                    // Track whale position for exit detection
                                                    let whale_key =
                                                        (format!("{:?}", buyer), token_str.clone());
                                                    whale_positions.insert(whale_key, value_mon);
                                                    info!(
                                                        tracked_whales = whale_positions.len(),
                                                        "📊 Tracking whale for exit signals"
                                                    );

                                                    // 📱 Telegram alert: Buy success
                                                    if !tg_token.is_empty()
                                                        && !tg_chat_id.is_empty()
                                                    {
                                                        let msg = format!(
                                                            "✅ BOUGHT {:.1} MON\n📍 Token: {}...{}\n📊 Score: {} | Whale: {:.0} MON\n🔗 TX: {}",
                                                            actual_follow_amount,
                                                            &token_str[..8], &token_str[token_str.len()-4..],
                                                            score, value_mon,
                                                            buy_tx_hash
                                                        );
                                                        let _ = send_telegram(
                                                            &http_client,
                                                            &tg_token,
                                                            &tg_chat_id,
                                                            &msg,
                                                        )
                                                        .await;
                                                    }

                                                    // Save position
                                                    let position = serde_json::json!({
                                                        "token_address": token_str,
                                                        "token_name": format!("Whale Follow {}", &token_str[..8]),
                                                        "amount_mon": actual_follow_amount,
                                                        "entry_price_mon": actual_follow_amount,
                                                        "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
                                                        "score": score,
                                                        "whale_buy_mon": value_mon,
                                                        "followed_whale": format!("{:?}", buyer)
                                                    });

                                                    let path = "positions.json";
                                                    let mut positions: serde_json::Value =
                                                        std::fs::read_to_string(path)
                                                            .ok()
                                                            .and_then(|s| {
                                                                serde_json::from_str(&s).ok()
                                                            })
                                                            .unwrap_or(serde_json::json!({}));

                                                    positions[&token_str] = position;
                                                    let _ = std::fs::write(
                                                        path,
                                                        serde_json::to_string_pretty(&positions)
                                                            .unwrap(),
                                                    );

                                                    info!("📊 Stats: Whales={}, Followed={}, Skipped={}", whales_seen, tokens_followed, tokens_skipped);
                                                } else {
                                                    error!("❌ TX FAILED!");
                                                    // 📱 Telegram alert: Buy failed
                                                    if !tg_token.is_empty()
                                                        && !tg_chat_id.is_empty()
                                                    {
                                                        let msg = format!(
                                                            "❌ BUY FAILED: {}...{}",
                                                            &token_str[..8],
                                                            &token_str[token_str.len() - 4..]
                                                        );
                                                        let _ = send_telegram(
                                                            &http_client,
                                                            &tg_token,
                                                            &tg_chat_id,
                                                            &msg,
                                                        )
                                                        .await;
                                                    }
                                                }
                                            }
                                            Ok(Err(e)) => error!(?e, "Receipt error"),
                                            Err(_) => error!("Receipt timeout"),
                                        }
                                    }
                                    Err(e) => error!(?e, "TX send error"),
                                }
                            }
                        }
                    }
                }
            }
        }

        // Cleanup
        if processed_txs.len() > 1000 {
            processed_txs.clear();
        }
    }

    Ok(())
}
