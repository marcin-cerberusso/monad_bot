use alloy::{
    network::EthereumWallet,
    providers::{Provider, ProviderBuilder, WalletProvider},
    signers::local::PrivateKeySigner,
    sol,
    primitives::{U256, Address},
    rpc::types::TransactionRequest,
    sol_types::SolCall,
};
use std::{env, str::FromStr, time::{Duration, SystemTime}, collections::HashMap, fs};
use dotenv::dotenv;
use url::Url;
use anyhow::{Result, Context};
use chrono::Local;
use serde::{Serialize, Deserialize};
use reqwest::Client;

// ═══════════════════════════════════════════════════════════════════════════════
// 📉 POSITION MANAGER v4.0 - MORALIS API + ROUTER FALLBACK
// ═══════════════════════════════════════════════════════════════════════════════
// - Sprawdza cenę przez Moralis API (primary)
// - Fallback: Router getAmountsOut
// - Trailing Stop Loss
// - Hard Stop Loss  
// - Moonbag Secure
// ═══════════════════════════════════════════════════════════════════════════════

sol! {
    struct SellParams {
        address token;
        uint256 amount;
        uint256 amountOutMin;
        address to;
        uint256 deadline;
    }
    function sell(SellParams params) external;
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct Position {
    token_address: String,
    token_name: String,
    amount_mon: f64,
    #[serde(default)]
    entry_price_mon: f64,
    timestamp: u64,
    #[serde(default)]
    highest_value_mon: f64,
    #[serde(default)]
    moonbag_secured: bool,
    #[serde(default)]
    copied_from: String,
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

impl Default for Settings {
    fn default() -> Self {
        Settings {
            trailing_stop_pct: 20.0,
            hard_stop_loss_pct: -40.0,
            take_profit_pct: 100.0,
            moonbag_portion: 0.3,
        }
    }
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
        token_address,
        chain
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
    println!("[{}] {}", Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
}

fn wei_to_mon(wei: U256) -> f64 {
    let s = wei.to_string();
    s.parse::<f64>().unwrap_or(0.0) / 1e18
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
    
    log(&format!("⚙️  Trailing: {}% | Hard SL: {}% | TP: {}%", 
        settings.trailing_stop_pct, settings.hard_stop_loss_pct, settings.take_profit_pct));

    let rpc_url_str = env::var("MONAD_RPC_URL").context("Brak MONAD_RPC_URL")?;
    let private_key = env::var("PRIVATE_KEY").context("Brak PRIVATE_KEY")?;
    let router_str = env::var("ROUTER_ADDRESS").unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());
    let wmon_str = env::var("WMON_ADDRESS").unwrap_or("0x760AfE86e5de5fa0Ee542fc7B7B713e1c5425701".to_string());
    
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

    loop {
        let path = "positions.json";
        
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

                let position_list: Vec<(String, Position)> = positions.iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect();

                for (addr_str, pos) in position_list {
                    let token_address = match Address::from_str(&addr_str) {
                        Ok(addr) => addr,
                        Err(_) => continue,
                    };
                    
                    // 1. Sprawdź balance tokena
                    let balance_selector = hex::decode("70a08231").unwrap();
                    let mut balance_data = balance_selector;
                    balance_data.extend_from_slice(&[0u8; 12]);
                    balance_data.extend_from_slice(my_address.as_slice());
                    
                    let balance_req = TransactionRequest::default()
                        .to(token_address)
                        .input(balance_data.into());
                    
                    let current_balance = match provider.call(&balance_req).await {
                        Ok(bytes) => {
                            if bytes.len() >= 32 {
                                U256::from_be_slice(&bytes[..32])
                            } else {
                                U256::ZERO
                            }
                        }
                        Err(_) => U256::ZERO,
                    };

                    if current_balance == U256::ZERO {
                        log(&format!("   ⚠️ {} - Zero balance (już sprzedane?)", pos.token_name));
                        to_remove.push(addr_str.clone());
                        continue;
                    }

                    let balance_tokens = wei_to_mon(current_balance);
                    
                    // 2. Pobierz cenę - kolejność: Moralis -> DexScreener -> Router
                    let mut token_price_mon: Option<f64> = None;
                    let mut price_source = "unknown";
                    
                    // Try Moralis first
                    if !moralis_api_key.is_empty() {
                        if let Some(usd_price) = get_moralis_price(&http_client, &addr_str, &moralis_api_key, monad_chain).await {
                            // Convert USD to MON value
                            token_price_mon = Some(usd_price / 0.03 * balance_tokens);
                            price_source = "Moralis";
                        }
                    }
                    
                    // Try DexScreener as backup
                    if token_price_mon.is_none() {
                        if let Some(native_price) = get_dexscreener_price(&http_client, &addr_str).await {
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
                            ((updated_pos.highest_value_mon - current_value) / updated_pos.highest_value_mon) * 100.0
                        } else {
                            0.0
                        };
                        
                        let emoji = if pnl_pct > 100.0 { "🔥🔥" }
                                   else if pnl_pct > 50.0 { "🔥" } 
                                   else if pnl_pct > 0.0 { "📈" } 
                                   else if pnl_pct > -20.0 { "📉" } 
                                   else { "💀" };
                        
                        log(&format!("   {} {} | {:.4} MON ({:+.1}%) | ATH drop: {:.1}% [{}]", 
                            emoji, pos.token_name, current_value, pnl_pct, drop_from_ath, price_source));

                        let mut should_sell = false;
                        let mut sell_reason = String::new();
                        let mut sell_amount = current_balance;
                        
                        // 💀 HARD STOP LOSS
                        if pnl_pct <= settings.hard_stop_loss_pct {
                            should_sell = true;
                            sell_reason = format!("💀 HARD STOP LOSS ({:.1}% <= {}%)", pnl_pct, settings.hard_stop_loss_pct);
                        }
                        
                        // 📉 TRAILING STOP (aktywny po 50% zysku)
                        else if pnl_pct > 50.0 && drop_from_ath >= settings.trailing_stop_pct {
                            should_sell = true;
                            sell_reason = format!("📉 TRAILING STOP (drop {:.1}% >= {}%)", drop_from_ath, settings.trailing_stop_pct);
                        }
                        
                        // 💰 MOONBAG SECURE (sell portion at 2x)
                        else if pnl_pct >= settings.take_profit_pct && !updated_pos.moonbag_secured {
                            should_sell = true;
                            sell_reason = format!("💰 MOONBAG SECURE ({:.1}% >= {}%)", pnl_pct, settings.take_profit_pct);
                            let sell_portion = (balance_tokens * settings.moonbag_portion * 1e18) as u128;
                            sell_amount = U256::from(sell_portion);
                            
                            updated_pos.moonbag_secured = true;
                            positions.insert(addr_str.clone(), updated_pos);
                            save_needed = true;
                        }
                        
                        // 🎯 TAKE PROFIT (3x+)
                        else if pnl_pct >= 200.0 {
                            should_sell = true;
                            sell_reason = format!("🎯 TAKE PROFIT 3x+ ({:.1}%)", pnl_pct);
                        }

                        if should_sell {
                            log(&format!("   🚨 {} -> SELLING!", sell_reason));
                            
                            let now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_secs();
                            let deadline = U256::from(now + 300);
                            
                            let sell_params = SellParams {
                                token: token_address,
                                amount: sell_amount,
                                amountOutMin: U256::from(1),
                                to: my_address,
                                deadline,
                            };
                            
                            let sell_call = sellCall { params: sell_params };
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
                                            log(&format!("   ✅ SPRZEDANE! Hash: {:?}", receipt.transaction_hash));
                                            
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
                        log(&format!("   ⚠️ {} - Nie mogę pobrać ceny (czekam...)", pos.token_name));
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
