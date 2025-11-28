use alloy::{
    providers::{Provider, ProviderBuilder},
    primitives::{Address, U256, B256},
    rpc::types::BlockTransactionsKind,
    consensus::Transaction as _,
    network::EthereumWallet,
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use std::{env, str::FromStr, time::{SystemTime, UNIX_EPOCH}, collections::{HashSet, HashMap}};
use dotenv::dotenv;
use anyhow::Result;
use chrono::Local;
use url::Url;
use serde::{Deserialize, Serialize};
use reqwest::Client;

// ═══════════════════════════════════════════════════════════════════════════════
// 🔥 GOD MODE v2: CREATOR ANALYSIS + DEXSCREENER + WHALE MODE 🔥
// ═══════════════════════════════════════════════════════════════════════════════
// Analizujemy TWÓRCĘ tokena, nie tylko nazwę!
// - Historia creatora (ile tokenów stworzył)
// - Czy poprzednie tokeny były udane
// - Liquidity i volume z DexScreener
// ═══════════════════════════════════════════════════════════════════════════════

sol! {
    #[derive(Debug)]
    function buy(uint256 tokenId, address tokenAddress, address recipient, uint256 deadline) external payable;
}

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
    #[serde(rename = "baseToken")]
    base_token: Option<DexToken>,
    #[serde(rename = "priceUsd")]
    price_usd: Option<String>,
    #[serde(rename = "priceChange")]
    price_change: Option<PriceChange>,
    liquidity: Option<Liquidity>,
    volume: Option<Volume>,
    #[serde(rename = "txns")]
    transactions: Option<Transactions>,
    #[serde(rename = "pairCreatedAt")]
    pair_created_at: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct DexToken {
    address: Option<String>,
    name: Option<String>,
    symbol: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PriceChange {
    h1: Option<f64>,
    h24: Option<f64>,
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
// 🧠 CREATOR REPUTATION TRACKER
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct CreatorStats {
    tokens_created: u32,
    successful_tokens: u32,  // Tokens that pumped >2x
    rugged_tokens: u32,      // Tokens that dumped >90%
    total_volume_usd: f64,
    avg_liquidity_usd: f64,
    reputation_score: u8,    // 0-100
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
// 🔍 DEXSCREENER API
// ═══════════════════════════════════════════════════════════════════════════════

async fn get_token_info(client: &Client, token_address: &str) -> Option<DexPair> {
    let url = format!("https://api.dexscreener.com/latest/dex/tokens/{}", token_address);
    
    match client.get(&url).send().await {
        Ok(resp) => {
            if let Ok(data) = resp.json::<DexScreenerResponse>().await {
                if let Some(pairs) = data.pairs {
                    // Return the pair with highest liquidity
                    return pairs.into_iter()
                        .filter(|p| p.chain_id.as_deref() == Some("monad"))
                        .max_by(|a, b| {
                            let liq_a = a.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
                            let liq_b = b.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
                            liq_a.partial_cmp(&liq_b).unwrap()
                        });
                }
            }
        }
        Err(e) => print_log(&format!("❌ DexScreener Error: {:?}", e)),
    }
    None
}

async fn analyze_token_quality(client: &Client, token_address: &str) -> (u8, String) {
    if let Some(pair) = get_token_info(client, token_address).await {
        let liquidity = pair.liquidity.as_ref().and_then(|l| l.usd).unwrap_or(0.0);
        let volume_24h = pair.volume.as_ref().and_then(|v| v.h24).unwrap_or(0.0);
        let price_change_1h = pair.price_change.as_ref().and_then(|p| p.h1).unwrap_or(0.0);
        let buys = pair.transactions.as_ref().and_then(|t| t.h24.as_ref()).and_then(|t| t.buys).unwrap_or(0);
        let sells = pair.transactions.as_ref().and_then(|t| t.h24.as_ref()).and_then(|t| t.sells).unwrap_or(0);
        
        let mut score = 50u8;
        let mut reasons = Vec::new();
        
        // Liquidity score
        if liquidity > 100000.0 {
            score = score.saturating_add(20);
            reasons.push(format!("High liq ${:.0}k", liquidity/1000.0));
        } else if liquidity > 10000.0 {
            score = score.saturating_add(10);
            reasons.push(format!("Good liq ${:.0}k", liquidity/1000.0));
        } else if liquidity < 1000.0 {
            score = score.saturating_sub(20);
            reasons.push("Low liq".to_string());
        }
        
        // Volume score
        if volume_24h > 50000.0 {
            score = score.saturating_add(15);
            reasons.push(format!("High vol ${:.0}k", volume_24h/1000.0));
        }
        
        // Buy/Sell ratio
        if buys > 0 && sells > 0 {
            let ratio = buys as f64 / sells as f64;
            if ratio > 2.0 {
                score = score.saturating_add(10);
                reasons.push(format!("Bullish {:.1}x buys", ratio));
            } else if ratio < 0.5 {
                score = score.saturating_sub(15);
                reasons.push("Bearish sells".to_string());
            }
        }
        
        // Price momentum
        if price_change_1h > 50.0 {
            score = score.saturating_add(10);
            reasons.push(format!("+{:.0}% 1h", price_change_1h));
        } else if price_change_1h < -30.0 {
            score = score.saturating_sub(10);
            reasons.push(format!("{:.0}% 1h dump", price_change_1h));
        }
        
        return (score.min(100), reasons.join(" | "));
    }
    
    (50, "No DexScreener data yet".to_string())
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📋 HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

fn print_log(msg: &str) {
    println!("[{}] {}", Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
}

fn calculate_whale_amount(score: u8, min_mon: f64, max_mon: f64) -> f64 {
    if score >= 85 {
        max_mon // 🐳 WHALE
    } else if score >= 75 {
        min_mon + (max_mon - min_mon) * 0.6 // 🦈 SHARK
    } else if score >= 65 {
        min_mon + (max_mon - min_mon) * 0.3 // 🐟 FISH
    } else if score >= 55 {
        min_mon // 🐟 SMALL
    } else {
        0.0 // 💩 SKIP
    }
}

fn decode_create_token_params(input: &[u8]) -> Option<(String, String)> {
    if input.len() < 100 { return None; }
    let mut found_strings = Vec::new();
    let mut current_string = String::new();
    for &byte in input.iter().skip(4) {
        if byte >= 32 && byte <= 126 { current_string.push(byte as char); } 
        else {
            if current_string.len() >= 2 && current_string.len() <= 30 {
                if current_string.chars().all(|c| c.is_alphanumeric() || c == ' ' || c == '-' || c == '_') {
                    found_strings.push(current_string.clone());
                }
            }
            current_string.clear();
        }
    }
    if current_string.len() >= 2 { found_strings.push(current_string); }
    
    if found_strings.len() >= 2 { Some((found_strings[0].clone(), found_strings[1].clone())) }
    else if found_strings.len() == 1 { Some((found_strings[0].clone(), found_strings[0].clone())) }
    else { None }
}

fn passes_blacklist(name: &str, symbol: &str) -> bool {
    let blacklist = ["test", "scam", "rug", "honeypot", "fake", "airdrop", "free"];
    let name_lower = name.to_lowercase();
    let symbol_lower = symbol.to_lowercase();
    
    !blacklist.iter().any(|word| name_lower.contains(word) || symbol_lower.contains(word))
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 MAIN
// ═══════════════════════════════════════════════════════════════════════════════

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();
    
    print_log("╔═══════════════════════════════════════════════════════════════╗");
    print_log("║  🔥 GOD MODE v2: CREATOR ANALYSIS + DEXSCREENER 🔥           ║");
    print_log("╚═══════════════════════════════════════════════════════════════╝");

    let rpc_url_str = env::var("MONAD_RPC_URL").expect("Brak MONAD_RPC_URL");
    let private_key = env::var("PRIVATE_KEY").expect("Brak PRIVATE_KEY");
    
    let rpc_url = Url::parse(&rpc_url_str)?;
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer.clone());
    let bot_address = signer.address();
    
    // Config
    let whale_min = env::var("WHALE_MIN_AMOUNT_MON").unwrap_or("5.0".to_string()).parse::<f64>().unwrap();
    let whale_max = env::var("WHALE_MAX_AMOUNT_MON").unwrap_or("50.0".to_string()).parse::<f64>().unwrap();
    let min_score = env::var("MIN_BUY_SCORE").unwrap_or("55".to_string()).parse::<u8>().unwrap();
    let router_str = env::var("ROUTER_ADDRESS").unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let router_address = Address::from_str(&router_str)?;
    
    print_log(&format!("👤 Wallet: {:?}", bot_address));
    print_log(&format!("🐳 Whale Range: {}-{} MON", whale_min, whale_max));
    print_log(&format!("📊 Min Score: {}", min_score));
    print_log(&format!("🔗 Router: {:?}", router_address));
    
    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_http(rpc_url);

    let http_client = Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()?;

    let mut last_block = provider.get_block_number().await?;
    let mut processed_txs = HashSet::new();
    let mut creator_db = load_creator_db();
    
    // Stats
    let mut tokens_seen = 0u32;
    let mut tokens_bought = 0u32;
    let mut tokens_skipped = 0u32;
    
    print_log(&format!("📦 Starting from block: {}", last_block));
    print_log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

    loop {
        let current_block = provider.get_block_number().await?;
        if current_block > last_block {
            if let Ok(Some(block)) = provider.get_block_by_number(current_block.into(), BlockTransactionsKind::Full).await {
                if let Some(txs) = block.transactions.as_transactions() {
                    for tx in txs {
                        let tx_hash = tx.inner.tx_hash();
                        if processed_txs.contains(tx_hash) { continue; }
                        processed_txs.insert(*tx_hash);

                        if let Some(to) = tx.to() {
                            if to == router_address {
                                let input = tx.input();
                                if input.len() >= 4 && hex::encode(&input[0..4]) == "ba12cd8d" {
                                    tokens_seen += 1;
                                    let creator = tx.from;
                                    let creator_str = format!("{:?}", creator);
                                    
                                    print_log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                    print_log(&format!("🚀 NEW TOKEN #{} | Block: {}", tokens_seen, current_block));
                                    print_log(&format!("   👤 Creator: {}", &creator_str[..12]));
                                    
                                    if let Some((name, symbol)) = decode_create_token_params(input) {
                                        print_log(&format!("   📝 {} ({})", name, symbol));
                                        
                                        // 1. Blacklist check
                                        if !passes_blacklist(&name, &symbol) {
                                            print_log("   🚫 BLACKLISTED NAME! Skipping.");
                                            tokens_skipped += 1;
                                            continue;
                                        }
                                        
                                        // 2. Creator reputation check
                                        let creator_stats = creator_db.get(&creator_str).cloned().unwrap_or_default();
                                        if creator_stats.rugged_tokens > 0 {
                                            print_log(&format!("   ⚠️  KNOWN RUGGER! ({} rugs) Skipping.", creator_stats.rugged_tokens));
                                            tokens_skipped += 1;
                                            continue;
                                        }
                                        
                                        if creator_stats.tokens_created > 0 {
                                            let success_rate = (creator_stats.successful_tokens as f64 / creator_stats.tokens_created as f64) * 100.0;
                                            print_log(&format!("   📊 Creator: {} tokens, {:.0}% success rate", 
                                                creator_stats.tokens_created, success_rate));
                                        } else {
                                            print_log("   📊 Creator: New dev (no history)");
                                        }
                                        
                                        // 3. Wait a bit for token to appear on DexScreener
                                        print_log("   ⏳ Waiting 3s for DEX listing...");
                                        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
                                        
                                        // 4. Get token address from receipt
                                        let mut token_addr = None;
                                        let mut token_id = U256::ZERO;
                                        
                                        for _ in 0..10 {
                                            if let Ok(Some(receipt)) = provider.get_transaction_receipt(*tx_hash).await {
                                                for log in receipt.inner.logs() {
                                                    if let Some(topic0) = log.topics().first() {
                                                        if topic0 == &B256::from_str("0xd37e3f4f651fe74251701614dbeac478f5a0d29068e87bbe44e5026d166abca9").unwrap() {
                                                            if let Some(topic1) = log.topics().get(1) { 
                                                                token_id = U256::from_be_bytes(topic1.0); 
                                                            }
                                                            if let Some(topic2) = log.topics().get(2) { 
                                                                token_addr = Some(Address::from_slice(&topic2[12..32])); 
                                                                break; 
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                            if token_addr.is_some() { break; }
                                            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                                        }
                                        
                                        if let Some(addr) = token_addr {
                                            let token_addr_str = format!("{:?}", addr);
                                            print_log(&format!("   📍 Token: {} (ID: {})", &token_addr_str[..12], token_id));
                                            
                                            // 5. DexScreener analysis
                                            print_log("   🔍 Checking DexScreener...");
                                            let (dex_score, dex_reason) = analyze_token_quality(&http_client, &token_addr_str).await;
                                            
                                            // 6. Calculate final score
                                            let creator_bonus: i16 = if creator_stats.successful_tokens > 2 { 10 } else { 0 };
                                            let newbie_penalty: i16 = if creator_stats.tokens_created == 0 { -5 } else { 0 };
                                            
                                            let final_score = ((dex_score as i16) + creator_bonus + newbie_penalty).max(0).min(100) as u8;
                                            
                                            let emoji = if final_score >= 80 { "🔥🔥🔥" } 
                                                       else if final_score >= 70 { "✨✨" } 
                                                       else if final_score >= 60 { "⚡" } 
                                                       else { "💩" };
                                            
                                            print_log(&format!("   {} Final Score: {}/100 | {}", emoji, final_score, dex_reason));
                                            
                                            // 7. Calculate buy amount
                                            let amount_mon = calculate_whale_amount(final_score, whale_min, whale_max);
                                            
                                            if amount_mon > 0.0 && final_score >= min_score {
                                                let whale_emoji = if amount_mon >= whale_max * 0.8 { "🐳" } 
                                                                 else if amount_mon >= whale_max * 0.5 { "🦈" } 
                                                                 else { "🐟" };
                                                
                                                print_log(&format!("   {} Buying: {:.1} MON", whale_emoji, amount_mon));
                                                
                                                let amount_wei = U256::from((amount_mon * 1e18) as u128);
                                                let deadline = U256::from(SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() + 120);
                                                
                                                let call = buyCall {
                                                    tokenId: token_id,
                                                    tokenAddress: addr,
                                                    recipient: bot_address,
                                                    deadline,
                                                };
                                                
                                                let tx_req = alloy::rpc::types::TransactionRequest::default()
                                                    .to(router_address)
                                                    .value(amount_wei)
                                                    .input(call.abi_encode().into())
                                                    .gas_limit(8_000_000)
                                                    .max_priority_fee_per_gas(1_000_000_000_000);

                                                match provider.send_transaction(tx_req).await {
                                                    Ok(pending) => {
                                                        print_log(&format!("   ✅ BUY SENT: {:?}", pending.tx_hash()));
                                                        tokens_bought += 1;
                                                        
                                                        // Update creator stats
                                                        let stats = creator_db.entry(creator_str.clone()).or_default();
                                                        stats.tokens_created += 1;
                                                        save_creator_db(&creator_db);
                                                        
                                                        // Save position
                                                        let position = serde_json::json!({
                                                            "token_address": token_addr_str,
                                                            "token_name": format!("{} ({})", name, symbol),
                                                            "amount_mon": amount_mon,
                                                            "entry_price_mon": amount_mon,
                                                            "peak_price_mon": amount_mon,
                                                            "timestamp": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
                                                            "creator": creator_str,
                                                            "score": final_score,
                                                            "trailing_active": false,
                                                            "partial_sold": false
                                                        });
                                                        
                                                        let path = "positions.json";
                                                        let mut positions: serde_json::Value = std::fs::read_to_string(path)
                                                            .ok()
                                                            .and_then(|s| serde_json::from_str(&s).ok())
                                                            .unwrap_or(serde_json::json!({}));
                                                        
                                                        positions[token_addr_str] = position;
                                                        let _ = std::fs::write(path, serde_json::to_string_pretty(&positions).unwrap());
                                                    },
                                                    Err(e) => print_log(&format!("   ❌ BUY FAILED: {:?}", e)),
                                                }
                                            } else {
                                                print_log(&format!("   💩 Score {} < {} minimum. Skipping.", final_score, min_score));
                                                tokens_skipped += 1;
                                            }
                                        }
                                    }
                                    
                                    // Stats
                                    print_log(&format!("   📈 Stats: Seen={} | Bought={} | Skipped={}", 
                                        tokens_seen, tokens_bought, tokens_skipped));
                                }
                            }
                        }
                    }
                }
            }
            last_block = current_block;
        }
        
        // Cleanup old processed txs
        if processed_txs.len() > 1000 {
            processed_txs.clear();
        }
        
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
}
