import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';
const require = createRequire(import.meta.url);
const { PGlite } = require(process.env.AUDIT_PGLITE_MODULE || '@electric-sql/pglite');
const db = new PGlite();
await db.waitReady;
process.stdout.write(JSON.stringify({ready: true}) + '\n');
for await (const line of createInterface({input: process.stdin})) {
  try {
    const input = JSON.parse(line);
    const result = await db.query(input.sql, input.params || []);
    process.stdout.write(JSON.stringify({ok: true, ...result}, (_, v) => typeof v === 'bigint' ? Number(v) : v) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ok: false, error: error.message, code: error.code}) + '\n');
  }
}
await db.close();
