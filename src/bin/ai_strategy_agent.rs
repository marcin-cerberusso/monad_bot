// ═══════════════════════════════════════════════════════════════════════════════
// 🧠 AI STRATEGY AGENT - DeepSeek/Gemini powered trading intelligence
// With AUTO SELL and DYNAMIC STOP-LOSS
// ═══════════════════════════════════════════════════════════════════════════════

use alloy::{
    network::EthereumWallet,
    primitives::{Address, U256},
    providers::{Provider, ProviderBuilder, WalletProvider},
    rpc::types::TransactionRequest,
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use chrono::Local;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::str::FromStr;
use std::time::Duration;
use tokio::time::sleep;
use url::Url;

// ═══════════════════════════════════════════════════════════════════════════════
// 🦎 GECKOTERMINAL API STRUCTURES
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Deserialize)]
struct GeckoPoolsResponse {
    data: Vec<GeckoPool>,
}

#[derive(Debug, Deserialize)]
struct GeckoPool {
    id: String,
    attributes: GeckoPoolAttributes,
}

#[derive(Debug, Deserialize)]
struct GeckoPoolAttributes {
    name: String,
    address: String,
    #[serde(rename = "base_token_price_usd")]
    base_token_price_usd: Option<String>,
    #[serde(rename = "fdv_usd")]
    fdv_usd: Option<String>,
    #[serde(rename = "reserve_in_usd")]
    reserve_in_usd: Option<String>,
    #[serde(rename = "price_change_percentage")]
    price_change: Option<GeckoPriceChange>,
    volume_usd: Option<GeckoVolume>,
    transactions: Option<GeckoTransactions>,
}

