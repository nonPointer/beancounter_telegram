const fs = require('fs');
const path = require('path');

const accountsDir = path.resolve(__dirname, '../../accounts');
const files = fs.readdirSync(accountsDir).filter(f => f.endsWith('.bean'));

const accounts = [];
for (const file of files) {
	const content = fs.readFileSync(path.join(accountsDir, file), 'utf-8');
	for (const match of content.matchAll(/\d{4}-\d{2}-\d{2} open (.*)/g)) {
		accounts.push(match[1].trim().split(/\s+/)[0]);
	}
}

accounts.sort();
fs.writeFileSync(
	path.resolve(__dirname, '../src/accounts.json'),
	JSON.stringify(accounts, null, 2)
);
console.log(`Synced ${accounts.length} accounts`);
