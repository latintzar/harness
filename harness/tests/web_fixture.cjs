/* Serve the shared shell and selected retail assets at their real URLs, without copying source. */
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const root = path.resolve(__dirname, '..');
const mirrors=new Map();
function merge(source, target) {
  fs.mkdirSync(target, { recursive: true });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name), to = path.join(target, entry.name);
    if (entry.isDirectory()) merge(from, to);
    else if (entry.isFile() && !fs.existsSync(to)) fs.symlinkSync(from, to);
  }
}
function web(vertical='retail') {
  if(!['retail','bare'].includes(vertical))throw new Error('Use a declared browser fixture vertical.');
  let mirror=mirrors.get(vertical);
  if (!mirror) {
    mirror = fs.mkdtempSync(path.join(os.tmpdir(), 'rocoto-test-web-'));
    mirrors.set(vertical,mirror);
    process.once('exit', () => fs.rmSync(mirror, { recursive: true, force: true }));
    // The real host resolves shared assets first, then the active vertical's files.
    merge(path.join(root, 'web'), mirror);
    merge(path.join(root, 'web'), path.join(mirror, 'static/harness'));
    merge(path.join(root, 'web/vendor'), path.join(mirror, 'static/vendor'));
    for (const name of ['brand', 'fonts', 'connection-brands']) merge(path.join(root, '../app/public', name), path.join(mirror, 'static/harness', name));
    if(vertical==='retail'){merge(path.join(root, 'verticals/retail/web'), mirror);merge(path.join(root,'verticals/retail/web'),path.join(mirror,'static/retail'));}
    const index=path.join(mirror,'index.html');
    const source=fs.readFileSync(index,'utf8');fs.unlinkSync(index);
    const assets=vertical==='bare'?'':['camera-streams.css','tracker-canvas.css','retail-scope.css','retail-ui.css'].map(name=>`<link rel="stylesheet" href="/static/retail/${name}" />`).concat(
      ['camera-streams.js','tracker-canvas.js','control-room.js','retail-shell.js','retail-scope.js','retail-ui.js'].map(name=>`<script src="/static/retail/${name}" defer></script>`)).join('\n');
    fs.writeFileSync(index,source.replace('<!-- vertical-assets -->',assets));
  }
  return mirror;
}
module.exports = { web };
