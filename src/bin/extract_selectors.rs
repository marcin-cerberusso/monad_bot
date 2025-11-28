use std::env;
use std::fs;

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        println!("Usage: extract_selectors <bytecode_file>");
        return;
    }

    let bytecode_hex = fs::read_to_string(&args[1]).expect("Failed to read file");
    let bytecode_hex = bytecode_hex.trim();
    let bytecode = hex::decode(bytecode_hex).expect("Failed to decode hex");

    println!("Searching for selectors (PUSH4 ... EQ)...");

    let mut i = 0;
    while i < bytecode.len() - 5 {
        // Look for PUSH4 (0x63) followed by 4 bytes, then EQ (0x14) or similar dispatch pattern
        // Standard dispatch: DUP1 (0x80) PUSH4 (0x63) ... EQ (0x14)
        // Or just PUSH4 ... EQ
        
        if bytecode[i] == 0x63 {
            if i + 5 < bytecode.len() && bytecode[i+5] == 0x14 {
                let selector = &bytecode[i+1..i+5];
                println!("Found selector: 0x{}", hex::encode(selector));
            } else if i > 0 && bytecode[i-1] == 0x80 && i + 5 < bytecode.len() {
                 // DUP1 PUSH4 ... (EQ might be later)
                 let selector = &bytecode[i+1..i+5];
                 println!("Found potential selector: 0x{}", hex::encode(selector));
            }
        }
        i += 1;
    }
}
