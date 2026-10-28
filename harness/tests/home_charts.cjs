/* The geometry behind the Rocoto marks (web/control-room/home-charts.js), run with Node: no page needed. */
const assert = require('node:assert/strict');
const path = require('node:path');
const charts = require(path.join(__dirname, '../web/display-charts.js'));
const g = charts.geometry;
const near = (a, b, slack = 0.02) => assert.ok(Math.abs(a - b) <= slack, `${a} is within ${slack} of ${b}`);

for (const name of ['line', 'spark', 'ring', 'arc', 'bars', 'ghost']) assert.equal(typeof charts[name], 'function', `RocotoCharts.${name}`);
assert.equal(charts.line(null, [1, 2, 3]), null, 'a mark with no host draws nothing');
assert.equal(globalThis.RocotoCharts, charts, 'the module is also the page global');

// samples: numbers, {value}, numeric text; everything else is a missing measurement
assert.deepEqual([3, { value: 4 }, '5', { value: '6.5' }].map(g.valueOf), [3, 4, 5, 6.5]);
assert.deepEqual([null, undefined, NaN, '', 'n/a', {}, { value: null }, Infinity].map(g.valueOf), Array(8).fill(null));

// where samples sit: zero at the foot with floor 0, the newest sample at the right edge
const box = { width: 200, height: 100, top: 10, bottom: 10, left: 0, right: 0 };
let layout = g.lineLayout([0, 5, 10], { ...box, floor: 0 });
assert.deepEqual(layout.points, [[0, 90], [100, 50], [200, 10]]);
assert.equal(layout.base, 90);
// a missing sample stays missing: the line breaks there and resumes
layout = g.lineLayout([1, 2, null, 4, 5], { ...box, floor: 0 });
assert.equal(layout.points[2], null);
assert.deepEqual(layout.runs.map(run => run.length), [2, 2]);
assert.equal((g.strokePath(layout.runs).match(/M/g) || []).length, 2, 'two strokes, one per run');
assert.deepEqual(g.lineLayout([null, null], box).runs, []);
// a level series rests: at the foot when it is zero, through the middle otherwise
assert.deepEqual(g.lineLayout([0, 0, 0], box).points.map(p => p[1]), [90, 90, 90]);
assert.deepEqual(g.lineLayout([4, 4, 4], box).points.map(p => p[1]), [50, 50, 50]);
// the soft floor keeps a small change small; the series' own range fills the box
const soft = g.lineLayout([100, 101], box).points, own = g.lineLayout([100, 101], { ...box, floor: 'auto' }).points;
assert.ok(soft[0][1] - soft[1][1] < 3, 'one part in a hundred moves the line by a hair');
assert.deepEqual(own.map(p => p[1]), [90, 10]);
assert.ok(g.lineLayout([-5, 5], box).min <= -5, 'a series below zero keeps its own foot');
// one sample sits at the newest edge
assert.deepEqual(g.lineLayout([7], { ...box, floor: 0 }).points, [[200, 10]]);

// the curve passes through every measurement and never overshoots one
const through = [[0, 80], [50, 20], [100, 20], [150, 60], [200, 10]];
const d = g.monotonePath(through);
const numbers = d.replace(/[MC]/g, ' ').trim().split(/[ ,]+/).map(Number);
assert.deepEqual([numbers[0], numbers[1]], through[0]);
const segments = [];
for (let i = 2; i < numbers.length; i += 6) segments.push(numbers.slice(i, i + 6));
assert.equal(segments.length, through.length - 1);
segments.forEach(([, y1, , y2, x, y], i) => {
  assert.deepEqual([x, y], through[i + 1], `segment ${i} ends on its measurement`);
  const low = Math.min(through[i][1], through[i + 1][1]) - 0.01, high = Math.max(through[i][1], through[i + 1][1]) + 0.01;
  assert.ok(y1 >= low && y1 <= high && y2 >= low && y2 <= high, `segment ${i} stays between its two measurements`);
});
assert.equal(g.monotonePath([]), '');
assert.equal(g.monotonePath([[1, 2]]), 'M1,2');
assert.equal(g.monotonePath([[0, 0], [10, 5]]), 'M0,0L10,5');
assert.ok(g.areaPath([[[0, 50], [100, 20]]], 100).endsWith('L100,100L0,100Z'), 'the area closes on the baseline');
assert.equal(g.areaPath([[[0, 50]]], 100), '', 'a lone sample has no area');

// circles: degrees run clockwise from twelve o'clock
assert.deepEqual(g.polar(0, 0, 10, 0), [0, -10]);
assert.deepEqual(g.polar(0, 0, 10, 90), [10, 0]);
assert.deepEqual(g.polar(0, 0, 10, 180), [0, 10]);
assert.match(g.arcPath(50, 50, 40, 0, 90), /A40,40 0 0 1 90,50$/);
assert.match(g.arcPath(50, 50, 40, 0, 270), /A40,40 0 1 1 /);
assert.equal((g.arcPath(50, 50, 40, 0, 360).match(/A/g) || []).length, 2, 'a whole ring is two half turns');

// bars stand on the baseline; the shoulders never round past half the bar
assert.equal(g.barPath(0, 0, 4, 10, 10), 'M0,10V2Q0,0 2,0H2Q4,0 4,2V10Z');
assert.equal(g.barPath(10, 20, 8, 50, 0), 'M10,50V20Q10,20 10,20H18Q18,20 18,20V50Z');

// motion read from the theme's own statements
const linear = g.bezier(0, 0, 1, 1), ease = g.parseEasing('cubic-bezier(0.16, 1, 0.3, 1)', linear);
assert.equal(linear(0), 0); assert.equal(linear(1), 1); near(linear(0.5), 0.5);
assert.ok(ease(0.25) > 0.6, 'the draw curve leaves fast');
near(ease(1), 1, 0);
assert.equal(g.parseEasing('ease-out', linear), linear);
assert.deepEqual(['700ms', '0.4s', ' 240 ', 'soon', ''].map(text => g.parseDuration(text, 111)), [700, 400, 240, 111, 111]);

console.log('home charts geometry: ok');
