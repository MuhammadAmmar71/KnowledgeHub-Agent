/**
 * Runs before every build. Warns loudly while deployment placeholders are
 * still in the package config, so a package that can't work (no backend URL,
 * unresolvable API permission) isn't uploaded to the App Catalog by mistake.
 * Exits non-zero only with --strict (use that in release pipelines).
 */
const fs = require('fs');
const path = require('path');

const FILES = [
  'config/package-solution.json',
  'sharepoint/assets/elements.xml',
  'sharepoint/assets/ClientSideInstance.xml',
];
const PLACEHOLDERS = {
  __APP_REGISTRATION_DISPLAY_NAME__: 'the Entra app registration display name (webApiPermissionRequests.resource)',
  __BACKEND_HOST__: 'the backend host in apiUrl (e.g. knowledge-agent.<region>.azurecontainerapps.io)',
};

const found = [];
for (const file of FILES) {
  const text = fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
  for (const [token, meaning] of Object.entries(PLACEHOLDERS)) {
    if (text.includes(token)) found.push(`  ${file}: ${token} -> ${meaning}`);
  }
}

if (found.length) {
  const strict = process.argv.includes('--strict');
  console.warn(`\n${strict ? 'ERROR' : 'WARNING'}: deployment placeholders are still set. The package will build, but`);
  console.warn('it must not be uploaded to the App Catalog until these are replaced:');
  console.warn(found.join('\n') + '\n');
  process.exit(strict ? 1 : 0);
}
console.log('Deployment config: no placeholders left.');
