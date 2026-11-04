(()=>{
 const $=id=>document.getElementById(id), upload=$('inbox-upload'), input=$('inbox-files'), drop=$('inbox-drop');
 let chosen=[],plan=null,running=false,uploading=false,timer=null;
 function controls(){ $('import-all').disabled=running||uploading||!plan?.count; $('refresh-plan').disabled=running||uploading; upload.querySelector('button').disabled=running||uploading; }
 function select(files){chosen=Array.from(files);$('upload-selection').textContent=chosen.length+' files selected';}
 input.addEventListener('change',()=>select(input.files));
 for(const event of ['dragenter','dragover'])drop.addEventListener(event,e=>{e.preventDefault();drop.classList.add('dragging')});
 drop.addEventListener('dragleave',()=>drop.classList.remove('dragging'));
 drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('dragging');select(e.dataTransfer.files);input.required=false});
 async function json(url,options){const r=await fetch(url,options);const d=await r.json();if(!r.ok)throw Error(d.detail||d.error||'Request failed');return d;}
 function el(tag,text){const n=document.createElement(tag);n.textContent=text;return n;}
 async function preview(){
  $('plan-summary').textContent='Inspecting arrivals and finding their shelves…';$('import-all').disabled=true;
  try{plan=await json('/api/inbox/plan');$('placement-plan').replaceChildren();
   const uncertain=plan.groups.filter(g=>g.needs_review).length;
   $('plan-summary').textContent=plan.count+' inbox items → '+plan.groups.length+' collections'+(uncertain?' · '+uncertain+(uncertain===1?' collection will':' collections will')+' go to Unsorted':'')+(plan.review.length?' · '+plan.review.length+' items cannot be imported':'');
   for(const g of plan.groups){const card=el('details','');card.className='inbox-plan';card.append(el('summary',g.title+' · '+g.items.length+(g.items.length===1?' inbox item':' inbox items')+(g.needs_review?' · Needs review':'')),el('p','Shelf: '+g.shelf.replace(/^\d+-/,'').replace(/^_/,'').replaceAll('-',' ')),el('p',g.reason));const list=el('ul','');for(const name of g.items)list.append(el('li',name));card.append(list);$('placement-plan').append(card);}
   for(const item of plan.review)$('placement-plan').append(el('p',item.name+' — '+item.reason));
  }catch(e){plan=null;$('plan-summary').textContent=e.message;}controls();
 }
 function sendFile(file,index,total){return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/inbox/upload?filename='+encodeURIComponent(file.name));xhr.setRequestHeader('X-Library-Request','1');xhr.upload.onprogress=e=>{const fraction=e.lengthComputable?e.loaded/e.total:0;$('upload-progress').value=100*(index+fraction)/total;$('upload-status').textContent='Uploading '+(index+1)+' of '+total+': '+file.name+' · '+Math.round(fraction*100)+'%';};xhr.onload=()=>{let d;try{d=JSON.parse(xhr.responseText)}catch{d={detail:'Upload failed'}}if(xhr.status>=200&&xhr.status<300)resolve();else reject(Error(d.detail||'Upload failed'));};xhr.onerror=()=>reject(Error('Upload connection failed.'));xhr.send(file);});}
 upload.addEventListener('submit',async e=>{e.preventDefault();if(!chosen.length||running||uploading)return;uploading=true;controls();$('upload-progress').hidden=false;let ok=0,errors=[];const files=[...chosen];for(let i=0;i<files.length;i++){if(files[i].size>2*1024**3){errors.push(files[i].name+': exceeds 2 GB');continue;}try{await sendFile(files[i],i,files.length);ok++;}catch(err){errors.push(files[i].name+': '+err.message);}}$('upload-progress').value=100;$('upload-status').textContent=ok+' files uploaded.'+(errors.length?' '+errors.join('; '):' Ready for automatic placement.');uploading=false;input.value='';chosen=[];$('upload-selection').textContent='';await preview();});
 async function status(){
  try{const state=await json('/api/inbox/status');const previous=running;running=!!state.running;$('import-message').textContent=state.message||'';$('import-error').hidden=!state.error;$('import-error').textContent=state.error||'';$('import-progress').hidden=!running;if(running){$('import-progress').max=state.batch_total||1;$('import-progress').value=state.batch_completed||0;}$('import-results').replaceChildren();for(const item of state.result_collections||[]){const link=el('a',item.name+' →');link.className='quiet-button';link.href='/collection/'+item.id;$('import-results').append(link);}controls();if(running){clearTimeout(timer);timer=setTimeout(status,1500);}else if(previous)await preview();
  }catch(e){$('import-error').hidden=false;$('import-error').textContent='Cannot read import status: '+e.message;clearTimeout(timer);timer=setTimeout(status,4000);}
 }
 $('refresh-plan').addEventListener('click',preview);
 $('import-all').addEventListener('click',async()=>{if(running||!plan?.count)return;running=true;controls();$('import-message').textContent='Starting automatic import…';try{await json('/api/inbox/process',{method:'POST',headers:{'Content-Type':'application/json','X-Library-Request':'1'},body:JSON.stringify({automatic:true,groups:plan.groups})});await status();}catch(e){running=false;$('import-error').hidden=false;$('import-error').textContent=e.message;controls();}});
 status();preview();
})();
