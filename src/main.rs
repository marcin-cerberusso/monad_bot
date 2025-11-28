use alloy::{
    network::EthereumWallet,
    providers::{Provider, ProviderBuilder, WalletProvider},
    signers::local::PrivateKeySigner,
    sol,
    primitives::{U256, Address},
};
use std::{env, str::FromStr, thread, time::Duration};
use rand::Rng;
use dotenv::dotenv;
use url::Url;
use anyhow::{Result, Context};
use chrono::Local;

// Helper function for timestamped logging
fn log(msg: &str) {
    println!("[{}] {}", Local::now().format("%Y-%m-%d %H:%M:%S"), msg);
}

// Definiujemy interfejs kontraktu WMON (Wrapped Monad)
sol! {
    #[sol(rpc)]
    interface IWMON {
        function deposit() external payable; // Wpłać MON -> Dostaniesz WMON
        function withdraw(uint256 amount) external; // Wypłać WMON -> Dostaniesz MON
        function balanceOf(address owner) external view returns (uint256); // Sprawdź balans WMON
    }
}

// Konfiguracja (ładowana z .env)
struct Config {
    wmon_address: Address,
    cycles: u32,
    wrap_amount_wei: U256,
    sleep_after_wrap_min: u64,
    sleep_after_wrap_max: u64,
    sleep_cycles_min: u64,
    sleep_cycles_max: u64,
}

impl Config {
    fn from_env() -> Result<Self> {
        let wmon_str = env::var("WMON_ADDRESS").unwrap_or("0x760AfE86e5de5fa0Ee542fc7B7B713e1c5425701".to_string());
        let wmon_address = Address::from_str(&wmon_str).context("Nieprawidłowy adres WMON_ADDRESS")?;
        
        let cycles = env::var("FARMING_CYCLES").unwrap_or("50".to_string()).parse().unwrap_or(50);
        
        let wrap_amount_mon: f64 = env::var("FARMING_WRAP_AMOUNT_MON").unwrap_or("0.0001".to_string()).parse().unwrap_or(0.0001);
        let wrap_amount_wei = U256::from((wrap_amount_mon * 1e18) as u128);

        let sleep_after_wrap_min = env::var("FARMING_SLEEP_AFTER_WRAP_MIN").unwrap_or("15".to_string()).parse().unwrap_or(15);
        let sleep_after_wrap_max = env::var("FARMING_SLEEP_AFTER_WRAP_MAX").unwrap_or("45".to_string()).parse().unwrap_or(45);
        let sleep_cycles_min = env::var("FARMING_SLEEP_BETWEEN_CYCLES_MIN").unwrap_or("30".to_string()).parse().unwrap_or(30);
        let sleep_cycles_max = env::var("FARMING_SLEEP_BETWEEN_CYCLES_MAX").unwrap_or("90".to_string()).parse().unwrap_or(90);

        Ok(Self {
            wmon_address,
            cycles,
            wrap_amount_wei,
            sleep_after_wrap_min,
            sleep_after_wrap_max,
            sleep_cycles_min,
            sleep_cycles_max,
        })
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    // 1. Ładowanie konfiguracji
    dotenv().ok();
    log("🚀 Odpalam Monad Farmera (v2.2 - Stable)...");

    let config = Config::from_env()?;
    log(&format!("⚙️  Konfiguracja: WMON={:?}, Cykli={}, Wrap={} MON", config.wmon_address, config.cycles, (config.wrap_amount_wei.to_string().parse::<f64>().unwrap_or(0.0) / 1e18)));

    let rpc_url_str = env::var("MONAD_RPC_URL").context("Brak MONAD_RPC_URL w pliku .env")?;
    let private_key = env::var("PRIVATE_KEY").context("Brak PRIVATE_KEY w pliku .env")?;

    let rpc_url = Url::parse(&rpc_url_str)?;
    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer);

    // 2. Podłączenie do sieci
    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_http(rpc_url);

    let my_address = provider.wallet().default_signer().address();
    log(&format!("👤 Zalogowano jako: {:?}", my_address));
    
