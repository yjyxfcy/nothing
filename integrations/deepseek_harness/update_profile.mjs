#!/usr/bin/env node
import { readFileSync, writeFileSync } from 'node:fs';

const [profilePath, bundlePath] = process.argv.slice(2);
if (!profilePath || !bundlePath) {
  console.error('usage: node update_profile.mjs <package.json> <bundle-path>');
  process.exit(2);
}

const manifest = JSON.parse(readFileSync(profilePath, 'utf8'));
if (!manifest.dsh || typeof manifest.dsh !== 'object') manifest.dsh = {};
if (!manifest.dsh.profile || typeof manifest.dsh.profile !== 'object') manifest.dsh.profile = {};
const bundles = Array.isArray(manifest.dsh.profile.bundles) ? manifest.dsh.profile.bundles : [];
if (!bundles.includes('@local/dsh-rosetta-compat')) {
  bundles.push('@local/dsh-rosetta-compat');
}
manifest.dsh.profile.bundles = bundles;

if (!manifest.dependencies || typeof manifest.dependencies !== 'object') manifest.dependencies = {};
manifest.dependencies['@local/dsh-rosetta-compat'] =
  'link:' + bundlePath.replace(/\\/g, '/');

writeFileSync(profilePath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');
