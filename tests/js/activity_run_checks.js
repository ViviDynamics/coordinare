'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
class El {
  constructor(){this.style={};this.innerHTML='';this.textContent='';this.value='';this.attrs={};this.classList={add(){},remove(){}};}
  get childElementCount(){return (this.innerHTML.match(/class="af-row"/g)||[]).length;}
  setAttribute(k,v){this.attrs[k]=v;}
  getAttribute(k){return this.attrs[k];}
  insertAdjacentHTML(position,html){assert.equal(position,'afterbegin');this.innerHTML=html+this.innerHTML;}
}
const elements={};
['activity-feed','af-empty','af-filter','af-liveness','af-stale-note','nav-sse-dot','disconnected-banner','force-poll-btn'].forEach(id=>elements[id]=new El());
const sources=[];
class Source {
  static CLOSED=2;
  constructor(){this.readyState=1;this.listeners={};sources.push(this);}
  addEventListener(k,f){this.listeners[k]=f;}
  emit(k,data){this.listeners[k]({data:JSON.stringify(data)});}
  close(){this.readyState=2;}
}
const c={document:{getElementById:id=>elements[id]},EventSource:Source,console,location:{pathname:'/'},setTimeout(){return 1;},clearTimeout(){},renderState(){},renderDashboardExtras(){},renderActivePerformers(){},renderPerformersPage(){},renderHistoryPage(){},renderSymphoniesPage(){}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),c);
const [oldRun,newRun]=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
function send(wire){sources.at(-1).emit('state_update',wire.state);sources.at(-1).emit('activity_event',{entries:wire.entries});}
send(oldRun);
assert.equal(c._afEntries.length,1);
const cases={
 restart(){send(newRun);assert.equal(c._afEntries.length,1);assert.equal(c._afEntries[0].text,'run-1','New producer activity must survive a reused sequence');assert.ok(!elements['activity-feed'].innerHTML.includes('run-0'));},
 same(){const html=elements['activity-feed'].innerHTML;send(oldRun);assert.equal(c._afEntries.length,1);assert.equal(elements['activity-feed'].innerHTML,html);},
 filter(){c._afFilter='card';elements['af-filter'].value='card';send(newRun);assert.equal(c._afFilter,'card');assert.equal(elements['af-filter'].value,'card');assert.equal(c._afEntries[0].text,'run-1');},
 quiet(){c._afQuietCards.card=true;sources.at(-1).emit('state_update',newRun.state);assert.equal(Object.keys(c._afQuietCards).length,0,'Old quiet episodes must not survive a new run');assert.equal(c._afEntries.length,0);},
 legacy(){const state={phase:'idle'};sources.at(-1).emit('state_update',state);sources.at(-1).emit('activity_event',{entries:oldRun.entries});assert.equal(c._afEntries.length,1,'Missing identity must preserve conservative replay behavior');}
};
assert.ok(cases[process.argv[4]]);cases[process.argv[4]]();console.log('PASS '+process.argv[4]);
