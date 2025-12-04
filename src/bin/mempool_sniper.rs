use alloy::primitives::{Address, U256, Bytes};
use alloy::providers::{Provider, ProviderBuilder, WsConnect};
use alloy::network::EthereumWallet;
use alloy::signers::local::PrivateKeySigner;
use alloy::rpc::types::TransactionRequest;
use alloy::consensus::Transaction as TxTrait;
use futures::StreamExt;
use std::env;
use tokio::time::{sleep, Duration};

const ROUTER: &str = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22";

const SNIPE_AMOUNT_MON: f64 = 5.0;
const MIN_LIQUIDITY_MON: f64 = 100.0;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    dotenv::dotenv().ok();
    
    let private_key = env::var("PRIVATE_KEY").expect("PRIVATE_KEY must be set");
    let signer: PrivateKeySigner = private_key.parse()?;
    let wallet_address = signer.address();
    let wallet = EthereumWallet::from(signer);
    
    println!("╔══════════════════════════════════════════════════════════════╗");
    println!("║  RUST MEMPOOL SNIPER - New Token Hunter                      ║");
    println!("╠══════════════════════════════════════════════════════════════╣");
    println!("║  Wallet: {}  ║", wallet_address);
    println!("║  Snipe Amount: {} MON | Min Liq: {} MON                   ║", SNIPE_AMOUNT_MON, MIN_LIQUIDITY_MON);
    println!("╚══════════════════════════════════════════════════════════════╝");

    let ws_url = env::var("MONAD_WS_URL").unwrap_or_else(|_| "wss://monad-mainnet.g.alchemy.com/v2/KEY".into());
    let rpc_url = env::var("MONAD_RPC_URL").unwrap_or_else(|_| "https://monad-mainnet.g.alchemy.com/v2/KEY".into());

    println!("Connecting to mempool...");
    
    loop {
        if let Err(e) = run_sniper_loop(&ws_url, &rpc_url, wallet.clone()).await {
            println!("Error: {}, reconnecting in 3s...", e);
        }
        sleep(Duration::from_secs(3)).await;
    }
}

async fn run_sniper_loop(
    ws_url: &str,
    rpc_url: &str,
    wallet: EthereumWallet,
) -> Result<(), Box<dyn std::error::Error>> {
    
    let ws = WsConnect::new(ws_url);
    let provider = ProviderBuilder::new().on_ws(ws).await?;
    
    let tx_provider = ProviderBuilder::new()
        .wallet(wallet)
        .on_http(rpc_url.parse()?);
    
    println!("Connected! Monitoring for new token launches...");
    
    let sub = provider.subscribe_pending_transactions().await?;
    let mut stream = sub.into_stream();
    
    let http_provider = ProviderBuilder::new().on_http(rpc_url.parse()?);
    
    while let Some(tx_hash) = stream.next().await {
        if let Ok(Some(tx)) = http_provider.get_transaction_by_hash(tx_hash).await {
            let input = tx.input();
            
            if let Some(to) = tx.to() {
                let to_str = to.to_string().to_lowercase();
                
                if to_str == ROUTER.to_lowercase() {
                    let value_mon = tx.value().to::<u128>() as f64 / 1e18;
                    
                    if value_mon >= MIN_LIQUIDITY_MON && input.len() > 100 {
                        if input.len() >= 4 {
                            let selector = &input[0..4];
                            
                            println!("\nPOTENTIAL NEW TOKEN DETECTED!");
                            println!("   TX: {:?}", tx_hash);
                            println!("   Liquidity: {:.2} MON", value_mon);
                            println!("   Selector: 0x{}", hex::encode(selector));
                            
                            if let Some(token) = extract_token_from_creation(input) {
                                println!("   Token: {}", token);
                                println!("   SNIPING {} MON...", SNIPE_AMOUNT_MON);
                                
                                let router: Address = ROUTER.parse()?;
                                let token_addr: Address = token.parse()?;
                                
                                let mut calldata = vec![0xd9, 0x6a, 0x09, 0x4a];
                                calldata.extend_from_slice(&[0u8; 12]);
                                calldata.extend_from_slice(token_addr.as_slice());
                                
                                let value = U256::from((SNIPE_AMOUNT_MON * 1e18) as u128);
                                
                                let buy_tx = TransactionRequest::default()
                                    .to(router)
                                    .value(value)
                                    .input(Bytes::from(calldata).into());
                                
                                match tx_provider.send_transaction(buy_tx).await {
                                    Ok(pending) => println!("   SNIPED! TX: {:?}", pending.tx_hash()),
                                    Err(e) => println!("   Snipe failed: {}", e),
                                }
                            }
                            println!();
                        }
                    }
                }
            }
        }
    }
    
    Ok(())
}

fn extract_token_from_creation(input: &alloy::primitives::Bytes) -> Option<String> {
    if input.len() >= 36 {
        let token_bytes = &input[16..36];
        Some(format!("0x{}", hex::encode(token_bytes)))
    } else {
        None
    }
}
