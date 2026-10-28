const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context={window:{}};
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../web/harness-shell.js'),'utf8'),context);
const shell=context.window.RocotoHarnessShell;
const source=classes=>({schema:'rocoto.harness-shell.v1',pack:{id:'fixture',revision:'one'},fixed_chats:[{purpose:'organization',title:'Organization',icon:'building'}],sections:[{key:'Organization',type:'fixed'},...classes.map(kind=>({key:kind,type:'agent_class',kind,default:true,authority:'granted_effects',create_label:'New '+kind,empty_label:'Your agents appear here.',starter_prompt:'Create '+kind,input_kinds:['none']})),{key:'Recent',type:'recent'}],surfaces:[]});
test('bare and an arbitrary vertical use precisely their declared classes',()=>{
 for(const kind of ['action','order_desk']){
  const parsed=shell.parse(source([kind])); assert.ok(parsed);
  assert.equal(parsed.sections.filter(x=>x.type==='agent_class').length,1);
  assert.equal(shell.defaultClass(parsed).kind,kind);
  assert.equal(shell.classFor(parsed,kind).kind,kind);
 }
});
test('ambiguous or malformed classes fail instead of silently dropping rows',()=>{
 for(const data of [source(['action','action']),source(['action','order_desk'])])assert.equal(shell.parse(data),null);
 const data=source(['order_desk']);data.sections[1].input_kinds=[];assert.equal(shell.parse(data),null);
});
test('loading shell has just the Organization chat and Recent',()=>{
 assert.equal(shell.fallback.fixed_chats[0].purpose,'organization');
 assert.equal(shell.fallback.sections.length,2);
 assert.equal(shell.defaultClass(shell.fallback),null);
});
