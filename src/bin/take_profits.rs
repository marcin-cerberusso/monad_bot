// ═══════════════════════════════════════════════════════════════════════════════
// 💰 TAKE PROFITS - Sell 70% of profitable positions
// ═══════════════════════════════════════════════════════════════════════════════

use alloy::{
    network::{EthereumWallet, TransactionBuilder},
    primitives::{Address, U256},
    providers::{Provider, ProviderBuilder, WalletProvider},
    rpc::types::TransactionRequest,
    signers::local::PrivateKeySigner,
    sol,
    sol_types::SolCall,
};
use chrono::Local;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;

// NAD.FUN Router
const ROUTER: &str = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22";

// ABI
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

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Position {
    token_address: String,
    token_name: String,
    amount_mon: f64,
    entry_price_mon: f64,
    highest_value_mon: f64,
    timestamp: u64,
    #[serde(default)]
    moonbag_secured: bool,
}

fn log(msg: &str) {
    println!("[{}] {}", Local::now().format("%H:%M:%S"), msg);
}

fn wei_to_mon(wei: U256) -> f64 {
    wei.to::<u128>() as f64 / 1e18
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    dotenv::dotenv().ok();

    println!("═══════════════════════════════════════════════════════════════");
    println!("💰 TAKE PROFITS - Sell 70% of winning positions");
    println!("═══════════════════════════════════════════════════════════════");

    // Setup
    let rpc_url =
        env::var("MONAD_RPC_URL").unwrap_or_else(|_| "https://testnet-rpc.monad.xyz".to_string());
    let private_key = env::var("PRIVATE_KEY").expect("PRIVATE_KEY required");

    let signer: PrivateKeySigner = private_key.parse()?;
    let wallet = EthereumWallet::from(signer.clone());
    let rpc: url::Url = rpc_url.parse()?;
    let provider = ProviderBuilder::new().wallet(wallet.clone()).on_http(rpc);

    let my_address = provider.default_signer_address();
    let balance = provider.get_balance(my_address).await?;

    log(&format!("👤 Wallet: {:?}", my_address));
    log(&format!("💵 Balance: {:.2} MON", wei_to_mon(balance)));
    println!();

    // Load positions
    let positions_file = "positions.json";
    let positions: HashMap<String, Position> = match fs::read_to_string(positions_file) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => {
            log("❌ No positions.json found");
            return Ok(());
        }
    };

    // Find profitable positions (30%+ profit)
    let mut profitable: Vec<(String, Position, f64)> = Vec::new();

    for (addr, pos) in &positions {
        let entry = pos.amount_mon;
        let ath = pos.highest_value_mon;
        let pnl = if entry > 0.0 {
            ((ath - entry) / entry) * 100.0
        } else {
            0.0
        };

        if pnl >= 30.0 && !pos.moonbag_secured {
            profitable.push((addr.clone(), pos.clone(), pnl));
        }
    }

    if profitable.is_empty() {
        log("❌ No positions with 30%+ unrealized profit");
        log("💡 Positions need highest_value_mon > 1.3x amount_mon");
        return Ok(());
    }

    // Sort by PnL descending
    profitable.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap());

    log(&format!(
        "🎯 Found {} profitable positions:",
        profitable.len()
    ));
    for (addr, pos, pnl) in &profitable {
        log(&format!(
            "   • {} | +{:.1}% | Will sell 70%",
            pos.token_name, pnl
        ));
    }
    println!();

    let router_addr: Address = ROUTER.parse()?;

    // Execute sells
    for (token_addr_str, pos, pnl) in &profitable {
        let token_addr: Address = token_addr_str.parse()?;

        log(&format!(
            "🔄 Processing {} (+{:.1}%)...",
            pos.token_name, pnl
        ));

        // Get balance
        let balance_call = balanceOfCall {
            account: my_address,
        };
        let balance_result = provider
            .call(
                &TransactionRequest::default()
                    .to(token_addr)
                    .input(balance_call.abi_encode().into()),
            )
            .await?;

        let token_balance = U256::from_be_slice(&balance_result);
        if token_balance == U256::ZERO {
            log(&format!("   ⚠️ No balance, skipping"));
            continue;
        }

        let sell_amount = token_balance * U256::from(70) / U256::from(100);
        log(&format!(
            "   💰 Balance: {:.2} tokens",
            wei_to_mon(token_balance)
        ));
        log(&format!(
            "   📤 Selling: {:.2} tokens (70%)",
            wei_to_mon(sell_amount)
        ));

        // Approve
        let approve_call = approveCall {
            spender: router_addr,
            amount: sell_amount,
        };

        let gas_price = provider.get_gas_price().await?;
        let nonce = provider.get_transaction_count(my_address).await?;

        let approve_tx = TransactionRequest::default()
            .to(token_addr)
            .input(approve_call.abi_encode().into())
            .max_fee_per_gas(gas_price + gas_price / 2)
            .max_priority_fee_per_gas(gas_price / 10)
            .nonce(nonce)
            .gas_limit(100000)
            .with_chain_id(143);

        match provider.send_transaction(approve_tx).await {
            Ok(pending) => {
                let _ = pending.watch().await;
                log("   🔐 Approved");
            }
            Err(e) => {
                log(&format!("   ❌ Approve failed: {:?}", e));
                continue;
            }
        }

        // Sell
        let deadline = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            + 300;

        let sell_params = SellParams {
            amountIn: sell_amount,
            amountOutMin: U256::ZERO,
            token: token_addr,
            to: my_address,
            deadline: U256::from(deadline),
        };

        let sell_call = sellCall {
            params: sell_params,
        };

        let nonce = provider.get_transaction_count(my_address).await?;
        let sell_tx = TransactionRequest::default()
            .to(router_addr)
            .input(sell_call.abi_encode().into())
            .max_fee_per_gas(gas_price + gas_price / 2)
            .max_priority_fee_per_gas(gas_price / 10)
            .nonce(nonce)
            .gas_limit(300000)
            .with_chain_id(143);

        match provider.send_transaction(sell_tx).await {
            Ok(pending) => {
                let receipt = pending.get_receipt().await?;
                if receipt.status() {
                    log(&format!("   ✅ SOLD! TX: {:?}", receipt.transaction_hash));
                } else {
                    log(&format!("   ❌ TX failed: {:?}", receipt.transaction_hash));
                }
            }
            Err(e) => {
                log(&format!("   ❌ Sell failed: {:?}", e));
            }
        }
    }

    // Update positions.json - mark as moonbag_secured
    let mut updated_positions = positions.clone();
    for (addr, _, _) in &profitable {
        if let Some(pos) = updated_positions.get_mut(addr) {
            pos.moonbag_secured = true;
        }
    }
    fs::write(
        positions_file,
        serde_json::to_string_pretty(&updated_positions)?,
    )?;
    log("📝 Updated positions.json (moonbag_secured = true)");

    println!();
    println!("═══════════════════════════════════════════════════════════════");
    let new_balance = provider.get_balance(my_address).await?;
    log(&format!(
        "💵 New Balance: {:.2} MON",
        wei_to_mon(new_balance)
    ));
    log("✅ Take profits complete!");

    Ok(())
}
