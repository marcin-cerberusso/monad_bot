// SELL ALL POSITIONS - Clean slate!
// Sprzedaje 100% wszystkich tokenów

use alloy::{
    network::EthereumWallet,
    primitives::{Address, U256},
    providers::{Provider, ProviderBuilder},
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use anyhow::Result;
use dotenv::dotenv;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    env,
    str::FromStr,
    time::{SystemTime, UNIX_EPOCH},
};

sol! {
    #[derive(Debug)]
    function balanceOf(address account) external view returns (uint256);

    #[derive(Debug)]
    function approve(address spender, uint256 amount) external returns (bool);

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

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Position {
    token_address: String,
    token_name: String,
    amount_mon: f64,
    entry_price_mon: f64,
    timestamp: u64,
    #[serde(default)]
    highest_value_mon: f64,
    #[serde(default)]
    moonbag_secured: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();

    println!("╔═══════════════════════════════════════════════════════════════╗");
    println!("║  🧹 SELL ALL - CLEAN SLATE                                    ║");
    println!("╚═══════════════════════════════════════════════════════════════╝");

    let rpc_url = env::var("MONAD_RPC_URL").expect("Brak MONAD_RPC_URL");
    let private_key = env::var("PRIVATE_KEY").expect("Brak PRIVATE_KEY");
    let router_str = env::var("ROUTER_ADDRESS")
        .unwrap_or("0x6F6B8F1a20703309951a5127c45B49b1CD981A22".to_string());

    let signer = PrivateKeySigner::from_str(&private_key)?;
    let wallet = EthereumWallet::from(signer.clone());
    let bot_address = signer.address();
    let router_address = Address::from_str(&router_str)?;

    let provider = ProviderBuilder::new()
        .with_recommended_fillers()
        .wallet(wallet)
        .on_http(rpc_url.parse()?);

    // Load positions
    let positions: HashMap<String, Position> = std::fs::read_to_string("positions.json")
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();

    println!("📊 Found {} positions to sell", positions.len());
    println!("👤 Wallet: {:?}", bot_address);
    println!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

    let mut sold_count = 0;
    let mut failed_count = 0;
    let mut total_mon_recovered = 0.0;

    for (token_addr_str, position) in positions.iter() {
        println!(
            "\n🔄 Selling: {} ({})",
            position.token_name,
            &token_addr_str[..10]
        );

        let token_address = match Address::from_str(token_addr_str) {
            Ok(addr) => addr,
            Err(_) => {
                println!("   ❌ Invalid address, skipping");
                failed_count += 1;
                continue;
            }
        };

        // Get token balance
        let balance_call = balanceOfCall {
            account: bot_address,
        };
        let balance_result = provider
            .call(
                &alloy::rpc::types::TransactionRequest::default()
                    .to(token_address)
                    .input(balance_call.abi_encode().into()),
            )
            .await;

        let balance = match balance_result {
            Ok(data) => {
                if data.len() >= 32 {
                    U256::from_be_slice(&data[..32])
                } else {
                    println!("   ❌ Invalid balance response");
                    failed_count += 1;
                    continue;
                }
            }
            Err(e) => {
                println!("   ❌ Balance check failed: {:?}", e);
                failed_count += 1;
                continue;
            }
        };

        if balance == U256::ZERO {
            println!("   ⚠️ Zero balance, skipping");
            continue;
        }

        let balance_f64 = balance.to_string().parse::<f64>().unwrap_or(0.0) / 1e18;
        println!("   📦 Balance: {:.4} tokens", balance_f64);

        // Approve router
        println!("   📝 Approving...");
        let approve_call = approveCall {
            spender: router_address,
            amount: balance,
        };

        let approve_tx = alloy::rpc::types::TransactionRequest::default()
            .to(token_address)
            .input(approve_call.abi_encode().into())
            .gas_limit(100_000);

        match provider.send_transaction(approve_tx).await {
            Ok(pending) => {
                if let Ok(receipt) = pending.get_receipt().await {
                    if !receipt.status() {
                        println!("   ❌ Approve failed");
                        failed_count += 1;
                        continue;
                    }
                }
            }
            Err(e) => {
                println!("   ❌ Approve error: {:?}", e);
                failed_count += 1;
                continue;
            }
        }

        // Sell 100%
        println!("   💰 Selling 100%...");
        let deadline = U256::from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs()
                + 120,
        );

        let sell_params = SellParams {
            amountIn: balance,
            amountOutMin: U256::ZERO, // Accept any amount
            token: token_address,
            to: bot_address,
            deadline,
        };
        let sell_call = sellCall {
            params: sell_params,
        };

        let sell_tx = alloy::rpc::types::TransactionRequest::default()
            .to(router_address)
            .input(sell_call.abi_encode().into())
            .gas_limit(500_000)
            .max_priority_fee_per_gas(1_000_000_000_000);

        match provider.send_transaction(sell_tx).await {
            Ok(pending) => {
                let tx_hash = pending.tx_hash();
                println!("   📤 TX: {:?}", tx_hash);

                match pending.get_receipt().await {
                    Ok(receipt) => {
                        if receipt.status() {
                            println!("   ✅ SOLD! Gas: {}", receipt.gas_used);
                            sold_count += 1;
                            total_mon_recovered += position.entry_price_mon * 0.8;
                        // Estimate ~80% recovery
                        } else {
                            println!("   ❌ TX Failed");
                            failed_count += 1;
                        }
                    }
                    Err(e) => {
                        println!("   ❌ Receipt error: {:?}", e);
                        failed_count += 1;
                    }
                }
            }
            Err(e) => {
                println!("   ❌ Send error: {:?}", e);
                failed_count += 1;
            }
        }

        // Small delay between sells
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }

    println!("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
    println!("📊 SUMMARY:");
    println!("   ✅ Sold: {}", sold_count);
    println!("   ❌ Failed: {}", failed_count);
    println!("   💰 Est. MON recovered: ~{:.2}", total_mon_recovered);

    // Clear positions file
    if sold_count > 0 {
        println!("\n🧹 Clearing positions.json...");
        std::fs::write("positions.json", "{}")?;
        println!("✅ Clean slate! Ready for fresh start with $20!");
    }

    Ok(())
}
