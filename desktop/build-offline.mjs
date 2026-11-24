// Builds desktop/index.html: the game from ../index.html with three.js, cannon-es
// and the fonts bundled in, so it runs with no internet connection.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { build } from 'esbuild';

const src = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

const modOpen = src.indexOf('<script type="module">');
const modClose = src.indexOf('</script>', modOpen);
if (modOpen < 0 || modClose < 0) throw new Error('module script not found in ../index.html');
const gameCode = src.slice(modOpen + '<script type="module">'.length, modClose);

mkdirSync(new URL('./src/', import.meta.url), { recursive: true });
writeFileSync(new URL('./src/game.js', import.meta.url), gameCode);

const out = await build({
  entryPoints: [new URL('./src/game.js', import.meta.url).pathname],
  bundle: true, format: 'iife', minify: true, write: false, target: 'chrome120',
  alias: { 'three/addons': 'three/examples/jsm' }
});
const bundled = out.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');

// Inline Google Fonts as data URIs when reachable; otherwise the CSS fallbacks apply.
let fontCss = '';
const fontLink = src.match(/<link rel="stylesheet" href="(https:\/\/fonts\.googleapis\.com[^"]+)">/);
if (fontLink) {
  try {
    const css = await (await fetch(fontLink[1].replace(/&amp;/g, '&'), { headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36' } })).text();
    // keep only the latin subsets to stay small
    const blocks = css.split('/* ').filter(b => b.startsWith('latin */'));
    let inlined = '';
    for (const b of blocks) {
      const face = b.slice(b.indexOf('@font-face'));
      const url = face.match(/url\((https:[^)]+)\)/)[1];
      const buf = Buffer.from(await (await fetch(url)).arrayBuffer());
      inlined += face.replace(url, `data:font/woff2;base64,${buf.toString('base64')}`) + '\n';
    }
    fontCss = inlined;
    console.log(`fonts inlined: ${blocks.length} faces`);
  } catch (e) {
    console.log('fonts not inlined (offline fallbacks will be used):', e.message);
  }
}

let html = src.slice(0, modOpen) + `<script>\n${bundled}\n</script>` + src.slice(modClose + '</script>'.length);
html = html.replace(/<script type="importmap">[\s\S]*?<\/script>\s*/, '');
html = html.replace(/<link rel="preconnect"[^>]*>\s*/, '');
if (fontLink) html = html.replace(fontLink[0], fontCss ? `<style>\n${fontCss}</style>` : '');

writeFileSync(new URL('./index.html', import.meta.url), html);
console.log(`wrote desktop/index.html (${(html.length / 1024).toFixed(0)} KB)`);
