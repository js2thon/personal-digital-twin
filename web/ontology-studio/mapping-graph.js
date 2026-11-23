(()=>{'use strict';
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const svg=(tag,attrs={},label='')=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));if(label){if(tag==='text')n.textContent=label;else{const t=document.createElementNS('http://www.w3.org/2000/svg','title');t.textContent=label;n.append(t)}}return n};
const norm=(table,column)=>table+'.'+column;
function cardinalityOf(spec){const raw=String(spec?.cardinality||spec?.relation_cardinality||spec?.mapping_type||spec?.kind||'').toLowerCase().replace(/[\s-]/g,'_');if(['many_to_many','n_to_n','n:n','*:*','m:n'].includes(raw)||spec?.via_table||spec?.junction_table)return'many_to_many';if(['many_to_one','n_to_one','n:1','* : 1','m:1'].includes(raw))return'many_to_one';if(['one_to_many','1:n','1:*'].includes(raw))return'one_to_many';return'';}
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
 // Include schema object types even before any source table has been mapped.
 for(const [cls,entity] of Object.entries(schema.entity_types||{})){
  add(targetObjects,cls,{id:'class:'+cls,label:entity.label||cls,kind:'ontology-class',meta:{class:cls,technical_name:entity.technical_name||cls}});
 }
 // Keep schema relations selectable even when their contract mapping is missing.
 for(const [rn,spec] of Object.entries(schema.relation_types||{})){
  add(targetRels,'relation:'+rn,{id:'relation:'+rn,label:(spec.label||rn),kind:'ontology-relation',
   meta:{relation:rn,source:spec.source,target:spec.target,table:spec.table,via_table:spec.via_table||spec.junction_table,cardinality:cardinalityOf(spec)}});
 }
 const columns=[
  {title:'数据源对象',items:[...sourceTables.values()],x:145,color:'#f59e0b'},
  {title:'源字段',items:[...sourceFields.values()],x:390,color:'#f59e0b'},
  {title:'数据类型',items:[],x:625,color:'#94a3b8'},
  {title:'映射规则',items:[],x:860,color:'#eab308'},
  {title:'本体属性 / 关系',items:[...targetProps.values(),...targetRels.values()],x:1110,color:'#38bdf8'},
  {title:'本体对象',items:[...targetObjects.values()],x:1370,color:'#3b82f6'}
 ];
 const allNodes=new Map(),drawEdges=[];
 const pushNode=(col,node)=>{if(!allNodes.has(node.id)){allNodes.set(node.id,{...node,column:col});columns[col].items.push(allNodes.get(node.id))}return allNodes.get(node.id)};
 // Existing source and ontology nodes are placed into their semantic columns.
 columns[0].items.forEach(n=>allNodes.set(n.id,{...n,column:0}));
 columns[1].items.forEach(n=>allNodes.set(n.id,{...n,column:1}));
 columns[4].items.forEach(n=>allNodes.set(n.id,{...n,column:4}));
 columns[5].items.forEach(n=>allNodes.set(n.id,{...n,column:5}));
 // A field-level data type and mapping-rule node makes the mapping path explicit.
 for(const field of [...sourceFields.values()]){
  const typeId='type:'+field.id;
  const profile=job.profiles?.[field.meta?.table]?.columns?.[field.meta?.column]||{};
  const typeLabel=profile.dtype||field.meta?.data_type||'待识别';
  pushNode(2,{id:typeId,label:typeLabel,kind:'data-type',meta:{data_type:typeLabel,table:field.meta?.table,column:field.meta?.column}});
  drawEdges.push({from:field.id,to:typeId,style:'solid'});
 }
 const propertyEdges=edges.filter(e=>e.to.startsWith('property:'));
 const relationEdges=edges.filter(e=>e.to.startsWith('relation:'));
 for(const e of propertyEdges){
  const ruleId='rule:'+e.from+':'+e.to;
  pushNode(3,{id:ruleId,label:'字段映射',kind:'mapping-rule',meta:{rule:'字段映射',source:e.from,target:e.to}});
  drawEdges.push({from:e.from,to:'type:'+e.from,style:'solid'},{from:'type:'+e.from,to:ruleId,style:'dashed'},{from:ruleId,to:e.to,style:'dashed'});
 }
 for(const e of relationEdges){
  const ruleId='rule:'+e.from+':'+e.to;
  const relation=targetRels.get(e.to.replace('relation:',''));
  const relName=relation?.label||'关系';
  pushNode(3,{id:ruleId,label:e.label||'关系映射',kind:'mapping-rule',meta:{rule:e.label||'关系映射',relation:relName}});
  drawEdges.push({from:e.from,to:'type:'+e.from,style:'solid'},{from:'type:'+e.from,to:ruleId,style:'dashed'},{from:ruleId,to:e.to,style:'dashed'});
 }
 for(const e of edges.filter(e=>e.from.startsWith('table:')&&e.to.startsWith('class:'))){
  const ruleId='rule:'+e.from+':'+e.to;
  pushNode(3,{id:ruleId,label:'对象映射',kind:'mapping-rule',meta:{rule:'数据表 → 本体对象'}});
  drawEdges.push({from:e.from,to:ruleId,style:'dashed'},{from:ruleId,to:e.to,style:'dashed'});
 }
 // Connect ontology properties to their owning object type and relations to their target object.
 for(const p of targetProps.values()){
  const cls=p.meta?.class;if(cls&&targetObjects.has(cls))drawEdges.push({from:p.id,to:'class:'+cls,style:'solid'});
 }
 for(const r of targetRels.values()){
  const source=r.meta?.source,target=r.meta?.target;
  if(source&&targetObjects.has(source))drawEdges.push({from:r.id,to:'class:'+source,style:'relation'});
  if(target&&targetObjects.has(target))drawEdges.push({from:r.id,to:'class:'+target,style:'relation'});
 }
 // Table-to-field hierarchy is explicit and remains visible even before mappings exist.
 for(const field of sourceFields.values()){
  const table='table:'+field.meta?.table;
  if(allNodes.has(table))drawEdges.push({from:table,to:field.id,style:'solid'});
 }
 const maxRows=Math.max(1,...columns.map(c=>c.items.length));
 const H=Math.max(650,maxRows*76+150),W=1530;
 graph.setAttribute('viewBox','0 0 '+W+' '+H);graph.setAttribute('height',String(Math.min(H,1250)));
 const positions=new Map();
 columns.forEach((col,ci)=>{
  col.items.forEach((n,i)=>positions.set(n.id,{x:col.x,y:105+i*(H-155)/Math.max(1,col.items.length-1),node:n,column:ci}));
 });
 const headerLayer=svg('g'),edgeLayer=svg('g'),nodeLayer=svg('g');
 columns.forEach(col=>{
  headerLayer.append(svg('text',{x:col.x,y:35,'text-anchor':'middle',fill:'#cbd5e1','font-size':15,'font-weight':700},col.title));
  headerLayer.append(svg('line',{x1:col.x-88,y1:49,x2:col.x+88,y2:49,stroke:col.color,'stroke-width':2,opacity:.85}));
  if(!col.items.length)headerLayer.append(svg('text',{x:col.x,y:88,'text-anchor':'middle',fill:'#64748b','font-size':11},'暂无节点'));
 });
 for(const e of drawEdges){
  const a=positions.get(e.from),b=positions.get(e.to);if(!a||!b)continue;
  const sx=a.x+91,tx=b.x-91,mid=(sx+tx)/2;
  const stroke=e.style==='relation'?'#34d399':e.style==='dashed'?'#60a5fa':'#64748b';
  edgeLayer.append(svg('path',{d:'M '+sx+' '+a.y+' C '+mid+' '+a.y+', '+mid+' '+b.y+', '+tx+' '+b.y,fill:'none',stroke,'stroke-width':e.style==='relation'?2.4:1.6,'stroke-dasharray':e.style==='dashed'?'7 6':'none',opacity:.78}));
 }
 const palette={'source-table':'#f59e0b','source-field':'#fb923c','data-type':'#94a3b8','mapping-rule':'#eab308','ontology-class':'#2563eb','ontology-property':'#38bdf8','ontology-relation':'#34d399'};
 for(const [id,p] of positions){
  const n=p.node,color=palette[n.kind]||'#64748b',g=svg('g',{class:'smg-node',tabindex:0,role:'button','data-node':id});
  g.append(svg('rect',{x:p.x-91,y:p.y-23,width:182,height:46,rx:8,fill:'#111c2e',stroke:color,'stroke-width':1.6}));
  g.append(svg('rect',{x:p.x-91,y:p.y-23,width:5,height:46,rx:3,fill:color,stroke:'none'}));
  const label=String(n.label||id);g.append(svg('text',{x:p.x-78,y:p.y+4,'text-anchor':'start',fill:'#e2e8f0','font-size':12},label.length>22?label.slice(0,20)+'…':label));
  g.append(svg('title',{},label+' · '+n.kind));
  g.addEventListener('click',()=>inspect(n,edges));
  g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();inspect(n,edges)}});
  nodeLayer.append(g);
 }
 graph.append(headerLayer,edgeLayer,nodeLayer);
 summary.textContent=columns[0].items.length+' 个数据源对象 · '+columns[1].items.length+' 个源字段 · '+columns[2].items.length+' 个类型 · '+columns[3].items.length+' 条映射规则 · '+columns[4].items.length+' 个本体属性/关系 · '+columns[5].items.length+' 个本体对象';
 function inspect(n,allEdges){
  const connected=allEdges.filter(e=>e.from===n.id||e.to===n.id),m=n.meta||{};
  detail.innerHTML='<b>'+esc(n.label)+'</b><p class="smg-kind">'+esc(n.kind)+'</p><dl>'+
   Object.entries(m).map(([k,v])=>'<dt>'+esc(k)+'</dt><dd>'+esc(Array.isArray(v)?v.join(', '):v)+'</dd>').join('')+
   '</dl><p class="smg-kind">关联映射：'+connected.length+'</p>';
  if(n.kind==='source-field'){
   const mapping=(job.contract?.mappings||[]).find(x=>String(x.table)===String(m.table));
   const classes=Object.entries(schema.entity_types||{});
   const currentClass=mapping?.class||'';
   const attrsFor=cls=>Object.entries(schema.attributes||{}).filter(([key,a])=>a.owner===cls);
   const current=Object.entries(mapping?.attributes||{}).find(([key,col])=>String(col)===String(m.column))?.[0]||'';
   const classControl=mapping?'<input type="hidden" id="smgTargetClass" value="'+esc(currentClass)+'">':'<label for="smgTargetClass">目标本体对象</label><select id="smgTargetClass" style="width:100%;margin:6px 0"><option value="">请选择对象类型</option>'+classes.map(([key,v])=>'<option value="'+esc(key)+'">'+esc(v.label||key)+'</option>').join('')+'</select>';
   detail.insertAdjacentHTML('beforeend','<hr><p class="smg-kind">将源字段映射到本体属性；尚未建立对象映射的源表也可在此创建草稿。</p>'+classControl+'<label for="smgProperty">目标属性</label><select id="smgProperty" style="width:100%;margin:6px 0"><option value="">不映射</option>'+attrsFor(currentClass).map(([key,a])=>'<option value="'+esc(key)+'" '+(key===current?'selected':'')+'>'+esc(a.label||key)+' · '+esc(key)+'</option>').join('')+'</select><button id="smgApply" class="primary" style="width:100%">应用到草稿</button>');
   const fillProperties=()=>{const cls=$('smgTargetClass').value,items=attrsFor(cls);$('smgProperty').innerHTML='<option value="">不映射</option>'+items.map(([key,a])=>'<option value="'+esc(key)+'">'+esc(a.label||key)+' · '+esc(key)+'</option>').join('')};
   if(!mapping)$('smgTargetClass').onchange=fillProperties;
   $('smgApply').onclick=()=>{if(window.applySemanticMappingEdit)window.applySemanticMappingEdit({table:m.table,column:m.column,property:$('smgProperty').value,class:$('smgTargetClass').value});};
  }else if(n.kind==='ontology-relation'){
   const spec=schema.relation_types?.[m.relation]||{},profiles=job.profiles||{},maps=job.contract?.mappings||[];
   const option=(value,label)=>'<option value="'+esc(value)+'">'+esc(label)+'</option>';
   const select=(id,label,items,selected)=>'<label for="'+id+'">'+label+'</label><select id="'+id+'" style="width:100%;margin:5px 0 9px"><option value="">请选择</option>'+items.map(x=>option(x.value,x.label)).join('')+'</select>';
   const set=(id,value)=>{if($(id))$(id).value=value||''};
   if(m.cardinality==='many_to_one'){
    const mappedSource=maps.find(x=>x.class===m.source)?.table||'';
    const sourceTable=m.table||m.source_table||spec.table||mappedSource;
    const current=maps.flatMap(x=>(x.relations||[]).map(r=>({...r,_table:x.table}))).find(x=>x.type===m.relation)||{};
    const allTables=Object.keys(profiles).map(x=>({value:x,label:x}));
    const sourceOptions=allTables.length?allTables:maps.filter(x=>x.class===m.source).map(x=>({value:x.table,label:x.table}));
    const targets=maps.filter(x=>x.class===m.target).map(x=>x.table);
    const targetOptions=allTables.length?allTables:targets.map(x=>({value:x,label:x}));
    const fields=table=>Object.keys(profiles[table]?.columns||{}).map(x=>({value:x,label:x}));
    detail.insertAdjacentHTML('beforeend','<hr><b>编辑多对一关系映射</b><p class="smg-kind">先选择关系起点源表，再选择外键字段和目标对象数据表；尚未映射的表也可先配置。</p>'+
     select('smgRelSourceTable','关系起点 / 外键所在表',sourceOptions,sourceTable||current._table)+
     select('smgRelColumn','源外键字段',fields(sourceTable||current._table),current.column)+
     select('smgRelTargetTable','目标对象数据表',targetOptions,current.target_table||targets[0])+
     '<button id="smgRelApply" class="primary" style="width:100%">应用关系草稿</button>');
    set('smgRelSourceTable',sourceTable||current._table);set('smgRelColumn',current.column);set('smgRelTargetTable',current.target_table||targets[0]);
    $('smgRelSourceTable').onchange=()=>{$('smgRelColumn').innerHTML='<option value="">请选择</option>'+fields($('smgRelSourceTable').value).map(x=>option(x.value,x.label)).join('')};
    $('smgRelApply').onclick=()=>window.applySemanticRelationEdit?.({kind:'many_to_one',relation:m.relation,table:$('smgRelSourceTable').value,column:$('smgRelColumn').value,target_table:$('smgRelTargetTable').value});
   }else if(m.cardinality==='many_to_many'){
    const current=(job.contract?.links||[]).find(x=>x.type===m.relation)||{};
    const classTables=cls=>{const mapped=maps.filter(x=>x.class===cls).map(x=>({value:x.table,label:x.table}));return mapped.length?mapped:Object.keys(profiles).map(table=>({value:table,label:table+' · 待绑定对象'}))};
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
   }else{
    detail.insertAdjacentHTML('beforeend','<hr><b>配置关系映射类型</b><p class="smg-kind">该关系未声明可识别的基数。请选择映射方式后继续配置。</p><select id="smgCardinality" style="width:100%;margin:6px 0"><option value="">请选择</option><option value="many_to_one">多对一（源表外键）</option><option value="many_to_many">多对多（关联表）</option></select><button id="smgCardinalityApply" class="primary" style="width:100%">继续配置</button>');
    $('smgCardinalityApply').onclick=()=>{m.cardinality=$('smgCardinality').value;if(m.cardinality)inspect(n,allEdges)};
   }
  }
 }
 const first=[...sourceFields.values()][0]||[...targetRels.values()][0]||[...sourceTables.values()][0]||[...targetObjects.values()][0];
 if(first)inspect(first,edges);else detail.textContent='暂无可选择的映射节点';
}
window.renderSemanticMappingGraph=render;
})();