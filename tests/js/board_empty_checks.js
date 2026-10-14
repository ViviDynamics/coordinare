'use strict';
// 74 (updated for 139): the Cards panel tells an empty snapshot from a missing one.
// The header choice narrows the symphonies first, so "selected" means the scope.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={'swimlane-section':{innerHTML:''},'active-work-card':{classList:{toggle(){},remove(){}}}};
const c={document:{getElementById:id=>nodes[id]},esc:String};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),c);
function sym(name,snap){return {name,state:{board_snapshot:snap}};}
function render(symphonies,phase='blocked',choice=''){
  const full={phase,active_sessions:[],symphonies};
  c.renderActiveWorkPanels(choice===''?full:c.symphonyScope(full,choice));
  return nodes['swimlane-section'].innerHTML;
}
const held={TODO:[],BLOCKED:[],IN_PROGRESS:[],IN_REVIEW:[],BACKLOG:['held'],DONE:['finished']};
const cases={
  initial(){const html=render([sym('unpolled',null),sym('sample',held)]);assert.ok(html.includes('No active cards in this symphony'),'A received board is shown under All');assert.ok(!html.includes('Waiting for cards'));},
  late(){render([sym('unpolled',null),sym('sample',null)]);const html=render([sym('unpolled',null),sym('sample',held)]);assert.ok(!html.includes('Waiting for cards'),'A board that arrives late is shown');},
  held(){const html=render([sym('sample',held)]);assert.ok(!html.includes('Waiting for cards'),'A received held/completed board must not be reported missing');assert.ok(html.includes('Backlog: 1'));},
  empty(){const html=render([sym('sample',{})]);assert.ok(!html.includes('Waiting for cards'),'A received empty snapshot is not missing');},
  missing(){const html=render([sym('sample',null)]);assert.ok(html.includes('Waiting for cards'));},
  selected(){const syms=[sym('unpolled',null),sym('sample',held)];assert.ok(render(syms,'blocked','unpolled').includes('Waiting for cards'));assert.ok(!render(syms,'blocked','sample').includes('Waiting for cards'));},
  populated(){const html=render([sym('sample',{TODO:['new-card']})]);assert.ok(html.includes('new-card'));assert.ok(!html.includes('Waiting for cards'));},
  idleMissing(){const html=render([sym('sample',null)],'idle');assert.ok(html.includes('Waiting for cards'),'A missing snapshot must wait, not claim an empty TODO column');},
};
const name=process.argv[3];
assert.ok(cases[name],'unknown case '+name);
cases[name]();
