(()=>{'use strict';
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const svg=(tag,attrs={},label='')=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));if(label){if(tag==='text')n.textContent=label;else{const t=document.createElementNS('http://www.w3.org/2000/svg','title');t.textContent=label;n.append(t)}}return n};
const norm=(table,column)=>table+'.'+column;
function render(job){
 const root=$('semanticMappingGraph');if(!root)return;
 const contract=job?.contract||{},schema=job?.draft?.schema||job?.schema||{};
 const mappings=contract.mappings||[],links=contract.links||[];
 const graph=$('semanticMappingSvg'),detail=$('semanticMappingDetail'),summary=$('semanticMappingSummary');
 graph.replaceChildren();
 if(!mappings.length&&!links.length&&!Object.keys(schema.relation_types||{}).length&&!Object.keys(job.profiles||{}).length){summary.textContent='当前任务尚无源数据画像或关系定义；请先运行本体构建与数据发现阶段。';detail.textContent='暂无可配置的关系映射';return}
 const sourceTables=new Map(),sourceFields=new Map(),targetObjects=new Map(),targetProps=new Map(),targetRels=new Map(),edges=[];
 const add=(map,id,node)=>{if(!map.has(id))map.set(id,node);return map.get(id)};
 for(const m of mappings){
  const table=String(m.table||'unknown'),cls=String(m.class||'unknown');
  add(sourceTables,table,{id:'table:'+table,label:table,kind:'source-table',meta:{table:table,primary_key:m.primary_key||[]}});
  add(targetObjects,cls,{id:'class:'+cls,label:(schema.entity_types?.[cls]?.label||cls),kind:'ontology-class',meta:{class:cls,technical_name:schema.entity_types?.[cls]?.technical_name||cls}});
  edges.push({from:'table:'+table,to:'class:'+cls,label:'映射到对象'});
  for(const key of m.primary_key||[]){const id='field:'+norm(table,key);add(sourceFields,id,{id:id,label:key,kind:'source-field',meta:{table:table,column:key,role:'主键'}})}
  for(const [prop,col] of Object.entries(m.attributes||{})){
   const fid='field:'+norm(table,col),pid='property:'+cls+'.'+prop;
   add(sourceFields,fid,{id:fid,label:col,kind:'source-field',meta:{table:table,column:col,role:'属性来源'}});
   add(targetProps,pid,{id:pid,label:(schema.attributes?.[prop]?.label||prop),kind:'ontology-property',meta:{class:cls,property:prop,range:schema.attributes?.[prop]?.range||'未声明'}});
   edges.push({from:fid,to:pid,label:'字段映射'});
  }
  for(const rel of m.relations||[]){
   const fid='field:'+norm(table,rel.column),rid='relation:'+rel.type;
   add(sourceFields,fid,{id:fid,label:rel.column,kind:'source-field',meta:{table:table,column:rel.column,role:'外键'}});
   add(targetRels,rid,{id:rid,label:(schema.relation_types?.[rel.type]?.label||rel.type),kind:'ontology-relation',meta:{relation:rel.type,source:cls,target:rel.target,target_table:rel.target_table,source_table:table,cardinality:'many_to_one'}});
   edges.push({from:fid,to:rid,label:'外键 → 关系'});
  }
 }
 // Include every discovered source column, not only columns already mapped, so users can add mappings.
 for(const [table,profile] of Object.entries(job.profiles||{})){
  add(sourceTables,table,{id:'table:'+table,label:table,kind:'source-table',meta:{table:table,primary_key:profile.primary_key||[]}});
  for(const [column,info] of Object.entries(profile.columns||{})){
   const id='field:'+norm(table,column);
   add(sourceFields,id,{id:id,label:column,kind:'source-field',meta:{table:table,column:column,role:info.role||'未映射字段',data_type:info.dtype||'unknown'}});
  }
 }
 for(const link of links){
  const rid='relation:'+link.type;
  add(targetRels,rid,{id:rid,label:(schema.relation_types?.[link.type]?.label||link.type),kind:'ontology-relation',meta:{relation:link.type,via_table:link.table,source: schema.relation_types?.[link.type]?.source,target: schema.relation_types?.[link.type]?.target,cardinality:'many_to_many'}});
  for(const item of [[link.source_column,link.source_table,'关联起点'],[link.target_column,link.target_table,'关联终点']]){
   const column=item[0],table=item[1],role=item[2],fid='field:'+norm(link.table,column);
   add(sourceFields,fid,{id:fid,label:column,kind:'source-field',meta:{table:link.table,column:column,role:role}});
   edges.push({from:fid,to:rid,label:role});
  }
 }
 // Keep schema relations selectable even when their contract mapping is missing.
 for(const [rn,spec] of Object.entries(schema.relation_types||{})){
  add(targetRels,'relation:'+rn,{id:'relation:'+rn,label:(spec.label||rn),kind:'ontology-relation',
   meta:{relation:rn,source:spec.source,target:spec.target,table:spec.table,via_table:spec.via_table,cardinality:spec.cardinality||''}});
 }
 const W=1200,H=Math.max(560,Math.max(sourceTables.size+sourceFields.size,targetObjects.size+targetProps.size+targetRels.size)*74+80);
 graph.setAttribute('viewBox','0 0 '+W+' '+H);graph.setAttribute('height',String(Math.min(H,1100)));
 const left=[...sourceTables.values(),...sourceFields.values()],right=[...targetObjects.values(),...targetProps.values(),...targetRels.values()];
 const positions=new Map();
 const place=(items,x)=>items.forEach((n,i)=>positions.set(n.id,{x:x,y:45+i*(H-90)/Math.max(1,items.length-1),node:n}));
 place(left,235);place(right,965);
 const edgeLayer=svg('g'),nodeLayer=svg('g');
 for(const e of edges){const a=positions.get(e.from),b=positions.get(e.to);if(!a||!b)continue;const mid=(a.x+b.x)/2;edgeLayer.append(svg('path',{d:'M '+(a.x+95)+' '+a.y+' C '+mid+' '+a.y+', '+mid+' '+b.y+', '+(b.x-105)+' '+b.y,fill:'none',stroke:'#8293a8','stroke-width':1.5,opacity:.75}))}
 const palette={'source-table':'#0f766e','source-field':'#0d9488','ontology-class':'#2563eb','ontology-property':'#4f46e5','ontology-relation':'#9333ea'};
 for(const [id,p] of positions){
  const n=p.node,g=svg('g',{class:'smg-node',tabindex:0,role:'button','data-node':id});
  g.append(svg('rect',{x:p.x-94,y:p.y-22,width:188,height:44,rx:9,fill:palette[n.kind]+'18',stroke:palette[n.kind],'stroke-width':1.6}));
  const label=String(n.label||id);g.append(svg('text',{x:p.x,y:p.y+4,'text-anchor':'middle',fill:'currentColor','font-size':12},label.length>24?label.slice(0,22)+'…':label));
  g.addEventListener('click',()=>inspect(n,edges));
  g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();inspect(n,edges)}});
  nodeLayer.append(g);
 }
 graph.append(edgeLayer,nodeLayer);
 summary.textContent=sourceTables.size+' 张源表 · '+sourceFields.size+' 个源字段 · '+targetObjects.size+' 个本体对象 · '+targetProps.size+' 个属性映射 · '+targetRels.size+' 个关系映射';
 function inspect(n,allEdges){
  const connected=allEdges.filter(e=>e.from===n.id||e.to===n.id),m=n.meta||{};
  detail.innerHTML='<b>'+esc(n.label)+'</b><p class="smg-kind">'+esc(n.kind)+'</p><dl>'+
   Object.entries(m).map(([k,v])=>'<dt>'+esc(k)+'</dt><dd>'+esc(Array.isArray(v)?v.join(', '):v)+'</dd>').join('')+
   '</dl><p class="smg-kind">关联映射：'+connected.length+'</p>';
  if(n.kind==='source-field'){
   const mapping=(job.contract?.mappings||[]).find(x=>String(x.table)===String(m.table));
   const attrs=Object.entries(schema.attributes||{}).filter(([key,a])=>a.owner===mapping?.class);
   const current=Object.entries(mapping?.attributes||{}).find(([key,col])=>String(col)===String(m.column))?.[0]||'';
   if(mapping){
    detail.insertAdjacentHTML('beforeend','<hr><p class="smg-kind">调整字段对应的本体属性（仅修改当前草稿）</p><label for="smgProperty">目标属性</label><select id="smgProperty" style="width:100%;margin:6px 0"><option value="">不映射</option>'+attrs.map(([key,a])=>'<option value="'+esc(key)+'" '+(key===current?'selected':'')+'>'+esc(a.label||key)+' · '+esc(key)+'</option>').join('')+'</select><button id="smgApply" class="primary" style="width:100%">应用到草稿</button>');
    $('smgApply').onclick=()=>{if(window.applySemanticMappingEdit)window.applySemanticMappingEdit({table:m.table,column:m.column,property:$('smgProperty').value});};
   }
  }else if(n.kind==='ontology-relation'){
   const spec=schema.relation_types?.[m.relation]||{},profiles=job.profiles||{},maps=job.contract?.mappings||[];
   const option=(value,label)=>'<option value="'+esc(value)+'">'+esc(label)+'</option>';
   const select=(id,label,items,selected)=>'<label for="'+id+'">'+label+'</label><select id="'+id+'" style="width:100%;margin:5px 0 9px"><option value="">请选择</option>'+items.map(x=>option(x.value,x.label)).join('')+'</select>';
   const set=(id,value)=>{if($(id))$(id).value=value||''};
   if(m.cardinality==='many_to_one'){
    const sourceTable=m.table||m.source_table||spec.table||'';
    const sourceProfile=profiles[sourceTable]||{};
    const targets=maps.filter(x=>x.class===m.target).map(x=>x.table);
    const current=maps.find(x=>x.table===sourceTable)?.relations?.find(x=>x.type===m.relation)||{};
    const fields=Object.keys(sourceProfile.columns||{}).map(x=>({value:x,label:x}));
    detail.insertAdjacentHTML('beforeend','<hr><b>编辑多对一关系映射</b><p class="smg-kind">修改源外键列与目标对象对应的数据表</p>'+
     select('smgRelColumn','源外键字段',fields,current.column)+
     select('smgRelTargetTable','目标对象数据表',targets.map(x=>({value:x,label:x})),current.target_table||targets[0])+
     '<button id="smgRelApply" class="primary" style="width:100%">应用关系草稿</button>');
    set('smgRelColumn',current.column);set('smgRelTargetTable',current.target_table||targets[0]);
    $('smgRelApply').onclick=()=>window.applySemanticRelationEdit?.({kind:'many_to_one',relation:m.relation,table:sourceTable,column:$('smgRelColumn').value,target_table:$('smgRelTargetTable').value});
   }else if(m.cardinality==='many_to_many'){
    const current=(job.contract?.links||[]).find(x=>x.type===m.relation)||{};
    const classTables=cls=>maps.filter(x=>x.class===cls).map(x=>({value:x.table,label:x.table}));
    const junctions=Object.entries(profiles).map(([table,p])=>({value:table,label:table+' · '+Object.keys(p.columns||{}).length+' 列'}));
    const fields=table=>Object.keys(profiles[table]?.columns||{}).map(x=>({value:x,label:x}));
    const defaultTable=current.table||m.via_table||spec.via_table||'';
    detail.insertAdjacentHTML('beforeend','<hr><b>编辑多对多关系映射</b><p class="smg-kind">选择关联表、两端对象表及对应外键列</p>'+
     select('smgJunctionTable','关联表',junctions,defaultTable)+
     select('smgSourceTable','关系起点数据表',classTables(m.source),current.source_table)+
     select('smgSourceColumn','关联表中的起点外键',fields(defaultTable),current.source_column)+
     select('smgTargetTable','关系终点数据表',classTables(m.target),current.target_table)+
     select('smgTargetColumn','关联表中的终点外键',fields(defaultTable),current.target_column)+
     '<button id="smgRelApply" class="primary" style="width:100%">应用关系草稿</button>');
    set('smgJunctionTable',defaultTable);set('smgSourceTable',current.source_table);set('smgSourceColumn',current.source_column);set('smgTargetTable',current.target_table);set('smgTargetColumn',current.target_column);
    $('smgJunctionTable').onchange=()=>{const table=$('smgJunctionTable').value;const fill=id=>{const old=$(id).value;$(id).innerHTML='<option value="">请选择</option>'+fields(table).map(x=>option(x.value,x.label)).join('');if(fields(table).some(x=>x.value===old))$(id).value=old};fill('smgSourceColumn');fill('smgTargetColumn')};
    $('smgRelApply').onclick=()=>window.applySemanticRelationEdit?.({kind:'many_to_many',relation:m.relation,table:$('smgJunctionTable').value,source_table:$('smgSourceTable').value,source_column:$('smgSourceColumn').value,target_table:$('smgTargetTable').value,target_column:$('smgTargetColumn').value});
   }
  }
 }
 inspect(left[0]||right[0],edges);
}
window.renderSemanticMappingGraph=render;
})();