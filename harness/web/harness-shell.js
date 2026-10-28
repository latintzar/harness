/* Workspace navigation and agent classes come from the installed vertical. */
(() => {
  'use strict';
  const text=value=>typeof value==='string'&&value.trim().length>0;
  const key=value=>typeof value==='string'&&/^[a-z][a-z0-9_-]{0,79}$/.test(value);
  const unique=values=>new Set(values).size===values.length;
  const fallback=Object.freeze({standIn:true,fixed_chats:[{purpose:'organization',title:'Organization',icon:'building',opens:'chat'}],sections:[{key:'Organization',type:'fixed'},{key:'Recent',type:'recent'}],surfaces:[]});
  function parse(data) {
    if(data?.schema!=='rocoto.harness-shell.v1'||!Array.isArray(data.fixed_chats)||!Array.isArray(data.sections)||!Array.isArray(data.surfaces))return null;
    const fixed=data.fixed_chats;
    if(!fixed.length||fixed.some(row=>!key(row?.purpose)||!text(row.title))||!unique(fixed.map(row=>row.purpose))||!fixed.some(row=>row.purpose==='organization'))return null;
    const sections=data.sections,classes=sections.filter(row=>row?.type==='agent_class');
    if(!classes.length||sections.some(row=>!text(row?.key)||!['fixed','recent','agent_class'].includes(row.type))||!unique(sections.map(row=>row.key)))return null;
    if(['fixed','recent'].some(type=>sections.filter(row=>row.type===type).length!==1))return null;
    if(classes.some(row=>!key(row.kind)||!text(row.create_label)||!text(row.empty_label)||!text(row.starter_prompt)||!Array.isArray(row.input_kinds)||!row.input_kinds.length||row.input_kinds.some(value=>!key(value)))||!unique(classes.map(row=>row.kind))||classes.filter(row=>row.default===true).length!==1)return null;
    const surfaces=data.surfaces;
    if(surfaces.some(row=>!key(row?.id)||!text(row.title)||!['primary','settings','secondary'].includes(row.nav))||!unique(surfaces.map(row=>row.id)))return null;
    return {schema:data.schema,vertical:data.vertical||data.pack||null,product:data.product||{},
      fixed_chats:fixed.map(row=>({purpose:row.purpose,title:row.title,icon:row.icon,job:text(row.job)?row.job:'',opens:row.opens==='member_setup'?'member_setup':'chat'})),
      sections:sections.map(row=>({...row})),surfaces:surfaces.map(row=>({...row}))};
  }
  const defaultClass=shell=>shell.sections.find(row=>row.type==='agent_class'&&row.default===true)||null;
  const classFor=(shell,kind)=>shell.sections.find(row=>row.type==='agent_class'&&row.kind===kind)||null;
  window.RocotoHarnessShell=Object.freeze({parse,fallback,defaultClass,classFor});
})();
