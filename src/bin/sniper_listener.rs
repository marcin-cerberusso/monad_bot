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
use std::{env, str::FromStr, time::{SystemTime, UNIX_EPOCH}, collections::HashSet};
use dotenv::dotenv;
use anyhow::{Result, Context};
use chrono::Local;
use url::Url;
use serde::{Deserialize, Serialize};

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 GOD MODE: SNIPER WITH GEMINI AI BRAIN
// ═══════════════════════════════════════════════════════════════════════════════
// Przed każdym zakupem konsultujemy się z Gemini AI!
// AI ocenia: nazwę tokena, potencjał viralowy, czerwone flagi
// Efekt: Kupujemy tylko potencjalne gemy, omijamy śmieci!
// ═══════════════════════════════════════════════════════════════════════════════

sol! {
    #[derive(Debug)]
    function buy(uint256 tokenId, address tokenAddress, address recipient, uint256 deadline) external payable;
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🧠 GEMINI AI FILTER
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone)]
struct GeminiConfig {
    api_key: String,
    enabled: bool,
    min_score: u32,          // Minimalny score do zakupu (0-100)
    timeout_ms: u64,         // Timeout na odpowiedź AI
}

impl GeminiConfig {
    fn from_env() -> Self {
        Self {
            api_key: env::var("GEMINI_API_KEY").unwrap_or_default(),
            enabled: env::var("AI_FILTER_ENABLED").unwrap_or("true".to_string()) == "true",
            min_score: env::var("AI_MIN_SCORE").unwrap_or("60".to_string()).parse().unwrap_or(60),
            timeout_ms: env::var("AI_TIMEOUT_MS").unwrap_or("3000".to_string()).parse().unwrap_or(3000),
        }
    }
    
    fn is_available(&self) -> bool {
        self.enabled && !self.api_key.is_empty()
    }
}

#[derive(Debug, Clone, Serialize)]
struct GeminiRequest {
    contents: Vec<GeminiContent>,
    generation_config: GeminiGenerationConfig,
}

#[derive(Debug, Clone, Serialize)]
struct GeminiContent {
    parts: Vec<GeminiPart>,
}

#[derive(Debug, Clone, Serialize)]
struct GeminiPart {
    text: String,
}

#[derive(Debug, Clone, Serialize)]
struct GeminiGenerationConfig {
    temperature: f32,
    max_output_tokens: u32,
}

#[derive(Debug, Clone, Deserialize)]
struct GeminiResponse {
    candidates: Option<Vec<GeminiCandidate>>,
}

#[derive(Debug, Clone, Deserialize)]
struct GeminiCandidate {
    content: GeminiContentResponse,
}

#[derive(Debug, Clone, Deserialize)]
struct GeminiContentResponse {
    parts: Vec<GeminiPartResponse>,
}

#[derive(Debug, Clone, Deserialize)]
struct GeminiPartResponse {
    text: String,
}

#[derive(Debug, Clone)]
struct AiEvaluation {
    score: u32,           // 0-100
    verdict: String,      // BUY / SKIP / RISKY
    reasoning: String,    // Krótkie wyjaśnienie
}

async fn evaluate_token_with_gemini(
    config: &GeminiConfig,
    name: &str,
    symbol: &str,
) -> Result<AiEvaluation> {
    let prompt = format!(
        r#"You are a crypto meme coin analyst. Evaluate this NEW token for pump potential.

TOKEN NAME: {}
TOKEN SYMBOL: {}

Rate 0-100 based on:
- Catchy/memorable name (viral potential)
- Meme culture relevance (trending topics, celebrities, animals, food)
- Symbol quality (short, punchy, memorable)
- Red flags (scam keywords, too generic, offensive)

RESPOND IN EXACTLY THIS FORMAT (3 lines only):
SCORE: [number 0-100]
VERDICT: [BUY/SKIP/RISKY]
REASON: [one short sentence why]

Examples of HIGH scores (70+): PEPE, DOGE, BONK, WIF, POPCAT, BRETT
Examples of LOW scores (<40): TEST, TOKEN123, SCAM, RUGPULL"#,
        name, symbol
    );

    let request = GeminiRequest {
        contents: vec![GeminiContent {
            parts: vec![GeminiPart { text: prompt }],
        }],
        generation_config: GeminiGenerationConfig {
            temperature: 0.3,
            max_output_tokens: 100,
        },
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(config.timeout_ms))
        .build()?;

    let api_url = format!(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={}",
        config.api_key
    );

    let response = client
        .post(&api_url)
        .json(&request)
        .send()
        .await?;

    let gemini_response: GeminiResponse = response.json().await?;
    
    // Parse response
    if let Some(candidates) = gemini_response.candidates {
        if let Some(candidate) = candidates.first() {
            if let Some(part) = candidate.content.parts.first() {
                return parse_ai_response(&part.text);
            }
        }
    }

    Err(anyhow::anyhow!("Empty AI response"))
}

