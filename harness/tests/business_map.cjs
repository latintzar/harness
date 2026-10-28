/* Local D3 + structured fixtures. Optional original reference stays outside the repository. */
const { test, before, after } = require("node:test"),
  assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path"),
  http = require("node:http"),
  crypto = require("node:crypto");
const { chromium } = require("playwright");
const base = path.resolve(__dirname, ".."),
  web = process.env.ROCOTO_MAP_TEST_WEB || require('./web_fixture.cjs').web(),
  theme = JSON.parse(fs.readFileSync(path.join(base, "theme.json")));
let server, browser, origin;
before(async () => {
  server = http.createServer((req, res) => {
    const route = new URL(req.url, "http://localhost").pathname;
    if (route === "/") {
      res.setHeader("content-type", "text/html");
      res.setHeader(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'none'; img-src 'self';",
      );
      res.end(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/business-map.css"><script defer src="/business-map.js"></script><main id="root"></main>',
      );
      return;
    }
    if (route === "/theme.css") {
      res.setHeader("content-type", "text/css");
      res.end(
        ":root{" +
          Object.entries({
            ...theme.palette,
            ...theme.typography,
            "icon-size-meta": "16px",
            radius: "12px",
            "radius-sm": "8px",
          })
            .map(([k, v]) => "--" + k + ":" + v + ";")
            .join("") +
          "}*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--paper)}main{max-width:1000px;margin:auto}@media(max-width:480px){body{padding:16px}}",
      );
      return;
    }
    const file = path.resolve(web, "." + route);
    if (!file.startsWith(web + path.sep) || !fs.existsSync(file)) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.setHeader(
      "content-type",
      route.endsWith(".js") ? "text/javascript" : "text/css",
    );
    fs.createReadStream(file).pipe(res);
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  origin = "http://127.0.0.1:" + server.address().port;
  browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {}),
    args: ["--no-sandbox"],
  });
});
after(async () => {
  await browser?.close();
  await new Promise((r) => server?.close(r));
});
async function fixture(width = 1600, mobile = false) {
  const context = await browser.newContext({
      viewport: { width, height: 1000 },
      hasTouch: mobile,
      isMobile: mobile,
    }),
    page = await context.newPage(),
    external = [],
    errors = [];
  page.setDefaultTimeout(5000);
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/*", (r) => {
    if (
      !r
        .request()
        .url()
        .startsWith(origin + "/")
    ) {
      external.push(r.request().url());
      return r.abort();
    }
    return r.continue();
  });
  await page.goto(origin);
  await page.waitForFunction(() => window.RocotoBusinessMap);
  return { page, context, external, errors, close: () => context.close() };
}
function source() {
  const polygon = (size) => ({
      type: "Polygon",
      coordinates: [
        [
          [-3 - size, 40 - size],
          [-3 + size, 40 - size],
          [-3 + size, 40 + size],
          [-3 - size, 40 + size],
          [-3 - size, 40 - size],
        ],
      ],
    }),
    feature = (geometry, properties = {}) => ({
      type: "Feature",
      geometry,
      properties,
    }),
    collection = (features) => ({ type: "FeatureCollection", features });
  return {
    origin: {
      name: "Fixture cafe",
      coordinates: [-3, 40],
      address: "Fixture address",
      url: "https://example.com/cafe",
      source: "Fixture source",
    },
    bbox: [-3.02, 39.98, -2.98, 40.02],
    pois: [
      {
        id: "shop-one",
        name: "Fixture shop",
        category: "shop",
        coordinates: [-2.995, 40.002],
        url: "https://example.com/shop",
        walkMinutes: 4,
      },
      {
        id: "transit-one",
        name: "Fixture station",
        category: "transit",
        coordinates: [-3.005, 39.998],
        url: "https://example.com/station",
        walkMinutes: 6,
      },
    ],
    catchments: [5, 10, 15].map((minutes) => ({
      minutes,
      geometry: polygon(minutes * 0.0007),
    })),
    roads: collection([
      feature(
        {
          type: "LineString",
          coordinates: [
            [-3.01, 40],
            [-3, 40],
            [-2.99, 40],
          ],
        },
        { name: "Fixture street", kind: "residential" },
      ),
    ]),
    buildings: collection([feature(polygon(0.001), { use: "residential" })]),
    green: collection([feature(polygon(0.0005))]),
    stats: { totalBuildings: 1, taggedBuildings: 1 },
    methodology: {
      source: "Fixture mapped data",
      sourceTimestamp: null,
      walkingSpeedKmh: 4.8,
      walkingMethod: "network",
      catchmentNote: "Estimated nearby-street envelope.",
      poiTimeNote: "Rounded network distance plus street connector.",
      buildingUseNote: "Mapped use from source tags.",
      uncertainty: ["Fixture uncertainty."],
      attribution: "Fixture contributor",
      license: "https://example.com/license",
    },
    sources: [
      {
        title: "Fixture source",
        url: "https://example.com/source",
        retrieved_at: "2026-10-05T12:00:00Z",
      },
    ],
  };
}
async function render(page, data) {
  await page.evaluate((data) => {
    document
      .querySelectorAll(".business-map")
      .forEach((el) => RocotoBusinessMap.dispose(el));
    const el = RocotoBusinessMap.render(data, {
      onExpand: () => {
        window.expanded = true;
      },
    });
    document.querySelector("#root").replaceChildren(el);
    window.mapElement = el;
  }, data);
  await page.waitForFunction(() =>
    ["ready", "error"].includes(
      document.querySelector(".business-map").dataset.mapState,
    ),
  );
}
test("business origin stays neutral and rendering preserves supplied geometry", async () => {
  const h = await fixture();
  try {
    const data = source();
    data.origin.name = "Fixture workshop";
    await h.page.evaluate((data) => {
      window.originalMapData = data;
      window.originalMapJSON = JSON.stringify(data);
      const map = RocotoBusinessMap.render(data);
      document.querySelector("#root").append(map);
    }, data);
    await h.page.waitForFunction(
      () =>
        document.querySelector(".business-map").dataset.mapState === "ready",
    );
    assert.equal(
      await h.page
        .getByRole("button", {
          name: "Fixture workshop, Business",
          exact: true,
        })
        .isVisible(),
      true,
    );
    assert.equal(
      await h.page.evaluate(
        () => JSON.stringify(originalMapData) === originalMapJSON,
      ),
      true,
    );
    assert.deepEqual(h.errors, []);
  } finally {
    await h.close();
  }
});
test("map renders supplied geometry and pins with source-qualified walking details", async () => {
  const h = await fixture();
  try {
    await render(h.page, source());
    assert.equal(
      await h.page.locator(".business-map").getAttribute("data-map-state"),
      "ready",
    );
    assert.equal(
      await h.page.locator('[data-layer="buildings"] path').count(),
      1,
    );
    assert(
      (
        await h.page.locator('[data-layer="buildings"] path').getAttribute("d")
      ).startsWith("M"),
    );
    await h.page
      .getByRole("button", { name: "Fixture shop, Shop", exact: true })
      .click();
    assert(
      (await h.page.locator(".business-map-selected").textContent()).includes(
        "~4 min walk",
      ),
    );
    assert.equal(
      await h.page
        .getByRole("link", { name: "View place", exact: true })
        .getAttribute("href"),
      "https://example.com/shop",
    );
    await h.page
      .getByText("Sources and walking estimates", { exact: true })
      .click();
    assert(
      (await h.page.locator(".business-map-method").textContent()).includes(
        "Source date awaiting record",
      ),
    );
    assert(
      (await h.page.locator(".business-map-method").textContent()).includes(
        "Retrieved 2026-10-05T12:00:00Z",
      ),
    );
    assert(
      (await h.page.locator(".business-map-method").textContent()).includes(
        "Fixture uncertainty.",
      ),
    );
    assert.deepEqual(h.external, []);
    assert.deepEqual(h.errors, []);
  } finally {
    await h.close();
  }
});
test("layer category and walking controls alter only their supplied geometry", async () => {
  const h = await fixture();
  try {
    await render(h.page, source());
    const initial = await h.page
      .locator('[data-layer="buildings"] path')
      .getAttribute("d");
    await h.page
      .getByRole("combobox", { name: "Walking area", exact: true })
      .selectOption("5");
    assert.notEqual(
      await h.page.locator('[data-layer="buildings"] path').getAttribute("d"),
      initial,
    );
    await h.page.getByLabel("Land use", { exact: true }).uncheck();
    assert.equal(
      await h.page.locator(".business-map-legend").isVisible(),
      false,
    );
    await h.page
      .locator('select[data-control="category"]')
      .selectOption("transit");
    assert.equal(
      await h.page
        .getByRole("button", { name: "Fixture shop, Shop", exact: true })
        .isVisible(),
      false,
    );
    await h.page.locator('input[data-control="amenities"]').uncheck();
    assert.equal(await h.page.locator(".business-map-pin:visible").count(), 1);
    await h.page.locator('input[data-control="walking"]').uncheck();
    assert.equal(
      await h.page.locator('[data-layer="walking"]').getAttribute("display"),
      "none",
    );
  } finally {
    await h.close();
  }
});
test("zoom pan keyboard and reset remain bounded and independently operable", async () => {
  const h = await fixture();
  try {
    await render(h.page, source());
    const svg = h.page.locator(".business-map-svg");
    await h.page.getByRole("button", { name: "Zoom in", exact: true }).click();
    assert.equal(await svg.evaluate((el) => d3.zoomTransform(el).k), 1.6);
    await svg.focus();
    await h.page.keyboard.press("ArrowRight");
    assert((await svg.evaluate((el) => d3.zoomTransform(el).x)) < 0);
    await h.page.keyboard.press("Home");
    assert.equal(await svg.evaluate((el) => d3.zoomTransform(el).k), 1);
    for (let i = 0; i < 10; i++)
      await h.page
        .getByRole("button", { name: "Zoom in", exact: true })
        .click();
    assert.equal(await svg.evaluate((el) => d3.zoomTransform(el).k), 8);
    await h.page
      .getByRole("button", { name: "Reset view", exact: true })
      .click();
    assert.equal(await svg.evaluate((el) => d3.zoomTransform(el).k), 1);
    await h.page
      .getByRole("button", { name: "Expand map", exact: true })
      .click();
    assert.equal(await h.page.evaluate(() => expanded), true);
  } finally {
    await h.close();
  }
});
test("hostile text and private or executable place links remain inert", async () => {
  const h = await fixture();
  try {
    const data = source();
    data.origin.name = "<img src=x onerror=alert(1)>";
    data.origin.url = "javascript:alert(1)";
    data.pois[0].url = "http://127.0.0.1/secret";
    data.pois[1].url = "https://user:secret@example.com/";
    data.methodology.source = "<script>alert(1)</script>";
    data.roads.features[0].properties.name = "<svg onload=alert(1)>";
    await render(h.page, data);
    assert.equal(
      await h.page
        .locator("#root img,#root script,#root foreignObject")
        .count(),
      0,
    );
    assert.equal(
      await h.page
        .getByRole("link", { name: "View place", exact: true })
        .isVisible(),
      false,
    );
    await h.page
      .getByRole("button", { name: "Fixture shop, Shop", exact: true })
      .click();
    assert.equal(
      await h.page
        .getByRole("link", { name: "View place", exact: true })
        .isVisible(),
      false,
    );
    assert.deepEqual(h.external, []);
    assert.deepEqual(h.errors, []);
  } finally {
    await h.close();
  }
});
test("sourced places with null walking metadata render without walking claims", async () => {
  const h = await fixture();
  try {
    const data = source();
    data.catchments = [];
    for (const place of data.pois) delete place.walkMinutes;
    data.methodology.walkingMethod = null;
    data.methodology.walkingSpeedKmh = null;
    data.methodology.catchmentNote =
      "This source pack supplies place locations.";
    delete data.methodology.poiTimeNote;
    await render(h.page, data);
    assert.equal(
      await h.page.locator(".business-map").getAttribute("data-map-state"),
      "ready",
    );
    assert.equal(
      await h.page.locator('[data-layer="walking"] path').count(),
      0,
    );
    assert.equal(
      await h.page.locator('input[data-control="walking"]').isDisabled(),
      true,
    );
    assert.equal(
      (await h.page.locator(".business-map-provenance").textContent()).includes(
        "Estimated walking",
      ),
      false,
    );
    await h.page
      .getByRole("button", { name: "Fixture shop, Shop", exact: true })
      .click();
    assert.equal(
      (await h.page.locator(".business-map-selected").textContent()).includes(
        "min walk",
      ),
      false,
    );
    assert.equal(
      (
        await h.page.locator(".business-map-svg").getAttribute("aria-label")
      ).includes("walking"),
      false,
    );
    assert.equal(
      await h.page.locator(".business-map-method summary").textContent(),
      "Map sources",
    );
    await h.page.locator(".business-map-method summary").click();
    assert.equal(
      (
        await h.page.locator(".business-map-method dt").allTextContents()
      ).includes("Walking method"),
      false,
    );
    assert(
      (await h.page.locator(".business-map-method").textContent()).includes(
        "Retrieved 2026-10-05T12:00:00Z",
      ),
    );
    assert.deepEqual(h.errors, []);
    assert.deepEqual(h.external, []);
  } finally {
    await h.close();
  }
});
test("metric bounds accept a host-valid polar map and reject a diagonal over 20 km", async () => {
  const h = await fixture();
  try {
    const data = source();
    data.kind = "business_neighborhood";
    data.schema_version = 1;
    data.binding = {
      business_name: data.origin.name,
      profile_location: data.origin.address,
    };
    data.bbox = [-1.015, 84.98, 1.015, 85];
    data.origin.coordinates = [0, 84.99];
    data.pois = [];
    data.catchments = [];
    for (const layer of ["roads", "buildings", "green"])
      data[layer].features = [];
    data.stats = {
      totalBuildings: 0,
      taggedBuildings: 0,
      counts: { residential: 0, business: 0, mixed: 0, civic: 0, unknown: 0 },
    };
    await render(h.page, data);
    assert.equal(
      await h.page.locator(".business-map").getAttribute("data-map-state"),
      "ready",
    );
    assert.equal(
      await h.page
        .getByRole("button", { name: "Fixture cafe, Business", exact: true })
        .isVisible(),
      true,
    );
    assert.equal(
      await h.page
        .getByRole("combobox", { name: "Walking area", exact: true })
        .isDisabled(),
      true,
    );
    data.bbox = [-0.2, 84.8, 0.2, 85];
    data.origin.coordinates = [0, 84.9];
    await render(h.page, data);
    assert.equal(
      await h.page.locator(".business-map").getAttribute("data-map-state"),
      "error",
    );
    assert.equal(await h.page.locator("#root svg").count(), 0);
    assert.deepEqual(h.external, []);
    assert.deepEqual(h.errors, []);
  } finally {
    await h.close();
  }
});
test("invalid or excessive geometry is rejected before rendering paths", async () => {
  const h = await fixture();
  try {
    for (const change of [
      (d) => (d.origin.coordinates = [0, 91]),
      (d) => (d.bbox = [-180, -85, 180, 85]),
      (d) =>
        (d.pois = Array.from({ length: 201 }, (_, i) => ({
          ...d.pois[0],
          id: "place-" + i,
        }))),
      (d) =>
        (d.buildings.features[0].geometry = {
          type: "GeometryCollection",
          geometries: [],
        }),
      (d) => d.catchments.push(d.catchments[0]),
      (d) => (d.catchments[0].geometry.coordinates = []),
      (d) => (d.pois[0].coordinates = [0, 0]),
      (d) => (d.roads.features = Array(20001).fill(d.roads.features[0])),
      (d) =>
        (d.roads.features[0].geometry.coordinates = Array(250001).fill([
          -3, 40,
        ])),
    ]) {
      const data = source();
      change(data);
      await render(h.page, data);
      assert.equal(
        await h.page.locator(".business-map").getAttribute("data-map-state"),
        "error",
      );
      assert.equal(await h.page.locator("#root svg").count(), 0);
    }
    assert.deepEqual(h.external, []);
  } finally {
    await h.close();
  }
});
test("explicit and detached cleanup disconnect resize and zoom listeners", async () => {
  const h = await fixture();
  try {
    await render(h.page, source());
    await h.page.evaluate(() => RocotoBusinessMap.dispose(mapElement));
    assert.equal(
      await h.page.locator(".business-map").getAttribute("data-map-state"),
      "disposed",
    );
    assert.equal(
      await h.page
        .locator(".business-map-svg")
        .evaluate(
          (el) => (el.__on || []).filter((item) => item.name === "zoom").length,
        ),
      0,
    );
    await render(h.page, source());
    await h.page.evaluate(() => mapElement.remove());
    await h.page.waitForFunction(
      () => mapElement.dataset.mapState === "disposed",
    );
    assert.equal(
      await h.page.evaluate(
        () =>
          (mapElement.querySelector("svg").__on || []).filter(
            (item) => item.name === "zoom",
          ).length,
      ),
      0,
    );
  } finally {
    await h.close();
  }
});
test("touch pin selection and pinch zoom work on mobile without page overflow", async () => {
  const h = await fixture(390, true);
  try {
    await render(h.page, source());
    await h.page
      .getByRole("button", { name: "Fixture shop, Shop", exact: true })
      .tap();
    assert.equal(
      await h.page.locator(".business-map-selected strong").textContent(),
      "Fixture shop",
    );
    const bounds = await h.page.locator(".business-map-svg").boundingBox(),
      cx = bounds.x + bounds.width / 2,
      cy = bounds.y + bounds.height / 2,
      client = await h.context.newCDPSession(h.page);
    await client.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [
        { x: cx - 25, y: cy, id: 1 },
        { x: cx + 25, y: cy, id: 2 },
      ],
    });
    await client.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [
        { x: cx - 50, y: cy, id: 1 },
        { x: cx + 50, y: cy, id: 2 },
      ],
    });
    await client.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    assert(
      (await h.page
        .locator(".business-map-svg")
        .evaluate((el) => d3.zoomTransform(el).k)) > 1,
    );
    assert.equal(
      await h.page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
  } finally {
    await h.close();
  }
});
test("D3 local distribution matches its immutable manifest", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(base, "web/vendor/d3-7.9.0/manifest.json")),
  );
  assert.equal(manifest.version, "7.9.0");
  for (const entry of manifest.files) {
    const bytes = fs.readFileSync(path.join(base, "web/vendor", entry.path));
    assert.equal(bytes.length, entry.size_bytes);
    assert.equal(
      crypto.createHash("sha256").update(bytes).digest("hex"),
      entry.sha256,
    );
  }
});
test(
  "exact original reference data renders at desktop and mobile widths",
  { skip: !process.env.ROCOTO_MAP_REFERENCE_DATA },
  async () => {
    const data = JSON.parse(
      fs.readFileSync(process.env.ROCOTO_MAP_REFERENCE_DATA),
    );
    for (const width of [1600, 390]) {
      const h = await fixture(width, width === 390);
      try {
        await render(h.page, data);
        assert.equal(
          await h.page.locator(".business-map").getAttribute("data-map-state"),
          "ready",
        );
        assert.equal(
          await h.page.locator('[data-layer="buildings"] path').count(),
          data.buildings.features.length,
        );
        assert.equal(
          await h.page.locator('[data-layer="roads"] path').count(),
          data.roads.features.length,
        );
        assert.equal(
          await h.page.locator(".business-map-selected strong").textContent(),
          data.origin.name,
        );
        assert.equal(
          await h.page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          true,
        );
        if (process.env.ROCOTO_MAP_PROOF_DIR)
          await h.page.screenshot({
            path: path.join(
              process.env.ROCOTO_MAP_PROOF_DIR,
              `map-${width}.png`,
            ),
            fullPage: true,
          });
        assert.deepEqual(h.external, []);
        assert.deepEqual(h.errors, []);
      } finally {
        await h.close();
      }
    }
  },
);
