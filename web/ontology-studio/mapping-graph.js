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
 if(!mappings.length&&!links.length){summary.textContent='当前任务没有映射契约；请先运行 Pipeline 的映射阶段。';detail.textContent='暂无映射节点';return}
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
   add(targetRels,rid,{id:rid,label:(schema.relation_types?.[rel.type]?.label||rel.type),kind:'ontology-relation',meta:{relation:rel.type,source:cls,target:rel.target,target_table:rel.target_table}});
   edges.push({from:fid,to:rid,label:'外键 → 关系'});
  }
 }
 for(const link of links){
  const rid='relation:'+link.type;
  add(targetRels,rid,{id:rid,label:(schema.relation_types?.[link.type]?.label||link.type),kind:'ontology-relation',meta:{relation:link.type,via_table:link.table,cardinality:'many_to_many'}});
  for(const item of [[link.source_column,link.source_table,'关联起点'],[link.target_column,link.target_table,'关联终点']]){
   const column=item[0],table=item[1],role=item[2],fid='field:'+norm(link.table,column);
   add(sourceFields,fid,{id:fid,label:column,kind:'source-field',meta:{table:link.table,column:column,role:role}});
   edges.push({from:fid,to:rid,label:role});
  }
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
 }
 inspect(left[0]||right[0],edges);
}
window.renderSemanticMappingGraph=render;
})();