const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
function sdk(){const scope={window:{},AbortController};vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../web/workspace-tabs.js'),'utf8'),scope);return scope.window.RocotoWorkspaceTabs;}
const shell=id=>({vertical:{id,revision:'one'},surfaces:[{id:'orders',title:'Orders'}]});
const root=()=>({replaceChildren(){},hidden:false});
test('a source-owned second vertical mounts in the same tab host',async()=>{
 const api=sdk(),seen=[];api.register('manufacturer',{orders:{mount:(_root,context)=>{seen.push(context.shell.vertical.id);return ()=>seen.push('unmounted');}}});
 const tabs=api.create({root:root(),context:{},defaultHome:{mount(){}}});tabs.configure(shell('manufacturer'),'workspace-one');await tabs.open('orders');assert.deepEqual(seen,['manufacturer']);tabs.destroy();assert.deepEqual(seen,['manufacturer','unmounted']);
});
test('switching workspace aborts old work and cleans a late mount',async()=>{
 const api=sdk();let finish,signal,cleaned=0;api.register('manufacturer',{orders:{mount:(_root,context)=>{signal=context.signal;return new Promise(resolve=>finish=()=>resolve(()=>cleaned++));}}});
 const tabs=api.create({root:root(),context:{},defaultHome:{mount(){}}});tabs.configure(shell('manufacturer'),'one');const mounting=tabs.open('orders');tabs.configure(shell('manufacturer'),'two');assert.equal(signal.aborted,true);finish();await mounting;assert.equal(cleaned,1);
});
test('bare Home mounts without any vertical registration; undeclared tabs fail',async()=>{
 const api=sdk();let homes=0;const tabs=api.create({root:root(),context:{},defaultHome:{mount(){homes++;}}});tabs.configure(shell('bare'),'one');await tabs.open();assert.equal(homes,1);await assert.rejects(tabs.open('foreign'));assert.throws(()=>api.register('bare',{orders:{}}));
});
