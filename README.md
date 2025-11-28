# 🌾 Monad Farming Bot v2.1

Automated WMON (Wrapped Monad) farming bot that wraps and unwraps MON tokens on the Monad testnet to generate testnet activity and potentially earn rewards.

## 🎯 What it does

The bot performs continuous cycles of:
1. **Wrap**: Deposit MON → Receive WMON
2. **Unwrap**: Withdraw WMON → Receive MON back

Each cycle wraps and unwraps **0.0001 MON** with random delays to simulate organic activity.

## ⚡ Quick Start

### Prerequisites

- Rust (latest stable version)
- Node.js v16+ (for address verification utility)
- 100-200 MON tokens on Monad testnet

### Installation

```bash
# 1. Navigate to the monad_engine directory
cd monad_engine

# 2. Install Node.js dependencies (for address derivation)
npm install

# 3. Build the Rust bot (optional - will build on first run)
cargo build --release
```

### Configuration

The bot uses environment variables from `.env` file:

```env
MONAD_RPC_URL=https://testnet-rpc.monad.xyz
PRIVATE_KEY=your_private_key_here_without_0x_prefix
```

> [!WARNING]
> **Never commit your `.env` file or share your private key!**

### Getting MON Tokens

1. Open Phantom wallet
2. Switch to **Monad Testnet** network
3. Send 100-200 MON to your bot's address
4. The bot will display its address on first run

For detailed instructions, see [TRANSFER_INSTRUCTIONS.md](TRANSFER_INSTRUCTIONS.md)

### Running the Bot

```bash
# Run in development mode (with debug info)
cargo run

# Run in release mode (optimized, faster)
cargo run --release
```

## 📊 Expected Output

```
[2025-11-27 22:00:01] 🚀 Odpalam Monad Farmera (v2.1 - Enhanced)...
[2025-11-27 22:00:01] 👤 Zalogowano jako: 0x6962cf43518daf0d8d2fdfff5b895c27750116a3
[2025-11-27 22:00:01] 🌐 RPC: https://testnet-rpc.monad.xyz
[2025-11-27 22:00:01] 🎯 WMON Contract: 0x760afe86e5de5fa0ee542fc7b7b713e1c5425701
[2025-11-27 22:00:01] 🔄 Zaplanowano: 50 cykli
[2025-11-27 22:00:01] 💰 Kwota na cykl: 100000000000000 wei (0.0001 MON)
[2025-11-27 22:00:02] 💼 Balans początkowy: 150.5 MON

[2025-11-27 22:00:02] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[2025-11-27 22:00:02] 🌾 Rozpoczynam pętlę farmingową...
[2025-11-27 22:00:02] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[2025-11-27 22:00:03] 🔄 ===== Cykl 1/50 =====
[2025-11-27 22:00:03] 📦 Wrapowanie 0.0001 MON...
[2025-11-27 22:00:04] ⏳ Transakcja wysłana, czekam na potwierdzenie...
[2025-11-27 22:00:06] ✅ Wrap udany! Hash: 0xabc...def
...
```

## 🛠️ Configuration

### Bot Parameters

Edit these constants in `src/main.rs`:

```rust
const WMON_ADDRESS: Address = address!("760AfE86e5de5fa0Ee542fc7B7B713e1c5425701");
const CYCLES: u32 = 50;              // Number of wrap/unwrap cycles
const WRAP_AMOUNT_WEI: u128 = 100_000_000_000_000; // 0.0001 MON
```

### Random Delays

The bot uses random delays to avoid detection:
- **15-45 seconds** after wrapping
- **30-90 seconds** between cycles

## 🔧 Utilities

### Verify Your Address

Use the Node.js utility to verify your wallet address matches the private key:

```bash
# Using npm script
npm run derive-address YOUR_PRIVATE_KEY_HEX

# Or directly
node derive_address.js YOUR_PRIVATE_KEY_HEX
```

Expected output:
```
Adres: 0x6962cf43518daf0d8d2fdfff5b895c27750116a3
```

## 🐛 Troubleshooting

### Bot shows "OSTRZEŻENIE: Portfel ma 0 MON!"

**Solution**: Transfer 100-200 MON from Phantom to the displayed address. See [TRANSFER_INSTRUCTIONS.md](TRANSFER_INSTRUCTIONS.md)

### "Zbyt niski balans MON" during wrap

**Cause**: You're running out of MON tokens
**Solution**: Transfer more MON to your wallet or reduce `CYCLES` constant

### "Błąd wysyłania transakcji"

**Possible causes**:
1. Network connectivity issues - check RPC URL
2. Insufficient gas - ensure you have enough MON
3. Nonce issues - wait a few seconds and try again

### Node.js script fails with "Cannot find module 'ethereumjs-util'"

**Solution**: Run `npm install` in the monad_engine directory

## 📁 Project Structure

```
monad_engine/
├── src/
│   └── main.rs              # Main bot logic
├── Cargo.toml               # Rust dependencies
├── package.json             # Node.js dependencies
├── derive_address.js        # Address derivation utility
├── .env                     # Configuration (not in git)
├── .gitignore              
├── TRANSFER_INSTRUCTIONS.md # How to fund your wallet
└── README.md               # This file
```

## 🔒 Security Notes

- **Never share your private key** with anyone
- Keep your `.env` file secure and never commit it to git
- This is for **testnet only** - do not use mainnet keys
- The bot is for educational and testnet farming purposes

## 📈 Performance

- **Cycles**: 50 (configurable)
- **Amount per cycle**: 0.0001 MON
- **Estimated time**: ~30-60 minutes for 50 cycles (due to random delays)
- **Total MON locked**: 0.0001 MON at a time (minimal capital requirements)

## 🚀 Advanced Usage

### Running in Background

```bash
# Using nohup
nohup cargo run --release > monad_bot.log 2>&1 &

# Using tmux (recommended)
tmux new -s monad
cargo run --release
# Press Ctrl+B, then D to detach
```

### Monitoring Logs

```bash
# If using nohup
tail -f monad_bot.log

# If using tmux
tmux attach -t monad
```

## 📚 Additional Resources

- [Monad Documentation](https://docs.monad.xyz)
- [Monad Explorer](https://monadexplorer.com)
- [INFRASTRUCTURE.md](../INFRASTRUCTURE.md) - Advanced node setup guide

## ⚖️ License

MIT License - Use at your own risk. This is experimental testnet software.

---

**Made with 🌾 for Monad testnet farming**
