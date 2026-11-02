// Focused Chromium comparison using installed upstream source, not reimplemented val().
// Usage: node scripts/probe-js-conditions.mjs /tmp/probe/node_modules/playwright/index.mjs \
//   .venv-da19/lib/python3.14/site-packages/docassemble/webapp/static/app
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const { chromium } = await import(pathToFileURL(resolve(process.argv[2])));
const appDir = resolve(process.argv[3]);
const app = readFileSync(`${appDir}/app.js`, 'utf8');
const jquery = readFileSync(`${appDir}/jquery.js`, 'utf8');
function fragment(start, end) {
  const first = app.indexOf(start);
  const last = app.indexOf(end, first);
  assert(first >= 0 && last > first, `upstream source markers changed: ${start}`);
  return app.slice(first, last);
}
const helpers = fragment('function atou(', 'function hideTablist(')
  + fragment('function daAppendIfExists(', 'function setChoices(')
  + fragment('function val(', 'var da_val = val;');
const announcement = app.includes('function daAnnounceShowIfReveal()')
  ? fragment('function daAnnounceShowIfReveal()', 'function dagoogleapicallback()') : '';
const handlers = fragment('$(".dajsshowif").each(function () {', '$(".dashowif").each(function () {');
const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  const page = await browser.newPage();
  await page.setContent('<html><body></body></html>');
  await page.addScriptTag({ content: jquery });
  await page.addScriptTag({ content: `
    var daVarLookupCheckbox = {}, daVarLookupSelect = {}, daVarLookupMulti = {}, daVarLookupOption = {};
    var daComboBoxes = {}, daInputsSeen = {}, daTriggerQueries = [];
    var daShowIfInProcess = false, daShowHideHappened = false, daShowIfSetupComplete = false;
    ${announcement}
    ${helpers}
  ` });
  const observations = await page.evaluate((handlers) => {
    const b64 = (name) => btoa(name).replace(/=/g, '');
    const cases = [
      ['count-one', 'val("count") == 1', ['count'], true, '1', true],
      ['count-plural', 'val("count") > 1', ['count'], true, '2', true],
      ['count-singular-hidden', 'val("count") == 1', ['count'], true, '2', false],
      ['strict-number', 'val("count") === 1', ['count'], true, '1', false],
      ['strict-string', 'val("count") === "1"', ['count'], true, '1', true],
      ['missing-and-controller', '!val("missing") && val("count") == 1', ['missing', 'count'], true, '1', true],
      ['short-circuit-true', 'true || val("count")', ['count'], true, '', true],
      ['short-circuit-false-hide', 'false && val("count")', ['count'], false, '', true],
      ['no-trigger-show', 'true', [], true, '', false],
      ['no-trigger-hide', 'false', [], false, '', false],
      ['namespace-only-hide', 'val("missing") == 1', ['missing'], false, '', false],
      ['unanswered-number', 'val("count") === ""', ['count'], true, '', true],
      ['unanswered-radio', 'val("radio") === null', ['radio'], true, '', true],
      ['account_settings-indexed-signature', 'val("firmdata.attorneys[i].signature_yn") && !val("firmdata.attorneys[i].signature_replace")', ['firmdata.attorneys[i].signature_yn', 'firmdata.attorneys[i].signature_replace'], true, '', true],
      ['dept-of-revenue-null-not-MO', 'val("M.dept_of_revenue.dlr.address.state") !== "MO"', ['M.dept_of_revenue.dlr.address.state'], true, '', true],
      ['foreclosure-indexed-not-deed', 'val("M.foreclosure.deed.creditors[i].type") !== "deed"', ['M.foreclosure.deed.creditors[i].type'], true, '', true],
      ['demo-checkbox-options', `val("liked_fruits['Apple']") && val("liked_fruits['Plum']")`, ["liked_fruits['Apple']", "liked_fruits['Plum']"], true, '', true],
      ['demo-yesnomaybe-string-None', 'val("maybe") === "None" && val("maybe") !== null', ['maybe'], true, '', true],
      ['group-array-truthiness', 'val("liked_fruits")', ['liked_fruits'], true, '', true],
      ['checkbox-array-identity', 'val("liked_fruits") === val("liked_fruits")', ['liked_fruits'], true, '', false],
      ['checkbox-array-nonidentity', 'val("liked_fruits") !== val("liked_fruits")', ['liked_fruits'], true, '', true],
      ['multiselect-array-identity', 'val("selected_fruits") === val("selected_fruits")', ['selected_fruits'], true, '', false],
      ['multiselect-array-nonidentity', 'val("selected_fruits") !== val("selected_fruits")', ['selected_fruits'], true, '', true],
      ['generic-numeric-spelling', 'val("x.count") === "1.0"', ['x.count'], true, '1.0', true],
      ['indexed-numeric-spelling', 'val("people[i].count") === "1.0"', ['people[i].count'], true, '1.0', true],
      ['radio-boolean', 'val("radio") === true', ['radio'], true, '', true, true],
    ];
    return cases.map(([name, expression, vars, sign, count, expected, checked = false]) => {
      const info = { expression, vars, sign, mode: 0 };
      document.body.innerHTML = `<div class="da-form-group"><input id="${b64('count')}" name="${b64('count')}" value="${count}"></div>
        <div class="da-form-group"><input type="radio" id="${b64('radio')}" name="${b64('radio')}" value="True" ${checked ? 'checked' : ''}></div>
        <div class="da-form-group dajsshowif" style="display:none" data-jsshowif="${btoa(JSON.stringify(info))}"><input name="answer"></div>`;
      const add = (name, type, value, checked = false) => {
        const input = document.createElement('input');
        input.id = input.name = b64(name); input.type = type; input.value = value; input.checked = checked;
        const wrapper = document.createElement("div"); wrapper.className = "da-form-group";
        wrapper.append(input); document.body.prepend(wrapper);
        return input;
      };
      add('firmdata.attorneys[i].signature_yn', 'checkbox', 'True', true);
      add('M.dept_of_revenue.dlr.address.state', 'radio', 'MO');
      add('M.foreclosure.deed.creditors[i].type', 'text', 'other');
      add('maybe', 'radio', 'None', true);
      add('x.count', 'text', count);
      add('people[i].count', 'text', count);
      const selectWrapper = document.createElement('div');
      selectWrapper.className = 'da-form-group';
      selectWrapper.innerHTML = `<select id="${b64('selected_fruits')}" name="${b64('selected_fruits')}" class="damultiselect" multiple><option value="apple-option" selected>Apple</option></select>`;
      document.body.prepend(selectWrapper);
      daVarLookupOption['apple-option'] = 'Apple';
      const group = document.createElement('div');
      group.className = 'da-field-group da-field-checkboxes';
      const groupWrapper = document.createElement("div"); groupWrapper.className = "da-form-group";
      groupWrapper.append(group); document.body.prepend(groupWrapper);
      const options = ['Apple', 'Plum'].map(key => {
        const elem = add(`liked_fruits['${key}']`, 'checkbox', 'True', true);
        elem.dataset.cbvalue = b64(key); group.append(elem);
        return { elem };
      });
      daVarLookupMulti[b64("liked_fruits")] = options.map(({elem}) => elem.name);
      daVarLookupCheckbox.liked_fruits = [{ elem: group, checkboxes: options }];
      // Install upstream handlers, then perform the same initial manual triggers.
      eval(handlers);
      $('input,select').trigger('daManualTrigger');
      const visible = $('.dajsshowif').css('display') !== 'none';
      return { name, visible, expected, value: val('count') };
    });
  }, handlers);
  for (const observed of observations) assert.equal(observed.visible, observed.expected, JSON.stringify(observed));
  const chains = await page.evaluate((handlers) => {
    const b64 = (name) => btoa(name).replace(/=/g, '');
    const wrapper = (name, expression, vars, input) => `<div id="${name}-wrapper" class="da-form-group dajsshowif" style="display:none" data-jsshowif="${btoa(JSON.stringify({expression, vars, sign: true, mode: 0}))}">${input}</div>`;
    const input = (name, value, type = 'text', checked = false) => `<input id="${b64(name)}" name="${b64(name)}" type="${type}" value="${value}" ${checked ? 'checked' : ''}>`;
    const settle = () => {
      // Upstream's daTriggerAllShowHides repeats manual triggers to a bound.
      // Mark inputs seen to suppress recursive change dispatch while measuring
      // the outer convergence loop independently of nested event ordering.
      daShowIfInProcess = true;
      daInputsSeen = Object.fromEntries([...document.querySelectorAll('input')].map(el => [el.id, true]));
      for (let round = 0; round < 100; round++) {
        daShowHideHappened = false;
        $('input').trigger('daManualTrigger');
        if (!daShowHideHappened) return {converged: true, rounds: round + 1};
      }
      return {converged: false, rounds: 100};
    };
    const visible = name => $(`#${name}-wrapper`).css('display') !== 'none';
    document.body.innerHTML = `<div class="da-form-group">${input('listen', 'True', 'radio', true)}</div>`
      + wrapper('modulation', 'val("listen") === true', ['listen'], input('modulation', 'AM'))
      + wrapper('antenna', 'val("modulation") === "AM"', ['modulation'], input('antenna', 'wire'))
      + wrapper('missing', '!val("modulation") && !val("absent")', ['modulation', 'absent'], input('missing', ''));
    eval(handlers);
    const defaults = {...settle(), modulation: visible('modulation'), antenna: visible('antenna')};
    document.getElementById(b64('listen')).checked = false;
    const reversed = {...settle(), modulation: visible('modulation'), antenna: visible('antenna'), missing: visible('missing'), controller: val('modulation')};
    document.getElementById(b64('listen')).checked = true;
    const restored = {...settle(), modulation: visible('modulation'), antenna: visible('antenna'), controller: val('modulation')};
    document.getElementById(b64('modulation')).disabled = true;
    const disabledLookup = val('modulation');
    document.body.innerHTML = wrapper('cycle', '!val("cycle")', ['cycle'], input('cycle', 'True', 'checkbox', true));
    eval(handlers);
    const cycle = settle();
    return {defaults, reversed, restored, disabledLookup, cycle};
  }, handlers);
  assert.equal(chains.defaults.converged, true);
  assert.equal(chains.defaults.modulation, true);
  assert.equal(chains.defaults.antenna, true);
  assert.equal(chains.reversed.converged, true);
  assert.equal(chains.reversed.modulation, false);
  assert.equal(chains.reversed.antenna, false);
  assert.equal(chains.reversed.missing, true);
  assert.equal(chains.reversed.controller, null);
  assert.equal(chains.restored.converged, true);
  assert.equal(chains.restored.antenna, true);
  assert.equal(chains.restored.controller, 'AM');
  assert.equal(chains.disabledLookup, null);
  assert.equal(chains.cycle.converged, false);
  await page.addScriptTag({content: readFileSync(`${appDir}/jquery.validate.js`, 'utf8')});
  const enablement = await page.evaluate((handlers) => {
    const b64 = name => btoa(name).replace(/=/g, '');
    return [true, false].flatMap(sign => ['false', "val('absent')"].map(expression => {
      const info = {expression: "val('country')", vars: ['country'], sign, mode: 1};
      const noTrigger = {expression, vars: expression === 'false' ? [] : ['absent'], sign, mode: 1};
      const dependent = {expression: 'val("initial") === "seed"', vars: ['initial'], sign: true, mode: 0};
      const nullDependent = {...dependent, expression: 'val("initial") === null'};
      document.body.innerHTML = `<form id="daform"><div class="da-form-group"><select id="${b64('country')}" name="${b64('country')}"><option value=""></option><option value="US">US</option></select></div>
        <div id="state-wrapper" class="da-form-group dajsshowif" data-jsshowif="${btoa(JSON.stringify(info))}">
        <input id="${b64('state')}" name="${b64('state')}" required>
        <input id="bounded" name="bounded" type="text" min="2" value="1">
        <input id="prefilled" name="prefilled" value="MO"></div>
        <div id="initial-wrapper" class="da-form-group dajsshowif" data-jsshowif="${btoa(JSON.stringify(noTrigger))}"><input id="${b64('initial')}" name="${b64('initial')}" value="seed" required><input id="initial-disabled" name="initial-disabled" disabled></div>
        <div id="dependent-wrapper" class="da-form-group dajsshowif" style="display:none" data-jsshowif="${btoa(JSON.stringify(dependent))}"><input name="dependent" required></div>
        <div id="null-wrapper" class="da-form-group dajsshowif" style="display:none" data-jsshowif="${btoa(JSON.stringify(nullDependent))}"><input name="null-dependent" required></div></form>`;
      const validator = $('#daform').validate();
      const snapshot = () => {
        validator.form();
        return {
        visible: $('#state-wrapper').css('display') !== 'none',
        enabled: !document.getElementById(b64('state')).disabled,
        lookup: val('state'),
        required: !validator.invalid[b64('state')],
        constraint: !validator.invalid.bounded,
        posted: $('#daform').serializeArray().map(item => item.name),
        initial: !document.getElementById(b64('initial')).disabled,
        initialLookup: val('initial'),
        initialIsVisibleUnset: $('#initial-wrapper').data('isVisible') === undefined,
        dependent: $('#dependent-wrapper').css('display') !== 'none',
        nullDependent: $('#null-wrapper').css('display') !== 'none',
        initialDisabled: document.getElementById('initial-disabled').disabled,
      }; };
      eval(handlers);
      $('input,select').trigger('daManualTrigger');
      const empty = snapshot();
      document.getElementById(b64('country')).value = 'US';
      $('input,select').trigger('daManualTrigger');
      const selected = snapshot();
      document.getElementById(b64('country')).value = '';
      $('input,select').trigger('daManualTrigger');
      const reversed = snapshot();
      return {sign, expression, empty, selected, reversed};
    }));
  }, handlers);
  for (const row of enablement) {
    for (const [state, enabled] of [[row.empty, !row.sign], [row.selected, row.sign], [row.reversed, !row.sign]]) {
      assert.equal(state.visible, true);
      assert.equal(state.enabled, enabled);
      assert.equal(state.lookup, enabled ? '' : null);
      assert.equal(state.required, !enabled);
      assert.equal(state.constraint, !enabled);
      assert.equal(state.posted.includes('prefilled'), enabled);
      assert.equal(state.initial, true);
      assert.equal(state.initialLookup, null);
      assert.equal(state.initialIsVisibleUnset, true);
      assert.equal(state.dependent, false);
      assert.equal(state.nullDependent, true);
      assert.equal(state.posted.includes(btoa('initial').replace(/=/g, '')), true);
      assert.equal(state.initialDisabled, true);
    }
  }
  console.log(JSON.stringify({ upstream: appDir, browser: await browser.version(), observations, chains, enablement }, null, 2));
} finally {
  await browser.close();
}
