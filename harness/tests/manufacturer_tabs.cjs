const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function node(tag='root') { return {tag, children: [], textContent: '', append(item){this.children.push(item);},replaceChildren(...children){this.children=children;}}; }
function harness(fetch) {
  const scope={window:{}, AbortController, fetch, document:{createElement:node}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/workspace-tabs.js'),'utf8'),scope);
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'verticals/manufacturer/web/orders.js'),'utf8'),scope);
  return scope.window.RocotoWorkspaceTabs;
}
const shell={vertical:{id:'manufacturer',revision:'one'},surfaces:[{id:'orders',title:'Orders'}]};
test('the actual manufacturer tab renders API rows and cleans up',async()=>{
  const calls=[];
  const sdk=harness(async(url,options)=>{calls.push([url,options.signal]);return {ok:true,json:async()=>({orders:[{id:'A-1',status:'quoted'}]})};});
  const root=node(), tabs=sdk.create({root,context:{},defaultHome:{mount(){}}});
  tabs.configure(shell,'company-a'); await tabs.open('orders');
  assert.equal(root.children[0].textContent,'Orders');
  assert.equal(root.children[1].children[0].textContent,'A-1: quoted');
  assert.equal(calls[0][0],'/api/manufacturer/orders');
  tabs.destroy(); assert.equal(calls[0][1].aborted,true); assert.equal(root.children.length,0);
});
test('a response from the previous company cannot populate the new company tab',async()=>{
  let finish;
  const sdk=harness(()=>new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>({orders:[{id:'private',status:'quoted'}]})});}));
  const root=node(), tabs=sdk.create({root,context:{},defaultHome:{mount(){}}});
  tabs.configure(shell,'company-a'); const pending=tabs.open('orders');
  tabs.configure(shell,'company-b'); finish(); await pending;
  assert.equal(root.children.length,0);
});
