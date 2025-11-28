const { privateToAddress } = require('ethereumjs-util');

// Wygenerowany klucz prywatny
const privateKey = Buffer.from(process.argv[2], 'hex');
const address = privateToAddress(privateKey);

console.log('Adres:', '0x' + address.toString('hex'));