    // Check Chain ID to be sure
    let chain_id = provider.get_chain_id().await?;
    log(&format!("🔗 Chain ID: {}", chain_id));

    // Initial balance check
    match provider.get_balance(my_address).await {
        Ok(balance) => {
            let balance_eth = balance.to_string().parse::<f64>().unwrap_or(0.0) / 1e18;
            log(&format!("💼 Balans: {} MON", balance_eth));
        }
        Err(e) => log(&format!("❌ Błąd balansu: {:?}", e)),
    }

    let wmon_contract = IWMON::new(config.wmon_address, provider.clone());

    log("");
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    log("🌾 Rozpoczynam pętlę farmingową...");
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    
    // 3. Pętla Farmingowa
    for i in 1..=config.cycles {
        log("");
        log(&format!("🔄 ===== Cykl {}/{} =====", i, config.cycles));

        let amount = config.wrap_amount_wei;

        // --- KROK A: WRAP (MON -> WMON) ---
        // Sprawdź balans MON
        match provider.get_balance(my_address).await {
            Ok(balance) => {
                if balance < amount {
                    log(&format!("⚠️  Zbyt niski balans MON: {} wei. Pomijam wrap.", balance));
                } else {
                    log(&format!("📦 Wrapowanie..."));
                    let tx_builder = wmon_contract.deposit().value(amount);
                    match tx_builder.send().await {
                        Ok(tx) => {
                            log("⏳ Transakcja wysłana, czekam na potwierdzenie...");
                            match tx.get_receipt().await {
                                Ok(receipt) => log(&format!("✅ Wrap udany! Hash: {:?}", receipt.transaction_hash)),
                                Err(e) => log(&format!("❌ Błąd pobierania receipt (Wrap): {:?}", e)),
                            }
                        }
                        Err(e) => log(&format!("❌ Błąd wysyłania transakcji (Wrap): {:?}", e)),
                    }
                }
            }
            Err(e) => log(&format!("❌ Błąd sprawdzania balansu MON: {:?}", e)),
        }

        // Losowa pauza (anty-bot detection)
        random_sleep(config.sleep_after_wrap_min, config.sleep_after_wrap_max, "Czekam po wrapowaniu");

        // --- KROK B: UNWRAP (WMON -> MON) ---
        // Sprawdź balans WMON
        match wmon_contract.balanceOf(my_address).call().await {
            Ok(balance_result) => {
                let wmon_balance = balance_result._0;
                if wmon_balance < amount {
                    log(&format!("⚠️  Zbyt niski balans WMON: {} wei. Pomijam unwrap.", wmon_balance));
                } else {
                    log("📤 Odwijanie (Unwrap)...");
                    let tx_builder = wmon_contract.withdraw(amount);
                    match tx_builder.send().await {
                        Ok(tx) => {
                            log("⏳ Transakcja wysłana, czekam na potwierdzenie...");
                            match tx.get_receipt().await {
                                Ok(receipt) => log(&format!("✅ Unwrap udany! Hash: {:?}", receipt.transaction_hash)),
                                Err(e) => log(&format!("❌ Błąd pobierania receipt (Unwrap): {:?}", e)),
                            }
                        }
                        Err(e) => log(&format!("❌ Błąd wysyłania transakcji (Unwrap): {:?}", e)),
                    }
                }
            }
            Err(e) => log(&format!("❌ Błąd sprawdzania balansu WMON: {:?}", e)),
        }

        // Dłuższa pauza przed kolejnym cyklem
        random_sleep(config.sleep_cycles_min, config.sleep_cycles_max, "Odpoczynek przed kolejnym cyklem");
    }

    log("");
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    log(&format!("🏁 Koniec pracy! Wykonano {} cykli.", config.cycles));
    log("🌾 Farma zamknięta.");
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    Ok(())
}

fn random_sleep(min_secs: u64, max_secs: u64, reason: &str) {
    let mut rng = rand::thread_rng();
    let sleep_sec = rng.gen_range(min_secs..max_secs);
    log(&format!("⏳ {} ({}s)...", reason, sleep_sec));
    thread::sleep(Duration::from_secs(sleep_sec));
}
