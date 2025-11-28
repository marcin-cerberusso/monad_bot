use alloy::{
    network::EthereumWallet,
    providers::{Provider, ProviderBuilder, WalletProvider},
    signers::local::PrivateKeySigner,
    sol,
    primitives::{U256, Address},
    rpc::types::BlockTransactionsKind,
    consensus::Transaction as _,
    sol_types::SolCall,
};
use std::{env, str::FromStr, thread, time::{Duration, SystemTime}, collections::HashMap, fs};
use dotenv::dotenv;
use url::Url;
use anyhow::{Result, Context};
use chrono::Local;
use serde::{Serialize, Deserialize};

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
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Blacklist {
    tokens: Vec<String>,
    creators: Vec<String>,
}

fn load_config() -> Result<Config> {
    let config_str = fs::read_to_string("config.json")
        .context("Nie można odczytać config.json")?;
    let config: Config = serde_json::from_str(&config_str)
        .context("Błąd parsowania config.json")?;
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
    
    log(&format!("📋 Loaded {} whales from config.json", config.whales.len()));
    for whale in &config.whales {
        if whale.enabled {
            log(&format!("   🐳 {} - {} ({}%)", whale.name, &whale.address[..12], whale.copy_percentage));
        }
    }
    log(&format!("⚙️  Min: {} MON | Max: {} MON | Cooldown: {}s",
        config.settings.min_buy_amount_mon,
        config.settings.max_buy_amount_mon,
        config.settings.cooldown_seconds));

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

    let router_str = env::var("ROUTER_ADDRESS").unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let router_address = Address::from_str(&router_str).context("Nieprawidłowy ROUTER_ADDRESS")?;
    log(&format!("📍 Router: {:?}", router_address));

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
                    log(&format!("🔄 Config reloaded! {} whales", new_config.whales.len()));
                }
                config = new_config;
            }
            last_config_check = SystemTime::now();
        }
        
        // Get enabled whale addresses
        let target_wallets: Vec<Address> = config.whales.iter()
            .filter(|w| w.enabled)
            .filter_map(|w| Address::from_str(&w.address).ok())
            .collect();

        match provider.get_block_number().await {
            Ok(current_block_number) => {
                if current_block_number > last_block_number {
                    match provider.get_block_by_number(current_block_number.into(), BlockTransactionsKind::Full).await {
                        Ok(Some(block)) => {
                            if let Some(txs) = block.transactions.as_transactions() {
                                for tx in txs {
                                    let tx_from = tx.from;
                                    
                                    // Check if this tx is from one of our whales
                                    if !target_wallets.contains(&tx_from) {
                                        continue;
                                    }
                                    
                                    // Find which whale this is
                                    let whale = config.whales.iter()
                                        .find(|w| Address::from_str(&w.address).ok() == Some(tx_from));
                                    
                                    let whale_name = whale.map(|w| w.name.clone()).unwrap_or_else(|| format!("{:?}", tx_from));
                                    let copy_pct = whale.map(|w| w.copy_percentage).unwrap_or(100.0);
                                    
                                    if let Some(to_address) = tx.to() {
                                        if to_address == router_address {
                                            let value_mon = wei_to_mon(tx.inner.value());
                                            
                                            // Try to decode BUY
                                            if let Ok(decoded_buy) = buyCall::abi_decode(tx.inner.input(), true) {
                                                let params = decoded_buy.params;
                                                let token_str = format!("{:?}", params.token);
                                                
                                                // Check minimum value
                                                if value_mon < config.settings.min_target_value_mon {
                                                    continue;
                                                }
                                                
                                                // Check blacklist
                                                if config.blacklist.tokens.iter().any(|t| token_str.to_lowercase().contains(&t.to_lowercase())) {
                                                    log(&format!("🚫 Token {} is blacklisted", &token_str[..12]));
                                                    continue;
                                                }
                                                
                                                // Cooldown check
                                                let now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_secs();
                                                if let Some(&last_time) = last_buy_time.get(&token_str) {
                                                    if now - last_time < config.settings.cooldown_seconds {
                                                        log(&format!("⏳ Cooldown for {} ({}s)", &token_str[..12], config.settings.cooldown_seconds));
                                                        continue;
                                                    }
                                                }
                                                
                                                log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                                log(&format!("🚨 WHALE BUY DETECTED! 🐳 {}", whale_name));
                                                log(&format!("   💎 Token: {}", &token_str[..16]));
                                                log(&format!("   💰 Whale spent: {:.2} MON", value_mon));
                                                
                                                // Calculate our buy amount
                                                let calculated_amount = (value_mon * copy_pct / 100.0)
                                                    .min(config.settings.max_buy_amount_mon)
                                                    .max(config.settings.min_buy_amount_mon);
                                                
                                                let emoji = if calculated_amount >= config.settings.max_buy_amount_mon * 0.8 { "🐳" } 
                                                           else if calculated_amount >= config.settings.max_buy_amount_mon * 0.5 { "🦈" } 
                                                           else { "🐟" };
                                                
                                                log(&format!("   {} Copying: {:.2} MON ({}%)", emoji, calculated_amount, copy_pct));
                                                
                                                let buy_value_wei = U256::from((calculated_amount * 1e18) as u128);
                                                let deadline = U256::from(now + 300);
                                                
                                                let buy_params = BuyParams {
                                                    amountOutMin: U256::from(1),
                                                    token: params.token,
                                                    to: my_address,
                                                    deadline,
                                                };
                                                
                                                let buy_call = buyCall { params: buy_params };
                                                let calldata = buy_call.abi_encode();
                                                
                                                let tx_request = alloy::rpc::types::TransactionRequest::default()
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
                                                                log(&format!("   ✅ BOUGHT! Hash: {:?}", receipt.transaction_hash));
                                                                
                                                                // Save position
                                                                positions.insert(token_str.clone(), Position {
                                                                    token_address: token_str.clone(),
                                                                    token_name: format!("Copy_{}", &token_str[..8]),
                                                                    amount_mon: calculated_amount,
                                                                    entry_price_mon: calculated_amount,
                                                                    peak_price_mon: calculated_amount,
                                                                    timestamp: now,
                                                                    trailing_active: false,
                                                                    partial_sold: false,
                                                                    copied_from: whale_name.clone(),
                                                                });
                                                                last_buy_time.insert(token_str.clone(), now);
                                                                
                                                                let _ = save_positions(&positions);
                                                                log(&format!("   📊 Positions: {}", positions.len()));
                                                            }
                                                            Err(e) => log(&format!("   ❌ Receipt error: {:?}", e)),
                                                        }
                                                    }
                                                    Err(e) => log(&format!("   ❌ TX error: {:?}", e)),
                                                }
                                            }
                                            // Try to decode SELL (copy sells too!)
                                            else if let Ok(decoded_sell) = sellCall::abi_decode(tx.inner.input(), true) {
                                                let params = decoded_sell.params;
                                                let token_str = format!("{:?}", params.token);
                                                
                                                if let Some(position) = positions.get(&token_str) {
                                                    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
                                                    log(&format!("🚨 WHALE SELL DETECTED! 🐳 {}", whale_name));
                                                    log(&format!("   💎 Token: {}", &token_str[..16]));
                                                    log("   ⚡ Auto-selling our position!");
                                                    
                                                    let now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_secs();
                                                    let deadline = U256::from(now + 300);
                                                    
                                                    let sell_params = SellParams {
                                                        token: params.token,
                                                        amount: params.amount,
                                                        amountOutMin: U256::from(1),
                                                        to: my_address,
                                                        deadline,
                                                    };
                                                    
                                                    let sell_call = sellCall { params: sell_params };
                                                    let calldata = sell_call.abi_encode();
                                                    
                                                    let tx_request = alloy::rpc::types::TransactionRequest::default()
                                                        .to(router_address)
                                                        .input(calldata.into())
                                                        .gas_limit(8_000_000)
                                                        .max_priority_fee_per_gas(500_000_000_000);
                                                    
                                                    match provider.send_transaction(tx_request).await {
                                                        Ok(pending_tx) => {
                                                            log("   ⏳ Selling...");
                                                            match pending_tx.get_receipt().await {
                                                                Ok(receipt) => {
                                                                    log(&format!("   ✅ SOLD! Hash: {:?}", receipt.transaction_hash));
                                                                    positions.remove(&token_str);
                                                                    let _ = save_positions(&positions);
                                                                }
                                                                Err(e) => log(&format!("   ❌ Error: {:?}", e)),
                                                            }
                                                        }
                                                        Err(e) => log(&format!("   ❌ TX error: {:?}", e)),
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            last_block_number = current_block_number;
                        }
                        Ok(None) => {},
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
