use alloy::primitives::keccak256;

fn main() {
    let signatures = vec![
        "buy(uint256,address,address,uint256)",
        "sell(uint256,address,address,uint256)",
        "createToken(string,string,uint256)",
        "createToken(string,string,uint256,address)",
        "launch(string,string,uint256)",
        "launch(string,string,uint256,address)",
        "create(string,string,uint256)",
        "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
        "swapExactETHForTokens(uint256,address[],address,uint256)",
    ];

    for sig in signatures {
        let hash = keccak256(sig.as_bytes());
        println!("0x{} -> {}", hex::encode(&hash[0..4]), sig);
    }
}
