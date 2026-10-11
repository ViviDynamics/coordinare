'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes={'swimlane-section':{innerHTML:''},'active-work-card':{classList:{toggle(){},remove(){}}},'swimlane-tabs':{style:{},innerHTML:''}};
const c={document:{getElementById:id=>nodes[id]},_swimlaneTab:'sample',esc:String};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),c);
function sym(name,snap){return {name,state:{board_snapshot:snap}};}
function render(symphonies,phase='blocked'){c.renderActiveWorkPanels({phase,active_sessions:[],symphonies});return nodes['swimlane-section'].innerHTML;}
const held={TODO:[],BLOCKED:[],IN_PROGRESS:[],IN_REVIEW:[],BACKLOG:['held'],DONE:['finished']};
const cases={
 held(){const html=render([sym('sample',held)]);assert.ok(!html.includes('Waiting for board snapshot'),'A received held/completed board must not be reported missing');assert.ok(html.includes('Backlog')&&html.includes('Done'),'Empty active view must explain held/completed cards');},
 empty(){const html=render([sym('sample',{})]);assert.ok(!html.includes('Waiting for board snapshot'),'A received empty snapshot is not missing');},
 missing(){const html=render([sym('sample',null)]);assert.ok(html.includes('Waiting for board snapshot'));},
 selected(){const syms=[sym('unpolled',null),sym('sample',held)];c._swimlaneTab='unpolled';assert.ok(render(syms).includes('Waiting for board snapshot'));c._swimlaneTab='sample';assert.ok(!render(syms).includes('Waiting for board snapshot'),'Selection must use the selected board snapshot');},
 populated(){const html=render([sym('sample',{TODO:['new-card']})]);assert.ok(html.includes('new-card'));assert.ok(!html.includes('Waiting for board snapshot'));}
};
assert.ok(cases[process.argv[3]]);cases[process.argv[3]]();console.log('PASS '+process.argv[3]);
