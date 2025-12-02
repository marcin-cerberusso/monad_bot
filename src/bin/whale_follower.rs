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
    collections::HashSet,
    env,
    str::FromStr,
    time::{SystemTime, UNIX_EPOCH},
};

use futures::StreamExt;
use reqwest::Client;
use serde::Deserialize;

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
    info!("║  🐳 WHALE FOLLOWER v1 - Follow Smart Money on NAD.FUN 🐳     ║");
    info!("╚═══════════════════════════════════════════════════════════════╝");

    let ws_url_str = env::var("MONAD_WS_URL").expect("Missing MONAD_WS_URL");
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

    // 📱 Telegram config
    let tg_token = env::var("TELEGRAM_BOT_TOKEN").unwrap_or_default();
    let tg_chat_id = env::var("TELEGRAM_CHAT_ID").unwrap_or_default();

    // 🌟 TRUSTED WALLETS - get 500+ MON treatment (14 MON position)
    let trusted_wallets: HashSet<Address> = vec![
        "0x97B36E9D28C23fa665C947C222E3FffAfF284023", // Dev Wallet 7131
        "0x8D34e0165DFB2d70a5B5890e100581e0884F2EAb", // Trusted #2
        "0xe58982D5B56c07CDb18A04FC4429E658E6002d85", // Trusted #3
        "0x4a7906ab22fD2C4cC744D1e9b8bEa26a3F6FaDDA", // Trusted #4
    ]
    .into_iter()
    .filter_map(|s| Address::from_str(s).ok())
    .collect();

    // buy() selector: 0x6df9e92b
    let buy_selector = "6df9e92b";

    info!(wallet = %bot_address, min_whale_buy_mon, follow_amount_mon, min_score, min_liquidity, max_open_positions, trusted_count = trusted_wallets.len(), "📋 Config loaded");
    info!(
        "🛡️ Safety: Max {} positions, Min {} MON wallet balance",
        max_open_positions, min_wallet_balance_mon
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