#[derive(Debug, Deserialize)]
struct GeckoPriceChange {
    h1: Option<String>,
    h6: Option<String>,
    h24: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GeckoVolume {
    h1: Option<String>,
    h6: Option<String>,
    h24: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GeckoTransactions {
    h1: Option<GeckoTxCount>,
    h24: Option<GeckoTxCount>,
}

#[derive(Debug, Deserialize)]
struct GeckoTxCount {
    buys: Option<u32>,
    sells: Option<u32>,
}

// NAD.FUN v3 ABI
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
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 DATA STRUCTURES
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Position {
    token_address: String,
    token_name: String,
    amount_mon: f64,
    entry_price_mon: f64,
    highest_value_mon: f64,
    timestamp: u64,
    moonbag_secured: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PortfolioState {
    total_value_mon: f64,
    positions: Vec<PositionAnalysis>,
    market_sentiment: String,
    recommendations: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PositionAnalysis {
    token_name: String,
    current_value_mon: f64,
    pnl_pct: f64,
    drop_from_ath: f64,
    ai_score: i32,
    ai_action: String,
    ai_reason: String,
}

#[derive(Debug, Deserialize)]
struct DeepSeekResponse {
    choices: Vec<DeepSeekChoice>,
}

#[derive(Debug, Deserialize)]
struct DeepSeekChoice {
    message: DeepSeekMessage,
}

#[derive(Debug, Deserialize)]
struct DeepSeekMessage {
    content: String,
}

#[derive(Debug, Deserialize)]
struct GeminiResponse {
    candidates: Option<Vec<GeminiCandidate>>,
}

#[derive(Debug, Deserialize)]
struct GeminiCandidate {
    content: GeminiContent,
}

#[derive(Debug, Deserialize)]
struct GeminiContent {
    parts: Vec<GeminiPart>,
}

#[derive(Debug, Deserialize)]
struct GeminiPart {
    text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct AIStrategyConfig {
    enabled: bool,
    model: String, // "deepseek" or "gemini"
    check_interval_sec: u64,
    auto_adjust_stops: bool,
    risk_level: String, // "conservative", "moderate", "aggressive"
}

impl Default for AIStrategyConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            model: "gemini".to_string(),
            check_interval_sec: 300, // 5 minutes
            auto_adjust_stops: false,
            risk_level: "moderate".to_string(),
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🔧 HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

fn log(msg: &str) {
    println!("[{}] {}", Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
}

fn wei_to_mon(wei: U256) -> f64 {
    wei.to::<u128>() as f64 / 1e18
}

// Trade history entry
#[derive(Debug, Clone, Serialize, Deserialize)]
struct TradeHistoryEntry {
    token_address: String,
    token_name: String,
    entry_mon: f64,
    exit_mon: f64,
    pnl: f64,
    pnl_pct: f64,
    reason: String,
    timestamp: u64,
}

fn save_trade_to_history(entry: &TradeHistoryEntry) {
    let history_path = "trades_history.json";

    // Load existing history
    let mut history: Vec<TradeHistoryEntry> = fs::read_to_string(history_path)
        .ok()
        .and_then(|c| serde_json::from_str(&c).ok())
        .unwrap_or_default();

    // Add new trade
    history.push(entry.clone());

    // Save back
    if let Ok(json) = serde_json::to_string_pretty(&history) {
        let _ = fs::write(history_path, json);
        log(&format!(
            "📜 Trade saved to history: {} {:.1} MON -> {:.1} MON",
            entry.token_name, entry.entry_mon, entry.exit_mon
        ));
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🧠 AI PROVIDERS
// ═══════════════════════════════════════════════════════════════════════════════

async fn call_gemini(client: &Client, api_key: &str, prompt: &str) -> Option<String> {
    let url = format!(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={}",
        api_key
    );

    let body = serde_json::json!({
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024
        }
    });

    match client.post(&url).json(&body).send().await {
        Ok(resp) => {
            if let Ok(data) = resp.json::<GeminiResponse>().await {
                if let Some(candidates) = data.candidates {
                    if let Some(first) = candidates.first() {
                        if let Some(part) = first.content.parts.first() {
                            return Some(part.text.clone());
                        }
                    }
                }
            }
            None
        }
        Err(_) => None,
    }
}

async fn call_deepseek(client: &Client, api_key: &str, prompt: &str) -> Option<String> {
    let url = "https://api.deepseek.com/v1/chat/completions";

    let body = serde_json::json!({
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": "You are an expert crypto trading strategist. Analyze positions and provide actionable advice. Be concise and specific."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
        "max_tokens": 1024
    });

    match client
        .post(url)
        .header("Authorization", format!("Bearer {}", api_key))
        .json(&body)
        .send()
        .await
    {
        Ok(resp) => {
            if let Ok(data) = resp.json::<DeepSeekResponse>().await {
                if let Some(choice) = data.choices.first() {
                    return Some(choice.message.content.clone());
                }
            }
            None
        }
        Err(_) => None,
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 📊 PORTFOLIO ANALYSIS
// ═══════════════════════════════════════════════════════════════════════════════

fn build_analysis_prompt(
    positions: &[PositionAnalysis],
    balance_mon: f64,
    market_context: &str,
) -> String {
    let mut prompt = format!(
        r#"Analyze this crypto memecoin portfolio on Monad blockchain:

WALLET BALANCE: {:.2} MON (~${:.2} at $0.70/MON)

{}

MY POSITIONS:
"#,
        balance_mon,
        balance_mon * 0.70,
        market_context
    );

    for pos in positions {
        prompt.push_str(&format!(
            "- {} | Value: {:.4} MON | PnL: {:+.1}% | Drop from ATH: {:.1}%\n",
            pos.token_name, pos.current_value_mon, pos.pnl_pct, pos.drop_from_ath
        ));
    }

    prompt.push_str(
        r#"
CURRENT STRATEGY:
- Hard Stop Loss: -15% (NIE SPRZEDAWAJ wcześniej!)
- Trailing Stop: 20% drop from ATH (activates after +30% profit)
- Take Profit: 50% → sell 50%, keep rest
- Full Exit: 100%+ profit

🛡️ ZASADY CIERPLIWOŚCI (PATIENCE RULES):
1. NIE SPRZEDAWAJ pozycji ze stratą mniejszą niż -15%!
2. -1%, -2%, -5% to NORMALNE wahania - CZEKAJ!
3. "Dead token" lub "no data" to NIE powód do sprzedaży!
4. Minimum trzymanie: 30 minut przed sell
5. Lepiej stracić -15% RAZ niż -2% DZIESIĘĆ razy!
6. TYLKO sell gdy: strata > -15% LUB zysk > +30%

ANALYZE AND PROVIDE:
1. MARKET SENTIMENT (bullish/neutral/bearish) based on trending tokens
2. TOP 3 RECOMMENDATIONS (specific actions for MY positions)
3. RISK ASSESSMENT (low/medium/high)
4. Should any stop-loss be adjusted? Why?
5. Should any positions be sold IMMEDIATELY? (ONLY if loss > -15% or profit > +30%)
6. ENTRY SUGGESTIONS: Based on market trends, any tokens worth buying? (optional)

Be specific. Use token names. Format as JSON:
{
  "sentiment": "...",
  "risk": "...",
  "recommendations": ["action1", "action2", "action3"],
  "stop_loss_adjustments": [{"token": "...", "new_stop": -XX, "reason": "..."}],
  "sell_immediately": [{"token": "...", "percent": 100, "reason": "..."}],
  "entry_suggestions": [{"token": "...", "reason": "...", "size_mon": X}
}
"#,
    );

    prompt
}

fn parse_ai_response(response: &str) -> Option<AIAnalysis> {
    // Try to extract JSON from response
    let json_start = response.find('{')?;
    let json_end = response.rfind('}')?;
    let json_str = &response[json_start..=json_end];

    serde_json::from_str(json_str).ok()
}

#[derive(Debug, Deserialize)]
struct AIAnalysis {
    sentiment: String,
    risk: String,
    recommendations: Vec<String>,
    stop_loss_adjustments: Option<Vec<StopLossAdjustment>>,
    sell_immediately: Option<Vec<SellOrder>>,
    entry_suggestions: Option<Vec<EntrySuggestion>>,
}

#[derive(Debug, Deserialize)]
struct EntrySuggestion {
    token: String,
    reason: String,
    size_mon: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct StopLossAdjustment {
    token: String,
    new_stop: f64,
    reason: String,
}

#[derive(Debug, Deserialize, Clone)]
struct SellOrder {
    token: String,
    percent: f64,
    reason: String,
}

// Dynamic stop-loss storage
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct DynamicStops {
    stops: HashMap<String, f64>,
}

impl DynamicStops {
    fn load() -> Self {
        fs::read_to_string("dynamic_stops.json")
            .ok()
            .and_then(|c| serde_json::from_str(&c).ok())
            .unwrap_or_default()
    }

    fn save(&self) {
        if let Ok(json) = serde_json::to_string_pretty(self) {
            let _ = fs::write("dynamic_stops.json", json);
        }
    }

    fn set(&mut self, token: &str, stop: f64) {
        self.stops.insert(token.to_lowercase(), stop);
        self.save();
    }

    fn get(&self, token: &str) -> Option<f64> {
        self.stops.get(&token.to_lowercase()).copied()
    }
}

// ═══════════════════════════════════════════════════════════════════════════════
// 🦎 GECKOTERMINAL MARKET DATA
// ═══════════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone)]
struct MarketTrend {
    name: String,
    price_usd: f64,
    change_1h: f64,
    change_24h: f64,
    volume_24h: f64,
    liquidity_usd: f64,
    buys_24h: u32,
    sells_24h: u32,
}

async fn fetch_trending_pools(client: &Client) -> Vec<MarketTrend> {
    let url = "https://api.geckoterminal.com/api/v2/networks/monad/trending_pools";

    match client
        .get(url)
        .header("Accept", "application/json")
        .timeout(Duration::from_secs(10))
        .send()
        .await
    {
        Ok(resp) => {
            if let Ok(data) = resp.json::<GeckoPoolsResponse>().await {
                return data
                    .data
                    .iter()
                    .take(10)
                    .filter_map(|pool| {
                        let attrs = &pool.attributes;
                        let price = attrs
                            .base_token_price_usd
                            .as_ref()
                            .and_then(|p| p.parse::<f64>().ok())
                            .unwrap_or(0.0);
                        let change_1h = attrs
                            .price_change
                            .as_ref()
                            .and_then(|p| p.h1.as_ref())
                            .and_then(|v| v.parse::<f64>().ok())
                            .unwrap_or(0.0);
                        let change_24h = attrs
                            .price_change
                            .as_ref()
                            .and_then(|p| p.h24.as_ref())
                            .and_then(|v| v.parse::<f64>().ok())
                            .unwrap_or(0.0);
                        let volume_24h = attrs
                            .volume_usd
                            .as_ref()
                            .and_then(|v| v.h24.as_ref())
                            .and_then(|v| v.parse::<f64>().ok())
                            .unwrap_or(0.0);
                        let liquidity = attrs
                            .reserve_in_usd
                            .as_ref()
                            .and_then(|v| v.parse::<f64>().ok())
                            .unwrap_or(0.0);
                        let buys = attrs
                            .transactions
                            .as_ref()
                            .and_then(|t| t.h24.as_ref())
                            .and_then(|t| t.buys)
                            .unwrap_or(0);
                        let sells = attrs
                            .transactions
                            .as_ref()
                            .and_then(|t| t.h24.as_ref())
                            .and_then(|t| t.sells)
                            .unwrap_or(0);

                        // Filter out stablecoins and wrapped tokens
                        let name = &attrs.name;
                        if name.contains("USDC")
                            || name.contains("USDT")
                            || name.contains("WMON")
                            || name.contains("WETH")
                        {
                            return None;
                        }

                        Some(MarketTrend {
                            name: name.clone(),
                            price_usd: price,
                            change_1h,
                            change_24h,
                            volume_24h,
                            liquidity_usd: liquidity,
                            buys_24h: buys,
                            sells_24h: sells,
                        })
                    })
                    .collect();
            }
        }
        Err(e) => log(&format!("⚠️ GeckoTerminal error: {:?}", e)),
    }
    Vec::new()
}

fn build_market_context(trends: &[MarketTrend]) -> String {
    if trends.is_empty() {
        return "No market data available.".to_string();
    }

    let mut context = String::from("MONAD MARKET TRENDS (Top Trending Tokens):\n");

    // Top gainers
    let mut sorted = trends.to_vec();
    sorted.sort_by(|a, b| b.change_1h.partial_cmp(&a.change_1h).unwrap());

    context.push_str("\n🔥 TOP GAINERS (1h):\n");
    for t in sorted.iter().take(3) {
        context.push_str(&format!(
            "  - {} | +{:.1}% 1h | Vol ${:.0}k | Liq ${:.0}k\n",
            t.name,
            t.change_1h,
            t.volume_24h / 1000.0,
            t.liquidity_usd / 1000.0
        ));
    }

    // Top losers
    sorted.sort_by(|a, b| a.change_1h.partial_cmp(&b.change_1h).unwrap());
    context.push_str("\n📉 TOP LOSERS (1h):\n");
    for t in sorted.iter().take(3) {
        if t.change_1h < 0.0 {
            context.push_str(&format!(
                "  - {} | {:.1}% 1h | Vol ${:.0}k\n",
                t.name,
                t.change_1h,
                t.volume_24h / 1000.0
            ));
        }
    }

    // Volume leaders
    sorted.sort_by(|a, b| b.volume_24h.partial_cmp(&a.volume_24h).unwrap());
    context.push_str("\n📊 VOLUME LEADERS (24h):\n");
    for t in sorted.iter().take(3) {
        let buy_ratio = if t.sells_24h > 0 {
            t.buys_24h as f64 / t.sells_24h as f64
        } else {
            0.0
        };
        context.push_str(&format!(
            "  - {} | Vol ${:.0}k | {:.1}x buy ratio\n",
            t.name,
            t.volume_24h / 1000.0,
            buy_ratio
        ));
    }

    context
}

// ═══════════════════════════════════════════════════════════════════════════════
// 💰 AUTO-SELL EXECUTION - inline macro for cleaner code
// ═══════════════════════════════════════════════════════════════════════════════

// Sell logic moved inline to main() to avoid generic type issues

// ═══════════════════════════════════════════════════════════════════════════════
// 🚀 MAIN
// ═══════════════════════════════════════════════════════════════════════════════

const ROUTER_ADDRESS: &str = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenv::dotenv().ok();

    log("═══════════════════════════════════════════════════════════════");
    log("🧠 AI STRATEGY AGENT v2.0 - AUTO EXECUTE MODE");
    log("═══════════════════════════════════════════════════════════════");

    // Load config
    let gemini_api_key = env::var("GEMINI_API_KEY").unwrap_or_default();
    let deepseek_api_key = env::var("DEEPSEEK_API_KEY").unwrap_or_default();
    let rpc_url = env::var("MONAD_RPC_URL")
        .unwrap_or_else(|_| "https://monad-mainnet.g.alchemy.com/v2/demo".to_string());
    let private_key = env::var("PRIVATE_KEY")?;
    let auto_execute = env::var("AI_AUTO_EXECUTE").unwrap_or_default() == "true";

    // Determine which AI to use - DeepSeek is preferred (cheaper, better reasoning)
    let use_deepseek = !deepseek_api_key.is_empty();
    let use_gemini = !gemini_api_key.is_empty() && !use_deepseek;

    if !use_gemini && !use_deepseek {
        log("❌ No AI API key found! Set DEEPSEEK_API_KEY or GEMINI_API_KEY");
        return Ok(());
    }

    let ai_provider = if use_deepseek { "DeepSeek" } else { "Gemini" };
    log(&format!("🤖 Using {} AI", ai_provider));
    log(&format!(
        "⚡ Auto-Execute: {}",
        if auto_execute { "ENABLED" } else { "DISABLED" }
    ));

    // Setup provider with wallet for signing transactions
    let rpc_url: url::Url = rpc_url.parse()?;
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer);

    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_http(rpc_url);

    let my_address = provider.wallet().default_signer().address();
    let router_address = Address::from_str(ROUTER_ADDRESS)?;

    // Load dynamic stops
    let mut dynamic_stops = DynamicStops::load();

    log(&format!("👤 Wallet: {:?}", my_address));

    let http_client = Client::builder().timeout(Duration::from_secs(30)).build()?;

    let check_interval = Duration::from_secs(300); // 5 minutes

    loop {
        log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
        log("🔍 Analyzing portfolio...");

        // Load positions
        let positions: HashMap<String, Position> =
            if let Ok(content) = fs::read_to_string("positions.json") {
                serde_json::from_str(&content).unwrap_or_default()
            } else {
                HashMap::new()
            };

        if positions.is_empty() {
            log("📭 No positions to analyze");
            sleep(check_interval).await;
            continue;
        }

        // Get wallet balance
        let balance = provider.get_balance(my_address).await.unwrap_or(U256::ZERO);
        let balance_mon = wei_to_mon(balance);

        log(&format!("💰 Wallet: {:.2} MON", balance_mon));
        log(&format!("📊 Positions: {}", positions.len()));

        // Fetch market trends from GeckoTerminal
        log("🦎 Fetching market trends from GeckoTerminal...");
        let market_trends = fetch_trending_pools(&http_client).await;
        let market_context = build_market_context(&market_trends);
        log(&format!("📈 Got {} trending tokens", market_trends.len()));

        // Build position analysis (simplified - would need Lens calls for real values)
        let mut position_analyses: Vec<PositionAnalysis> = Vec::new();

        for (addr, pos) in &positions {
            // For demo, using stored values
            let current_value = pos.highest_value_mon.max(pos.amount_mon * 0.9);
            let pnl_pct = ((current_value - pos.amount_mon) / pos.amount_mon) * 100.0;
            let drop_from_ath = if pos.highest_value_mon > 0.0 {
                ((pos.highest_value_mon - current_value) / pos.highest_value_mon) * 100.0
            } else {
                0.0
            };

            position_analyses.push(PositionAnalysis {
                token_name: pos.token_name.clone(),
                current_value_mon: current_value,
                pnl_pct,
                drop_from_ath,
                ai_score: 0,
                ai_action: String::new(),
                ai_reason: String::new(),
            });
        }

        // Build prompt
        let prompt = build_analysis_prompt(&position_analyses, balance_mon, &market_context);

        // Call AI - DeepSeek preferred
        log("🧠 Consulting AI...");
        let ai_response = if use_deepseek {
            call_deepseek(&http_client, &deepseek_api_key, &prompt).await
        } else {
            call_gemini(&http_client, &gemini_api_key, &prompt).await
        };

        if let Some(response) = ai_response {
            log("✅ AI Response received!");

            // Parse and display
            if let Some(analysis) = parse_ai_response(&response) {
                log("═══════════════════════════════════════════════════════════════");
                log(&format!(
                    "📊 MARKET SENTIMENT: {}",
                    analysis.sentiment.to_uppercase()
                ));
                log(&format!("⚠️ RISK LEVEL: {}", analysis.risk.to_uppercase()));
                log("═══════════════════════════════════════════════════════════════");

                log("🎯 RECOMMENDATIONS:");
                for (i, rec) in analysis.recommendations.iter().enumerate() {
                    log(&format!("   {}. {}", i + 1, rec));
                }

                // Process stop-loss adjustments
                if let Some(ref adjustments) = analysis.stop_loss_adjustments {
                    if !adjustments.is_empty() {
                        log("📉 STOP-LOSS ADJUSTMENTS:");
                        for adj in adjustments {
                            log(&format!(
                                "   • {} → {}% ({})",
                                adj.token, adj.new_stop, adj.reason
                            ));
                            // Save to dynamic stops
                            dynamic_stops.set(&adj.token, adj.new_stop);
                        }
                        log("   ✅ Dynamic stops saved to dynamic_stops.json");
                    }
                }

                // Process immediate sells
                if let Some(ref sells) = analysis.sell_immediately {
                    if !sells.is_empty() {
                        log("🚨 URGENT SELLS:");
                        for sell in sells {
                            log(&format!(
                                "   • {} ({}%) - {}",
                                sell.token, sell.percent, sell.reason
                            ));
                        }

                        if auto_execute {
                            log("⚡ AUTO-EXECUTING SELLS...");
                            let mut sold_tokens: Vec<String> = Vec::new();

                            for sell in sells {
                                // Find token address by name
                                if let Some((addr, pos)) = positions.iter().find(|(_, p)| {
                                    p.token_name
                                        .to_lowercase()
                                        .contains(&sell.token.to_lowercase())
                                }) {
                                    // 🛡️ PATIENCE CHECK - don't sell small losses!
                                    let pnl_pct = if pos.entry_price_mon > 0.0 {
                                        ((pos.highest_value_mon - pos.entry_price_mon)
                                            / pos.entry_price_mon)
                                            * 100.0
                                    } else {
                                        0.0
                                    };

                                    // Skip if loss is between 0% and -15% (normal fluctuation)
                                    if pnl_pct < 0.0 && pnl_pct > -15.0 {
                                        log(&format!("   ⏳ SKIPPING {} - loss only {:.1}% (< -15% threshold)", sell.token, pnl_pct));
                                        log("      → PATIENCE: czekamy na odbicie lub większą stratę");
                                        continue;
                                    }

                                    // Skip if profit is less than +30% (let it run)
                                    if pnl_pct > 0.0 && pnl_pct < 30.0 {
                                        log(&format!("   ⏳ SKIPPING {} - profit only +{:.1}% (< +30% threshold)", sell.token, pnl_pct));
                                        log("      → PATIENCE: czekamy na większy zysk");
                                        continue;
                                    }

                                    log(&format!(
                                        "   🔄 Selling {} ({})... PnL: {:.1}%",
                                        sell.token, addr, pnl_pct
                                    ));

                                    if let Ok(token_addr) = Address::from_str(addr) {
                                        // === INLINE SELL LOGIC ===
                                        let token_name = &pos.token_name;

                                        // Get balance
                                        let balance_call = balanceOfCall {
                                            account: my_address,
                                        };
                                        let balance_tx = TransactionRequest::default()
                                            .to(token_addr)
                                            .input(balance_call.abi_encode().into());

                                        let balance = match provider.call(&balance_tx).await {
                                            Ok(result) => U256::from_be_slice(&result),
                                            Err(e) => {
                                                log(&format!(
                                                    "   ❌ Balance check failed: {:?}",
                                                    e
                                                ));
                                                continue;
                                            }
                                        };

                                        if balance.is_zero() {
                                            log(&format!("   ⚠️ {} - No balance", token_name));
                                            continue;
                                        }

                                        let sell_amount = if sell.percent >= 100.0 {
                                            balance
                                        } else {
                                            balance * U256::from((sell.percent * 100.0) as u64)
                                                / U256::from(10000u64)
                                        };

                                        log(&format!(
                                            "   💰 {} balance: {} tokens, selling {}%",
                                            token_name,
                                            balance.to::<u128>() as f64 / 1e18,
                                            sell.percent
                                        ));

                                        // Approve
                                        let approve_call = approveCall {
                                            spender: router_address,
                                            amount: sell_amount,
                                        };
                                        let approve_tx = TransactionRequest::default()
                                            .to(token_addr)
                                            .input(approve_call.abi_encode().into())
                                            .gas_limit(100_000);

                                        match provider.send_transaction(approve_tx).await {
                                            Ok(pending) => {
                                                if let Err(e) = pending.get_receipt().await {
                                                    log(&format!(
                                                        "   ⚠️ Approve receipt error: {:?}",
                                                        e
                                                    ));
                                                }
                                            }
                                            Err(e) => {
                                                log(&format!("   ❌ Approve failed: {:?}", e));
                                                continue;
                                            }
                                        }
                                        log(&format!("   🔐 {} approved", token_name));

                                        // Sell
                                        let deadline = U256::from(
                                            std::time::SystemTime::now()
                                                .duration_since(std::time::UNIX_EPOCH)
                                                .unwrap()
                                                .as_secs()
                                                + 300,
                                        );

                                        let sell_params = SellParams {
                                            amountIn: sell_amount,
                                            amountOutMin: U256::from(1),
                                            token: token_addr,
                                            to: my_address,
                                            deadline,
                                        };
                                        let sell_call = sellCall {
                                            params: sell_params,
                                        };

                                        let sell_tx = TransactionRequest::default()
                                            .to(router_address)
                                            .input(sell_call.abi_encode().into())
                                            .gas_limit(500_000)
                                            .max_priority_fee_per_gas(100_000_000_000);

                                        match provider.send_transaction(sell_tx).await {
                                            Ok(pending) => {
                                                match pending.get_receipt().await {
                                                    Ok(receipt) => {
                                                        log(&format!(
                                                            "   ✅ {} SOLD! TX: {:?}",
                                                            token_name, receipt.transaction_hash
                                                        ));

                                                        // Save trade to history
                                                        let entry_mon = pos.amount_mon;
                                                        // Estimate exit value (we sold at current value)
                                                        let exit_mon = pos
                                                            .highest_value_mon
                                                            .max(entry_mon * 0.9)
                                                            * (sell.percent / 100.0);
                                                        let pnl = exit_mon - entry_mon;
                                                        let pnl_pct = if entry_mon > 0.0 {
                                                            (pnl / entry_mon) * 100.0
                                                        } else {
                                                            0.0
                                                        };

                                                        let trade_entry = TradeHistoryEntry {
                                                            token_address: addr.clone(),
                                                            token_name: token_name.clone(),
                                                            entry_mon,
                                                            exit_mon,
                                                            pnl,
                                                            pnl_pct,
                                                            reason: format!("AI: {}", sell.reason),
                                                            timestamp: std::time::SystemTime::now()
                                                                .duration_since(
                                                                    std::time::UNIX_EPOCH,
                                                                )
                                                                .unwrap()
                                                                .as_secs(),
                                                        };
                                                        save_trade_to_history(&trade_entry);

                                                        if sell.percent >= 100.0 {
                                                            sold_tokens.push(addr.clone());
                                                        }
                                                    }
                                                    Err(e) => log(&format!(
                                                        "   ❌ {} sell failed: {:?}",
                                                        token_name, e
                                                    )),
                                                }
                                            }
                                            Err(e) => log(&format!(
                                                "   ❌ {} TX error: {:?}",
                                                token_name, e
                                            )),
                                        }
                                        // === END INLINE SELL ===
                                    }
                                } else {
                                    log(&format!(
                                        "   ⚠️ Token {} not found in positions",
                                        sell.token
                                    ));
                                }
                            }

                            // Remove sold positions from positions.json
                            if !sold_tokens.is_empty() {
                                let mut positions_mut = positions.clone();
                                for addr in &sold_tokens {
                                    positions_mut.remove(addr);
                                }
                                if let Ok(json) = serde_json::to_string_pretty(&positions_mut) {
                                    let _ = fs::write("positions.json", json);
                                    log(&format!(
                                        "   📝 Removed {} sold positions from tracking",
                                        sold_tokens.len()
                                    ));
                                }
                            }
                        } else {
                            log("   ⏸️ Auto-execute disabled. Set AI_AUTO_EXECUTE=true to enable.");
                        }
                    }
                }

                log("═══════════════════════════════════════════════════════════════");
            } else {
                // Raw response if JSON parsing fails
                log("📝 AI Analysis:");
                for line in response.lines().take(20) {
                    log(&format!("   {}", line));
                }
            }
        } else {
            log("⚠️ AI request failed, will retry next cycle");
        }

        log(&format!(
            "⏳ Next analysis in {} seconds...",
            check_interval.as_secs()
        ));
        sleep(check_interval).await;
    }
}
