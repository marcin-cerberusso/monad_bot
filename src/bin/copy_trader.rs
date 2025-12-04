use alloy::primitives::{Address, U256, Bytes};
use alloy::providers::{Provider, ProviderBuilder, WsConnect};
use alloy::network::EthereumWallet;
use alloy::signers::local::PrivateKeySigner;
use alloy::rpc::types::TransactionRequest;
use alloy::consensus::Transaction as TxTrait;
use futures::StreamExt;
use std::collections::HashSet;
use std::env;
use tokio::time::{sleep, Duration};

const ROUTER: &str = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22";

const WHALE_WHITELIST: &[&str] = &[
    "0xa49cee842116a89299a721d831bcf0511e8f6a15",
    "0x2fd1887e5d99014cb0b8884f06560ed20d65003d",
    "0xce04388107acbea0a7d108bdc2ca3360bb797431",
    "0xe79e8461fa12514235e5cf216050497d009c68e3",
    "0x28ddf82febffc3696dd66738af1ec162dc1189c8",
    "0xe2d8ef897db3297ce374c5b70283b2e0484e2950",
    "0x571b6770ed63863d7cc7d461b1c4ec5504f17faa",
    "0xf78827d8113b168c9796a5925239c6718d83e69c",
    "0xcb69535abbc95a042914507f963bdd74ad0025ff",
    "0xc70709843e67d8f848507cfaa8bd1d68913ce02d",
    "0x7d360835e422791e7b3f0d21199cc063816a7970",
    "0xf5a3fddbd8535d4f54db41cda4d03c95d6255efe",
    "0xca1a2fb7f3179d887504966b25d1606978adcd42",
    "0xd1f01634b227b167b6d15d24d955c86999330ee1",
    "0xb5a0b22cd6f0350857bd10cac37f58cbb5f71888",
];

const MIN_WHALE_BUY_MON: f64 = 50.0;
const FOLLOW_AMOUNT_MON: f64 = 10.0;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    dotenv::dotenv().ok();
    
    let private_key = env::var("PRIVATE_KEY").expect("PRIVATE_KEY must be set");
    let signer: PrivateKeySigner = private_key.parse()?;
    let wallet_address = signer.address();
    let wallet = EthereumWallet::from(signer);
    
    println!("╔══════════════════════════════════════════════════════════════╗");
    println!("║  RUST COPY TRADER - Ultra Fast Whale Following               ║");
    println!("╠══════════════════════════════════════════════════════════════╣");
    println!("║  Wallet: {}  ║", wallet_address);
    println!("║  Whales: {} tracked | Follow: {} MON                       ║", WHALE_WHITELIST.len(), FOLLOW_AMOUNT_MON);
    println!("╚══════════════════════════════════════════════════════════════╝");

    let whitelist: HashSet<String> = WHALE_WHITELIST.iter().map(|s| s.to_lowercase()).collect();

    let ws_url = env::var("MONAD_WS_URL").unwrap_or_else(|_| "wss://monad-mainnet.g.alchemy.com/v2/KEY".into());
    let rpc_url = env::var("MONAD_RPC_URL").unwrap_or_else(|_| "https://monad-mainnet.g.alchemy.com/v2/KEY".into());

    println!("Connecting...");
    
    loop {
        if let Err(e) = run_loop(&ws_url, &rpc_url, &whitelist, wallet.clone()).await {
            println!("Error: {}, reconnecting in 5s...", e);
        }
        sleep(Duration::from_secs(5)).await;
    }
}

async fn run_loop(
    ws_url: &str,
    rpc_url: &str,
    whitelist: &HashSet<String>,
    wallet: EthereumWallet,
) -> Result<(), Box<dyn std::error::Error>> {
    
    let ws = WsConnect::new(ws_url);
    let provider = ProviderBuilder::new().on_ws(ws).await?;
    
    let tx_provider = ProviderBuilder::new()
        .wallet(wallet)
        .on_http(rpc_url.parse()?);
    
    println!("Connected! Monitoring pending transactions...");
    
    let sub = provider.subscribe_pending_transactions().await?;
    let mut stream = sub.into_stream();
    
    let http_provider = ProviderBuilder::new().on_http(rpc_url.parse()?);
    
    while let Some(tx_hash) = stream.next().await {
        if let Ok(Some(tx)) = http_provider.get_transaction_by_hash(tx_hash).await {
            if let Some(to) = tx.to() {
                if to.to_string().to_lowercase() == ROUTER.to_lowercase() {
                    let from = tx.from.to_string().to_lowercase();
                    
                    if whitelist.contains(&from) {
                        let value_mon = tx.value().to::<u128>() as f64 / 1e18;
                        
                        if value_mon >= MIN_WHALE_BUY_MON {
                            println!("\nWHALE BUY DETECTED!");
                            println!("   From: {}", from);
                            println!("   Value: {:.2} MON", value_mon);
                            
                            if let Some(token) = extract_token(tx.input()) {
                                println!("   Token: {}", token);
                                println!("   Executing {} MON buy...", FOLLOW_AMOUNT_MON);
                                
                                let router: Address = ROUTER.parse()?;
                                let token_addr: Address = token.parse()?;
                                
                                let mut calldata = vec![0xd9, 0x6a, 0x09, 0x4a];
                                calldata.extend_from_slice(&[0u8; 12]);
                                calldata.extend_from_slice(token_addr.as_slice());
                                
                                let value = U256::from((FOLLOW_AMOUNT_MON * 1e18) as u128);
                                
                                let buy_tx = TransactionRequest::default()
                                    .to(router)
                                    .value(value)
                                    .input(Bytes::from(calldata).into());
                                
                                match tx_provider.send_transaction(buy_tx).await {
                                    Ok(pending) => println!("   TX: {:?}", pending.tx_hash()),
                                    Err(e) => println!("   Failed: {}", e),
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    
    Ok(())
}

fn extract_token(input: &alloy::primitives::Bytes) -> Option<String> {
    if input.len() >= 36 {
        let token_bytes = &input[16..36];
        Some(format!("0x{}", hex::encode(token_bytes)))
    } else {
        None
    }
}
