// Feed a JSON array of SchedulePlan objects from the local solver on stdin.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const candidates = JSON.parse(fs.readFileSync(0, 'utf8').replace(/^\uFEFF/, ''));
assert.equal(candidates.length, 3);
const html = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
scripts.forEach(source => new vm.Script(source));
new vm.Script(fs.readFileSync(path.join(__dirname, 'web/p1_3.js'), 'utf8'));
class Element {
  constructor() { this.children = []; this.style = {}; this.value = ''; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) {
    this.children = nodes;
    if (nodes[0] instanceof Option) this.value = nodes[0].value;
  }
  setAttribute() {}
  set innerHTML(value) { throw new Error('Unsafe HTML rendering: ' + value); }
}
class Option extends Element {
  constructor(text, value) { super(); this.textContent = text; this.value = value; }
}
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const context = vm.createContext({
  document: {getElementById: element, createElement: () => new Element(), querySelector: element},
  sessionStorage: {getItem: () => null, setItem() {}},
  Option, candidates
});
vm.runInContext(scripts[0], context);
vm.runInContext("current = {plan_id:'render-check', version:2, candidates, quarantine_impact:['EVT-005']}; render();", context);
assert.equal(element('candidate').children.length, 3);
assert(allText(element('risks')).includes('EVT-005'));
for (const candidate of candidates) {
  element('candidate').value = candidate.profile;
  vm.runInContext('draw()', context);
  assert.equal(element('ontime').textContent, (candidate.kpis.on_time_rate * 100).toFixed(1) + '%');
  assert.equal(element('risk').textContent,
    String(Object.values(candidate.material_reservations).filter(r => r.status === 'SHORTAGE').length));
  assert.equal(element('gantt').children.at(-1).children[1].textContent.split('\n').filter(Boolean).length,
    candidate.operations.length);
}
function allText(node) { return node.textContent + '\n' + node.children.map(allText).join('\n'); }
vm.runInContext(`
  current.candidates = [{profile:'<img src=x onerror=alert(1)>',
    kpis:{on_time_rate:0.5, changeover_count:3, overtime_hours:2, late_orders:['LATE-1']},
    material_reservations:{'SHORT-1':{status:'SHORTAGE'},'READY-1':{status:'READY',ready_at:0},'INBOUND-1':{status:'READY',ready_at:60}},
    operations:[], unscheduled_operations:[{order_id:'SHORT-1',operation_no:1,reason:'MATERIAL_SHORTAGE'}],
    violations:[{code:'VALIDATION_FAILED'}], required_actions:['publish_plan']}];
  render();
`, context);
assert.equal(element('risk').textContent, '1');
assert.equal(element('ontime').textContent, '50.0%');
for (const expected of ['SHORT-1','INBOUND-1','LATE-1','MATERIAL_SHORTAGE','VALIDATION_FAILED']) {
  assert(allText(element('risks')).includes(expected), expected);
}
assert.equal(element('plans').children[0].children[0].textContent, '<img src=x onerror=alert(1)>');
vm.runInContext('current.candidates[0].material_reservations = {}; draw();', context);
assert.equal(element('risk').textContent, '0');
vm.runInContext('delete current.candidates[0].material_reservations; draw();', context);
assert.equal(element('risk').textContent, '未提供');
vm.runInContext('current.candidates = []; render();', context);
assert.equal(element('risks').textContent, '暂无候选方案');
console.log('PASS: real solver candidates, profile selection, KPI fields, reservation map, shortages, inbound materials, unscheduled work, violations, quarantine impact, safe text, complete Gantt details, empty/missing data.');

async function checkChat() {
  const calls = [];
  element('upload').files = [];
  element('factory').value = 'factory_demo.json';
  element('prompt').value = 'generate plans';
  context.mockApi = async (route, options) => {
    calls.push({route, body:JSON.parse(options.body)});
    return {response:'Model explanation', mode:'bedrock', run_id:'test-run',
      traces:[{step:1,name:'model_intent',status:'completed',elapsed_ms:1,run_id:'test-run'}],
      plan:{plan_id:'new-plan',version:1,candidates}};
  };
  vm.runInContext('current = null; api = mockApi;', context);
  await element('send').onclick();
  assert.equal(calls[0].route, '/agent/chat');
  assert.equal(calls[0].body.message, 'generate plans');
  assert.equal(element('send').disabled, false);
  assert.equal(element('prompt').value, '');
  assert(element('trace').textContent.includes('model_intent'));
  element('prompt').value = 'explain plan';
  await element('send').onclick();
  assert.equal(calls[1].body.plan_id, 'new-plan');
  assert.equal(calls[1].body.expected_version, 1);
  context.mockApi = async () => { throw new Error('provider unavailable'); };
  vm.runInContext('api = mockApi;', context);
  element('prompt').value = 'keep this input';
  await element('send').onclick();
  assert.equal(element('prompt').value, 'keep this input');
  assert.equal(element('status').textContent, 'provider unavailable');
  assert.equal(element('send').disabled, false);
  console.log('PASS: Agent chat request, plan synchronization, version binding, real trace display, error recovery (DOM simulation).');
}
checkChat().catch(error => { console.error(error); process.exitCode = 1; });