fn parse_ai_response(text: &str) -> Result<AiEvaluation> {
    let lines: Vec<&str> = text.lines().collect();
    
    let mut score = 50u32;
    let mut verdict = "SKIP".to_string();
    let mut reasoning = "Could not parse AI response".to_string();
    
    for line in lines {
        let line_upper = line.to_uppercase();
        
        if line_upper.starts_with("SCORE:") {
            if let Some(num_str) = line.split(':').nth(1) {
                if let Ok(num) = num_str.trim().parse::<u32>() {
                    score = num.min(100);
                }
            }
        } else if line_upper.starts_with("VERDICT:") {
            if let Some(v) = line.split(':').nth(1) {
                verdict = v.trim().to_uppercase();
            }
        } else if line_upper.starts_with("REASON:") {
            if let Some(r) = line.split(':').nth(1) {
                reasoning = r.trim().to_string();
            }
        }
    }
    
    Ok(AiEvaluation { score, verdict, reasoning })
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📋 HELPER FUNCTIONS
// ═══════════════════════════════════════════════════════════════════════════════

fn print_log(msg: &str) {
    println!("[{}] {}", Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
}

fn decode_create_token_params(input: &[u8]) -> Option<(String, String)> {
    if input.len() < 100 { return None; }
    let mut found_strings = Vec::new();
    let mut current_string = String::new();
    for &byte in input.iter().skip(4) {
        if (32..=126).contains(&byte) { current_string.push(byte as char); } 
        else {
            if (2..=30).contains(&current_string.len()) 
                && current_string.chars().all(|c| c.is_alphanumeric() || c == ' ' || c == '-' || c == '_') {
                found_strings.push(current_string.clone());
            }
            current_string.clear();
        }
    }
    if current_string.len() >= 2 { found_strings.push(current_string); }
    
    if found_strings.len() >= 2 { Some((found_strings[0].clone(), found_strings[1].clone())) }
    else if found_strings.len() == 1 { Some((found_strings[0].clone(), found_strings[0].clone())) }
    else { None }
}

fn passes_basic_filters(name: &str, symbol: &str, blacklist: &HashSet<String>, min_len: usize) -> bool {
    let name_lower = name.to_lowercase();
    let symbol_lower = symbol.to_lowercase();
    if name.len() < min_len || symbol.len() < min_len { return false; }
    for word in blacklist { 
        if name_lower.contains(word) || symbol_lower.contains(word) { 
            return false; 
        } 
    }
    true
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 MAIN
// ═══════════════════════════════════════════════════════════════════════════════

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();
    
    print_log("═══════════════════════════════════════════════════════════════");
    print_log("🔥 GOD MODE SNIPER: GEMINI AI BRAIN ACTIVATED 🧠");
    print_log("═══════════════════════════════════════════════════════════════");

    // Load configs
    let gemini_config = GeminiConfig::from_env();
    
    if gemini_config.is_available() {
        print_log(&format!("🧠 AI Filter: ENABLED (min score: {})", gemini_config.min_score));
    } else {
        print_log("⚠️  AI Filter: DISABLED (no GEMINI_API_KEY)");
    }

    let rpc_url_str = env::var("MONAD_RPC_URL").expect("Brak MONAD_RPC_URL");
    let private_key = env::var("PRIVATE_KEY").expect("Brak PRIVATE_KEY");
    let rpc_url = Url::parse(&rpc_url_str)?;
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer.clone());
    let bot_address = signer.address();
    
    print_log(&format!("👤 Wallet: {:?}", bot_address));
    
    let snipe_amount_mon = env::var("AUTO_SNIPE_AMOUNT_MON").unwrap_or("10.0".to_string()).parse::<f64>().unwrap();
    let min_name_length = env::var("AUTO_SNIPE_MIN_NAME_LENGTH").unwrap_or("2".to_string()).parse::<usize>().unwrap();
    let cooldown_sec = env::var("AUTO_SNIPE_COOLDOWN_SEC").unwrap_or("10".to_string()).parse::<u64>().unwrap();
    let blacklist_str = env::var("AUTO_SNIPE_BLACKLIST").unwrap_or("test,scam,rug,honeypot".to_string());
    let blacklist: HashSet<String> = blacklist_str.split(',').map(|s| s.trim().to_lowercase()).collect();
    
    print_log(&format!("💰 Snipe Amount: {} MON", snipe_amount_mon));
    print_log(&format!("⏱️  Cooldown: {}s", cooldown_sec));
    print_log(&format!("🚫 Blacklist: {:?}", blacklist));
    print_log("═══════════════════════════════════════════════════════════════");
    
    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_http(rpc_url);

    let router_str = env::var("ROUTER_ADDRESS").unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let router_address = Address::from_str(&router_str).context("Nieprawidłowy adres ROUTER_ADDRESS")?;
    let mut last_block = provider.get_block_number().await?;
    let mut last_snipe_time = 0u64;
    
    // Stats
    let mut tokens_seen = 0u32;
    let mut tokens_approved = 0u32;
    let mut tokens_rejected = 0u32;

    print_log(&format!("👀 Watching Router: {:?}", router_address));
    print_log(&format!("📦 Starting from block: {}", last_block));
    print_log("═══════════════════════════════════════════════════════════════");

    loop {
        let current_block = provider.get_block_number().await?;
        if current_block > last_block {
            if let Ok(Some(block)) = provider.get_block_by_number(current_block.into(), BlockTransactionsKind::Full).await {
                if let Some(txs) = block.transactions.as_transactions() {
                    for tx in txs {
                        if let Some(to) = tx.to() {
                            if to == router_address {
                                let input = tx.input();
                                if input.len() >= 4 && hex::encode(&input[0..4]) == "ba12cd8d" {
                                    print_log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                    print_log(&format!("🚀 NEW TOKEN DETECTED! TX: {:?}", tx.inner.tx_hash()));
                                    tokens_seen += 1;
                                    
                                    if let Some((name, symbol)) = decode_create_token_params(input) {
                                        print_log(&format!("📝 Name: {} | Symbol: {}", name, symbol));
                                        
                                        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
                                        
                                        // Check cooldown
                                        if now - last_snipe_time < cooldown_sec {
                                            print_log(&format!("⏳ Cooldown active ({}s remaining)", cooldown_sec - (now - last_snipe_time)));
                                            continue;
                                        }
                                        
                                        // Basic filters
                                        if !passes_basic_filters(&name, &symbol, &blacklist, min_name_length) {
                                            print_log("❌ REJECTED: Failed basic filters (blacklist/length)");
                                            tokens_rejected += 1;
                                            continue;
                                        }
                                        
                                        // ═══════════════════════════════════════════════════════════════
                                        // 🧠 GEMINI AI EVALUATION
                                        // ═══════════════════════════════════════════════════════════════
                                        let mut ai_approved = true;
                                        
                                        if gemini_config.is_available() {
                                            print_log("🧠 Asking Gemini AI...");
                                            
                                            match evaluate_token_with_gemini(&gemini_config, &name, &symbol).await {
                                                Ok(eval) => {
                                                    let score_emoji = match eval.score {
                                                        80..=100 => "🔥🔥🔥",
                                                        60..=79 => "✨✨",
                                                        40..=59 => "⚡",
                                                        _ => "💩",
                                                    };
                                                    
                                                    print_log(&format!(
                                                        "🤖 AI Score: {}/100 {} | Verdict: {} | {}",
                                                        eval.score, score_emoji, eval.verdict, eval.reasoning
                                                    ));
                                                    
                                                    if eval.score < gemini_config.min_score {
                                                        print_log(&format!(
                                                            "❌ AI REJECTED: Score {} < {} minimum",
                                                            eval.score, gemini_config.min_score
                                                        ));
                                                        tokens_rejected += 1;
                                                        ai_approved = false;
                                                    } else {
                                                        print_log(&format!(
                                                            "✅ AI APPROVED: Score {} >= {} minimum",
                                                            eval.score, gemini_config.min_score
                                                        ));
                                                    }
                                                }
                                                Err(e) => {
                                                    print_log(&format!("⚠️  AI Error: {:?} - proceeding without AI", e));
                                                    // Continue without AI on error
                                                }
                                            }
                                        }
                                        
                                        if !ai_approved {
                                            continue;
                                        }
                                        
                                        // ═══════════════════════════════════════════════════════════════
                                        // 🎯 EXECUTE BUY
                                        // ═══════════════════════════════════════════════════════════════
                                        print_log("🎯 BUYING NOW!!! 💰💰💰");
                                        tokens_approved += 1;
                                        last_snipe_time = now;
                                        
                                        // Wait for receipt to get token address
                                        let tx_hash = *tx.inner.tx_hash();
                                        let mut token_addr = None;
                                        let mut token_id = U256::ZERO;
                                        
                                        for _ in 0..20 { // 10 seconds max
                                            if let Ok(Some(receipt)) = provider.get_transaction_receipt(tx_hash).await {
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
                                            print_log(&format!("📍 Token Address: {:?}", addr));
                                            print_log(&format!("🔢 Token ID: {}", token_id));
                                            
                                            // Anti-bot delay
                                            print_log("⏳ Anti-bot delay (1.5s)...");
                                            tokio::time::sleep(std::time::Duration::from_millis(1500)).await;

                                            let amount_wei = U256::from((snipe_amount_mon * 1e18) as u64);
                                            let deadline = U256::from(SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() + 60);
                                            
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
                                                .max_priority_fee_per_gas(500_000_000_000);

                                            match provider.send_transaction(tx_req).await {
                                                Ok(pending) => {
                                                    print_log(&format!("✅ BUY TX SENT: {:?}", pending.tx_hash()));
                                                    
                                                    // Save position (GOD MODE compatible)
                                                    let position = serde_json::json!({
                                                        "token_address": format!("{:?}", addr),
                                                        "token_name": format!("{} ({})", name, symbol),
                                                        "amount_mon": snipe_amount_mon,
                                                        "entry_price_mon": snipe_amount_mon,
                                                        "peak_price_mon": snipe_amount_mon,
                                                        "timestamp": now,
                                                        "trailing_active": false,
                                                        "partial_sold": false
                                                    });
                                                    
                                                    let path = "positions.json";
                                                    let mut positions: serde_json::Value = std::fs::read_to_string(path)
                                                        .ok()
                                                        .and_then(|s| serde_json::from_str(&s).ok())
                                                        .unwrap_or(serde_json::json!({}));
                                                    
                                                    positions[format!("{:?}", addr)] = position;
                                                    let _ = std::fs::write(path, serde_json::to_string_pretty(&positions).unwrap());
                                                    
                                                    print_log(&format!(
                                                        "📊 Stats: Seen={} | Approved={} | Rejected={} | Rate={:.1}%",
                                                        tokens_seen, tokens_approved, tokens_rejected,
                                                        (tokens_approved as f64 / tokens_seen as f64) * 100.0
                                                    ));
                                                },
                                                Err(e) => print_log(&format!("❌ BUY FAILED: {:?}", e)),
                                            }
                                        } else {
                                            print_log("❌ Could not find token address in receipt");
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            last_block = current_block;
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
}
