const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=()=>fs.readFileSync(path.join(__dirname,'../web/workspace-extensions.js'),'utf8');
const load=()=>{const window={};vm.runInNewContext(source(),{window});return window.RocotoWorkspaceExtensions;};

test('the bare extension host is empty and keeps each installed instance separate',()=>{
  const registry=load();assert.deepEqual(Object.keys(registry.install({})),[]);
  registry.register('manufacturer',host=>{let selected='';return {read:()=>host.owner,select:value=>{selected=value;},get selected(){return selected;}};});
  let owner='one';const first=registry.install({get owner(){return owner;}}),second=registry.install({owner:'two'});
  owner='changed';first.select('dispatch');assert.equal(first.read(),'changed');assert.equal(second.read(),'two');assert.equal(second.selected,'');
});

test('a page admits one source-owned vertical and validates its factory result',()=>{
  const registry=load();registry.register('retail',()=>({}));
  assert.throws(()=>registry.register('manufacturer',()=>({})),/one installed vertical/);
  const bad=load();assert.throws(()=>bad.register('../retail',()=>({})),/vertical/);
  bad.register('manufacturer',()=>null);assert.throws(()=>bad.install({}),/component/);
});
