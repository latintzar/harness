/* The supplied neighbourhood renderer, adapted to scoped structured data and local D3. */
(() => {
  "use strict";
  const LIMITS = Object.freeze({
    features: 20000,
    coordinates: 250000,
    places: 200,
    catchments: 3,
  });
  const controllers = new WeakMap(),
    live = new Set();
  let dependency, removals;
  const svgNS = "http://www.w3.org/2000/svg";
  const make = (tag, cls = "", text) => {
    const el = document.createElement(tag);
    el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const names = {
    cafe: "Cafe",
    shop: "Shop",
    transit: "Transport",
    school: "Education",
    health: "Health",
    leisure: "Leisure",
  };
  const uses = {
    residential: "Residential",
    business: "Business",
    mixed: "Mixed",
    civic: "Civic",
    unknown: "Unclassified",
  };
  const fills = Object.fromEntries(
    Object.keys(uses).map((key) => [
      key,
      key === "unknown"
        ? "var(--border)"
        : `color-mix(in srgb,var(--map-${key}) 42%,var(--paper))`,
    ]),
  );
  const string = (value, maximum = 256) =>
    typeof value === "string" && value.length <= maximum ? value : "";
  function safeURL(value) {
    if (
      typeof value !== "string" ||
      value.length > 2048 ||
      /[\x00-\x20\x7f]/.test(value)
    )
      return "";
    try {
      const url = new URL(value),
        host = url.hostname.toLowerCase();
      if (
        !["https:", "http:"].includes(url.protocol) ||
        url.username ||
        url.password ||
        url.port ||
        !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$/.test(
          host,
        ) ||
        /(?:^|\.)(?:localhost|local|internal|invalid|test)$/.test(host)
      )
        return "";
      return url.href;
    } catch {
      return "";
    }
  }
  function validate(data) {
    if (!data || typeof data !== "object") throw Error("map");
    let points = 0,
      features = 0;
    const coordinate = (point) => {
      if (
        !Array.isArray(point) ||
        point.length !== 2 ||
        !point.every(Number.isFinite) ||
        Math.abs(point[0]) > 180 ||
        Math.abs(point[1]) > 85 ||
        point[0] < w ||
        point[0] > e ||
        point[1] < s ||
        point[1] > n ||
        ++points > LIMITS.coordinates
      )
        throw Error("coordinates");
      return [...point];
    };
    const list = (value, maximum, fn) => {
      if (!Array.isArray(value) || value.length > maximum) throw Error("list");
      return value.map(fn);
    };
    const line = (value) => {
      if (value.length && value.length < 2) throw Error("line");
      return list(value, LIMITS.coordinates, coordinate);
    };
    const ring = (value) => {
      if (
        value.length &&
        (value.length < 4 ||
          value[0][0] !== value.at(-1)[0] ||
          value[0][1] !== value.at(-1)[1])
      )
        throw Error("ring");
      return list(value, LIMITS.coordinates, coordinate);
    };
    const polygon = (value) => list(value, 10000, ring);
    function geometry(value, allowed) {
      if (!value || !allowed.includes(value.type)) throw Error("geometry");
      const parsers = {
        LineString: line,
        MultiLineString: (v) => list(v, 10000, line),
        Polygon: polygon,
        MultiPolygon: (v) => list(v, 10000, polygon),
      };
      const result = {
        type: value.type,
        coordinates: parsers[value.type](value.coordinates),
      };
      if (result.type === "Polygon" || result.type === "MultiPolygon") {
        const normalize = (rings) =>
          rings.map((points, index) => {
            let area = 0;
            for (let i = 0; i < points.length - 1; i++)
              area +=
                points[i][0] * points[i + 1][1] -
                points[i + 1][0] * points[i][1];
            return (index === 0 ? area > 0 : area < 0)
              ? points.reverse()
              : points;
          });
        result.coordinates =
          result.type === "Polygon"
            ? normalize(result.coordinates)
            : result.coordinates.map(normalize);
      }
      return result;
    }
    function collection(value, allowed) {
      if (!value || value.type !== "FeatureCollection")
        throw Error("collection");
      return list(value.features, LIMITS.features, (feature) => {
        if (++features > LIMITS.features || feature?.type !== "Feature")
          throw Error("features");
        const properties = feature.properties || {};
        return {
          type: "Feature",
          geometry: geometry(feature.geometry, allowed),
          properties: {
            name: string(properties.name),
            kind: string(properties.kind, 32),
            highway: string(properties.highway, 32),
            use: Object.hasOwn(uses, properties.use)
              ? properties.use
              : "unknown",
          },
        };
      });
    }
    if (
      !Array.isArray(data.bbox) ||
      data.bbox.length !== 4 ||
      !data.bbox.every(Number.isFinite)
    )
      throw Error("bbox");
    const [w, s, e, n] = data.bbox;
    if (
      w < -180 ||
      e > 180 ||
      s < -85 ||
      n > 85 ||
      e <= w ||
      n <= s ||
      Math.hypot(
        (e - w) * 111320 * Math.cos(((s + n) / 2) * (Math.PI / 180)),
        (n - s) * 111320,
      ) > 20000
    )
      throw Error("bbox");
    const origin = {
      id: "origin",
      name: string(data.origin?.name),
      address: string(data.origin?.address, 512),
      source: string(data.origin?.source, 512),
      url: safeURL(data.origin?.url),
      coordinates: coordinate(data.origin?.coordinates),
      category: "origin",
      walkMinutes: 0,
    };
    if (
      !origin.name ||
      origin.coordinates[0] < w ||
      origin.coordinates[0] > e ||
      origin.coordinates[1] < s ||
      origin.coordinates[1] > n
    )
      throw Error("origin");
    const ids = new Set(["origin"]);
    const pois = list(data.pois, LIMITS.places, (place) => {
      if (
        !string(place?.id, 128) ||
        ids.has(place.id) ||
        !string(place.name) ||
        !Object.hasOwn(names, place.category)
      )
        throw Error("place");
      ids.add(place.id);
      return {
        id: place.id,
        name: place.name,
        category: place.category,
        coordinates: coordinate(place.coordinates),
        url: safeURL(place.url),
        walkMinutes:
          Number.isFinite(place.walkMinutes) &&
          place.walkMinutes >= 0 &&
          place.walkMinutes <= 240
            ? place.walkMinutes
            : null,
      };
    });
    const minutes = new Set(),
      catchments = list(data.catchments, LIMITS.catchments, (item) => {
        if (![5, 10, 15].includes(item?.minutes) || minutes.has(item.minutes))
          throw Error("catchment");
        minutes.add(item.minutes);
        const shape = geometry(item.geometry, ["Polygon", "MultiPolygon"]);
        if (!shape.coordinates.flat(shape.type === "Polygon" ? 1 : 2).length)
          throw Error("catchment");
        return { minutes: item.minutes, geometry: shape };
      });
    const roads = collection(data.roads, ["LineString", "MultiLineString"]),
      buildings = collection(data.buildings, ["Polygon", "MultiPolygon"]),
      green = collection(data.green, ["Polygon", "MultiPolygon"]);
    const method = data.methodology || {},
      methodology = {};
    for (const key of [
      "source",
      "sourceTimestamp",
      "retrievedAt",
      "attribution",
      "catchmentNote",
      "poiTimeNote",
      "buildingUseNote",
      "projectionNote",
    ])
      methodology[key] = string(method[key], 4096);
    methodology.walkingMethod = ["network", "straight_line_estimate"].includes(
      method.walkingMethod,
    )
      ? method.walkingMethod
      : "";
    methodology.uncertainty = list(method.uncertainty || [], 16, (item) =>
      string(item, 4096),
    ).filter(Boolean);
    methodology.license = safeURL(method.license);
    methodology.walkingSpeedKmh =
      Number.isFinite(method.walkingSpeedKmh) &&
      method.walkingSpeedKmh > 0 &&
      method.walkingSpeedKmh <= 20
        ? method.walkingSpeedKmh
        : null;
    const sources = list(data.sources || [], 24, (source) => ({
      url: safeURL(source?.url),
      retrieved_at: string(source?.retrieved_at, 64),
      title: string(source?.title),
    }));
    const total = data.stats?.totalBuildings,
      tagged = data.stats?.taggedBuildings;
    const stats =
      Number.isSafeInteger(total) &&
      Number.isSafeInteger(tagged) &&
      total >= 0 &&
      tagged >= 0 &&
      tagged <= total
        ? { totalBuildings: total, taggedBuildings: tagged }
        : null;
    return {
      origin,
      bbox: [w, s, e, n],
      pois,
      catchments,
      roads,
      buildings,
      green,
      methodology,
      sources,
      stats,
    };
  }
  function loadD3() {
    if (window.d3?.version === "7.9.0") return Promise.resolve(window.d3);
    if (!dependency)
      dependency = new Promise((resolve) => {
        const script = document.createElement("script");
        script.src = "/static/vendor/d3-7.9.0/d3.min.js";
        script.async = true;
        const finish = () => {
          clearTimeout(timer);
          script.onload = script.onerror = null;
          resolve(window.d3?.version === "7.9.0" ? window.d3 : null);
        };
        const timer = setTimeout(finish, 8000);
        script.onload = script.onerror = finish;
        document.head.append(script);
      });
    return dependency;
  }
  function glyph(category) {
    const paths = {
      origin: [
        "M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z",
        "M15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
      ],
      cafe: [
        "M4 8h12v9a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4Z",
        "M16 9h2a3 3 0 0 1 0 6h-2M7 3v2m4-2v2m4-2v2",
      ],
      shop: ["M5 8h14l1 13H4Z", "M8 8V6a4 4 0 0 1 8 0v2"],
      transit: [
        "M5 3h14v15H5ZM5 11h14M9 3v8M15 3v8M7 18l-2 3m12-3 2 3M8 15h1m6 0h1",
      ],
      school: ["m2 9 10-6 10 6-10 6Z", "M6 12v6l6 3 6-3v-6M22 9v8"],
      health: [
        "M20 5a5 5 0 0 0-8 1 5 5 0 0 0-8-1c-5 6 8 15 8 15S25 11 20 5Z",
        "m5 12 3-1 2 3 3-6 2 3h4",
      ],
      leisure: ["m8 2 5 8h-3l4 6H2l4-6H3Z", "M8 16v6M17 5l5 10h-4v7"],
    };
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", "business-map-glyph");
    for (const path of paths[category] || paths.cafe) {
      const el = document.createElementNS(svgNS, "path");
      el.setAttribute("d", path);
      svg.append(el);
    }
    return svg;
  }
  function dispose(root) {
    controllers.get(root)?.();
    controllers.delete(root);
    live.delete(root);
    if (!live.size) {
      removals?.disconnect();
      removals = null;
    }
  }
  function render(input, { node = make, icon, onExpand } = {}) {
    const root = node("section", "business-map");
    root.dataset.mapState = "loading";
    const status = node(
      "p",
      "business-map-status",
      "Loading neighborhood map…",
    );
    status.setAttribute("role", "status");
    root.append(status);
    let data;
    try {
      data = validate(input);
    } catch {
      root.dataset.mapState = "error";
      status.textContent = "Review the map’s source data.";
      return root;
    }
    let disposed = false,
      observer,
      svg,
      d3,
      gesture = false;
    controllers.set(root, () => {
      disposed = true;
      observer?.disconnect();
      svg?.on(".zoom", null);
      if (gesture && d3) {
        d3.select(window).on("mousemove.zoom mouseup.zoom", null);
        d3.dragEnable(window);
      }
      root.dataset.mapState = "disposed";
    });
    loadD3()
      .then((library) => {
        if (disposed) return;
        if (!library) {
          root.dataset.mapState = "error";
          status.textContent = "Refresh to load the map controls.";
          return;
        }
        d3 = library;
        const heading = node("div", "business-map-heading");
        heading.append(node("h3", "", `${data.origin.name} · neighborhood`));
        if (onExpand) {
          const button = node("button", "business-map-button", "Expand map");
          button.type = "button";
          if (icon) button.prepend(icon("fullscreen"));
          button.addEventListener("click", () => onExpand());
          heading.append(button);
        }
        const choices = node("div", "business-map-choices"),
          controls = {};
        function select(key, label, values) {
          const wrap = node("label", "business-map-field", label),
            element = node("select");
          element.dataset.control = key;
          element.setAttribute("aria-label", label);
          for (const [value, text] of values) {
            const option = node("option", "", text);
            option.value = String(value);
            element.append(option);
          }
          wrap.append(element);
          choices.append(wrap);
          controls[key] = element;
          return element;
        }
        select(
          "minutes",
          "Walking area",
          data.catchments.map((item) => [
            item.minutes,
            `${item.minutes} minutes`,
          ]),
        );
        select("category", "Amenities", [
          ["all", "All amenities"],
          ...Object.entries(names),
        ]);
        controls.minutes.disabled = !data.catchments.length;
        const layers = node("div", "business-map-layer-controls");
        for (const [key, label] of [
          ["land", "Land use"],
          ["amenities", "Amenities"],
          ["walking", "Walking area"],
        ]) {
          const wrap = node("label"),
            box = node("input");
          box.type = "checkbox";
          box.checked = true;
          box.dataset.control = key;
          wrap.append(box, node("span", "", label));
          layers.append(wrap);
          controls[key] = box;
        }
        controls.walking.disabled = !data.catchments.length;
        const zoomControls = node("div", "business-map-zoom");
        for (const [key, label, text] of [
          ["in", "Zoom in", "+"],
          ["out", "Zoom out", "−"],
          ["reset", "Reset view", "Reset view"],
        ]) {
          const button = node("button", "business-map-button", text);
          button.type = "button";
          button.dataset.zoom = key;
          button.setAttribute("aria-label", label);
          zoomControls.append(button);
        }
        layers.append(zoomControls);
        const frame = node("div", "business-map-frame"),
          svgElement = document.createElementNS(svgNS, "svg");
        const mapContents = [
          "business location",
          ...(data.roads.length ? ["mapped streets"] : []),
          ...(data.buildings.length ? ["building land use"] : []),
          ...(data.pois.length ? ["places"] : []),
          ...(data.catchments.length ? ["estimated walking areas"] : []),
        ];
        svgElement.classList.add("business-map-svg");
        svgElement.setAttribute("role", "img");
        svgElement.setAttribute(
          "aria-label",
          `Neighborhood around ${data.origin.name}; ${mapContents.join(", ")}`,
        );
        svgElement.setAttribute("tabindex", "0");
        const markerLayer = node("div", "business-map-markers");
        markerLayer.setAttribute("role", "group");
        markerLayer.setAttribute("aria-label", "Places on the map");
        const scale = node("div", "business-map-scale"),
          scaleLabel = node("span"),
          scaleLine = node("span", "business-map-scale-line");
        scale.setAttribute("aria-hidden", "true");
        scale.append(scaleLabel, scaleLine);
        frame.append(svgElement, markerLayer, scale);
        const selected = node("div", "business-map-selected"),
          placeName = node("strong"),
          placeDetail = node("span"),
          placeLink = node("a", "", "View place");
        placeLink.target = "_blank";
        placeLink.rel = "noopener noreferrer";
        selected.setAttribute("aria-live", "polite");
        selected.append(placeName, placeDetail, placeLink);
        const legend = node("div", "business-map-legend");
        legend.setAttribute("aria-label", "Building land use legend");
        for (const [key, label] of Object.entries(uses)) {
          const item = node("span"),
            swatch = node("i");
          swatch.style.background = fills[key];
          item.append(swatch, document.createTextNode(label));
          legend.append(item);
        }
        const provenance = node("div", "business-map-provenance");
        if (data.stats)
          provenance.append(
            node(
              "span",
              "",
              `${data.stats.taggedBuildings.toLocaleString()} of ${data.stats.totalBuildings.toLocaleString()} buildings classified`,
            ),
          );
        if (data.methodology.walkingSpeedKmh)
          provenance.append(
            node(
              "span",
              "",
              `Estimated walking · ${data.methodology.walkingSpeedKmh} km/h`,
            ),
          );
        if (data.methodology.license) {
          const a = node(
            "a",
            "",
            data.methodology.attribution || "Map attribution",
          );
          a.href = data.methodology.license;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          provenance.append(a);
        }
        const method = node("details", "business-map-method");
        const hasWalking =
          data.catchments.length ||
          data.pois.some((place) => place.walkMinutes !== null);
        method.append(
          node(
            "summary",
            "",
            hasWalking ? "Sources and walking estimates" : "Map sources",
          ),
        );
        const methodList = node("dl");
        for (const [key, label] of [
          ["source", "Map source"],
          ["sourceTimestamp", "Source date"],
          ["retrievedAt", "Retrieved"],
          ["walkingMethod", "Walking method"],
          ["catchmentNote", "Walking area"],
          ["poiTimeNote", "Place walking times"],
          ["buildingUseNote", "Land use"],
          ["projectionNote", "Data preparation"],
        ]) {
          const raw = data.methodology[key],
            value =
              key === "walkingMethod"
                ? {
                    network: "Walking network",
                    straight_line_estimate: "Straight-line estimate",
                  }[raw] || ""
                : raw;
          if (value || key === "sourceTimestamp") {
            const pair = node("div");
            pair.append(
              node("dt", "", label),
              node("dd", "", value || "Source date awaiting record"),
            );
            methodList.append(pair);
          }
        }
        method.append(methodList);
        if (data.methodology.uncertainty.length) {
          const caveats = node("ul");
          for (const text of data.methodology.uncertainty)
            caveats.append(node("li", "", text));
          method.append(caveats);
        }
        for (const source of data.sources) {
          if (!source.url) continue;
          const row = node("p"),
            a = node("a", "", source.title || new URL(source.url).hostname);
          a.href = source.url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          row.append(a);
          if (source.retrieved_at)
            row.append(
              document.createTextNode(` · Retrieved ${source.retrieved_at}`),
            );
          method.append(row);
        }
        root.replaceChildren(
          heading,
          choices,
          layers,
          frame,
          selected,
          legend,
          provenance,
          method,
        );
        svg = d3.select(svgElement);
        const mapGroup = svg
            .append("g")
            .attr("class", "business-map-geography"),
          greenGroup = mapGroup.append("g").attr("data-layer", "green"),
          walkGroup = mapGroup.append("g").attr("data-layer", "walking"),
          buildingGroup = mapGroup.append("g").attr("data-layer", "buildings"),
          roadGroup = mapGroup.append("g").attr("data-layer", "roads"),
          labelGroup = svg.append("g").attr("class", "business-map-labels");
        const state = {
          minutes: data.catchments.some((item) => item.minutes === 10)
            ? 10
            : data.catchments[0]?.minutes,
          category: "all",
          land: true,
          amenities: true,
          walking: Boolean(data.catchments.length),
          selected: "origin",
          zoom: { k: 1, x: 0, y: 0 },
        };
        controls.minutes.value = String(state.minutes);
        controls.walking.checked = state.walking;
        let width = 0,
          height = 0,
          projection,
          geoPath,
          transform = d3.zoomIdentity;
        const places = [data.origin, ...data.pois],
          pins = places.map((place) => {
            const button = node(
              "button",
              "business-map-pin" +
                (place.id === "origin" ? " business-map-origin" : ""),
            );
            button.type = "button";
            button.title = place.name;
            button.setAttribute(
              "aria-label",
              `${place.name}, ${place.id === "origin" ? "Business" : names[place.category]}`,
            );
            button.append(glyph(place.category));
            button.addEventListener("click", () => {
              if (disposed) return;
              state.selected = place.id;
              renderSelection();
              positionPins();
            });
            markerLayer.append(button);
            return { place, button };
          });
        function renderSelection() {
          const place =
            places.find((item) => item.id === state.selected) || data.origin;
          placeName.textContent = place.name;
          const detail = [
            place.id === "origin"
              ? place.address || "Starting location"
              : names[place.category],
          ];
          if (place.id !== "origin" && place.walkMinutes !== null)
            detail.push(
              `~${Math.max(1, Math.round(place.walkMinutes))} min walk`,
            );
          placeDetail.textContent = detail.join(" · ");
          placeLink.hidden = !place.url;
          if (place.url) placeLink.href = place.url;
          else placeLink.removeAttribute("href");
          pins.forEach((item) =>
            item.button.setAttribute(
              "aria-pressed",
              String(item.place.id === state.selected),
            ),
          );
        }
        function positionPins() {
          if (!projection) return;
          const placed = [],
            target = matchMedia("(pointer:coarse)").matches ? 46 : 34;
          [...pins]
            .sort(
              (a, b) =>
                (b.place.id === state.selected
                  ? 2
                  : b.place.id === "origin"
                    ? 1
                    : 0) -
                (a.place.id === state.selected
                  ? 2
                  : a.place.id === "origin"
                    ? 1
                    : 0),
            )
            .forEach(({ place, button }) => {
              const point = transform.apply(projection(place.coordinates));
              let visible =
                place.id === "origin" ||
                (state.amenities &&
                  (state.category === "all" ||
                    place.category === state.category));
              visible =
                visible &&
                point[0] > target / 2 &&
                point[0] < width - target / 2 &&
                point[1] > target / 2 &&
                point[1] < height - target / 2;
              if (
                visible &&
                place.id !== state.selected &&
                place.id !== "origin" &&
                placed.some(
                  (p) => Math.hypot(p[0] - point[0], p[1] - point[1]) < target,
                )
              )
                visible = false;
              button.hidden = !visible;
              if (visible) {
                placed.push(point);
                button.style.left = point[0] + "px";
                button.style.top = point[1] + "px";
              }
            });
        }
        const roadLabels = [],
          usedNames = new Set(),
          priority = {
            primary: 0,
            secondary: 1,
            tertiary: 2,
            residential: 3,
            unclassified: 4,
            pedestrian: 5,
          };
        data.roads
          .slice()
          .sort(
            (a, b) =>
              (priority[a.properties.highway || a.properties.kind] ?? 6) -
              (priority[b.properties.highway || b.properties.kind] ?? 6),
          )
          .forEach((feature) => {
            const name = feature.properties.name;
            if (!name || usedNames.has(name)) return;
            const lines =
                feature.geometry.type === "LineString"
                  ? [feature.geometry.coordinates]
                  : feature.geometry.coordinates,
              line = lines.slice().sort((a, b) => b.length - a.length)[0];
            if (!line || line.length < 2) return;
            usedNames.add(name);
            roadLabels.push({
              name,
              coordinates: line[Math.floor(line.length / 2)],
            });
          });
        function labels() {
          labelGroup.selectAll("*").remove();
          const taken = [];
          for (const label of roadLabels) {
            if (taken.length >= (width < 480 ? 6 : 11)) break;
            const p = transform.apply(projection(label.coordinates)),
              half = label.name.length * 3.6;
            if (
              p[0] < half + 9 ||
              p[0] > width - half - 9 ||
              p[1] < 16 ||
              p[1] > height - 30 ||
              taken.some(
                (t) =>
                  Math.abs(t.x - p[0]) < t.half + half + 14 &&
                  Math.abs(t.y - p[1]) < 25,
              )
            )
              continue;
            taken.push({ x: p[0], y: p[1], half });
            labelGroup
              .append("text")
              .attr("class", "business-map-street-label")
              .attr("text-anchor", "middle")
              .attr("x", p[0])
              .attr("y", p[1])
              .text(label.name);
          }
        }
        function scaleBar() {
          if (!projection) return;
          const meters = transform.k > 4 ? 50 : transform.k > 2 ? 100 : 200,
            at = data.origin.coordinates,
            offset = meters / (111320 * Math.cos((at[1] * Math.PI) / 180)),
            a = projection(at),
            b = projection([at[0] + offset, at[1]]);
          scaleLine.style.width = Math.abs(b[0] - a[0]) * transform.k + "px";
          scaleLabel.textContent = meters + " m";
        }
        function updateLayers() {
          buildingGroup
            .selectAll("path")
            .attr("fill", (f) =>
              state.land ? fills[f.properties.use] : fills.unknown,
            );
          const chosen = data.catchments.find(
            (c) => c.minutes === state.minutes,
          );
          walkGroup
            .selectAll("path")
            .data(chosen ? [chosen.geometry] : [])
            .join("path")
            .attr("d", geoPath)
            .attr("fill", "var(--map-walking)")
            .attr("fill-opacity", 0.09)
            .attr("stroke", "var(--map-walking)")
            .attr("stroke-opacity", 0.6)
            .attr("stroke-width", 1.5)
            .attr("vector-effect", "non-scaling-stroke");
          walkGroup.attr("display", state.walking ? null : "none");
          legend.hidden = !state.land;
          positionPins();
        }
        const zoom = d3
          .zoom()
          .scaleExtent([1, 8])
          .filter(
            (event) =>
              !event.button &&
              (event.type !== "wheel" || event.ctrlKey || event.metaKey),
          )
          .on("start", (event) => {
            gesture = Boolean(event.sourceEvent);
          })
          .on("zoom", (event) => {
            if (disposed) return;
            transform = event.transform;
            mapGroup.attr("transform", transform);
            state.zoom = {
              k: transform.k,
              x: transform.x / (width || 1),
              y: transform.y / (height || 1),
            };
            positionPins();
            labels();
            scaleBar();
          })
          .on("end", () => {
            gesture = false;
          });
        function resize() {
          if (disposed) return;
          if (!root.isConnected) {
            if (width) dispose(root);
            return;
          }
          const rect = frame.getBoundingClientRect();
          if (!rect.width) return;
          const nextWidth = Math.round(rect.width),
            nextHeight = Math.round(rect.height);
          if (width === nextWidth && height === nextHeight && projection)
            return;
          width = nextWidth;
          height = nextHeight;
          let [west, south, east, north] = data.bbox;
          const chosen = data.catchments.find(
            (c) => c.minutes === state.minutes,
          );
          if (chosen) {
            const bounds = d3.geoBounds(chosen.geometry);
            if (bounds.flat().every(Number.isFinite)) {
              west = bounds[0][0];
              south = bounds[0][1];
              east = bounds[1][0];
              north = bounds[1][1];
              const xPad = (east - west) * 0.07,
                yPad = (north - south) * 0.07;
              west -= xPad;
              east += xPad;
              south -= yPad;
              north += yPad;
            }
          }
          projection = d3.geoMercator().fitExtent(
            [
              [16, 16],
              [width - 16, height - 16],
            ],
            {
              type: "MultiPoint",
              coordinates: [
                [west, south],
                [east, north],
              ],
            },
          );
          geoPath = d3.geoPath(projection);
          svg.attr("viewBox", `0 0 ${width} ${height}`);
          greenGroup
            .selectAll("path")
            .data(data.green)
            .join("path")
            .attr("d", geoPath)
            .attr("fill", "var(--map-green)")
            .attr("fill-opacity", 0.1);
          buildingGroup
            .selectAll("path")
            .data(data.buildings)
            .join("path")
            .attr("d", geoPath)
            .attr("stroke", "var(--border)")
            .attr("stroke-width", 0.45)
            .attr("vector-effect", "non-scaling-stroke");
          roadGroup
            .selectAll("path")
            .data(data.roads)
            .join("path")
            .attr("d", geoPath)
            .attr("fill", "none")
            .attr("stroke", "var(--border)")
            .attr("stroke-width", (f) =>
              ["primary", "secondary"].includes(
                f.properties.highway || f.properties.kind,
              )
                ? 2
                : 1,
            )
            .attr("vector-effect", "non-scaling-stroke");
          zoom
            .extent([
              [0, 0],
              [width, height],
            ])
            .translateExtent([
              [0, 0],
              [width, height],
            ]);
          svg.call(zoom);
          const saved = { ...state.zoom };
          svg.call(
            zoom.transform,
            d3.zoomIdentity
              .translate(saved.x * width, saved.y * height)
              .scale(saved.k),
          );
          updateLayers();
          root.dataset.mapState = "ready";
        }
        Object.entries(controls).forEach(([key, el]) =>
          el.addEventListener("change", () => {
            if (disposed) return;
            state[key] =
              el.type === "checkbox"
                ? el.checked
                : key === "minutes"
                  ? Number(el.value)
                  : el.value;
            if (key === "minutes") {
              state.zoom = { k: 1, x: 0, y: 0 };
              width = 0;
              resize();
            }
            updateLayers();
          }),
        );
        zoomControls.querySelectorAll("button").forEach((button) =>
          button.addEventListener("click", () => {
            if (disposed) return;
            if (button.dataset.zoom === "reset")
              svg.call(zoom.transform, d3.zoomIdentity);
            else
              svg.call(
                zoom.scaleBy,
                button.dataset.zoom === "in" ? 1.6 : 1 / 1.6,
              );
          }),
        );
        svgElement.addEventListener("keydown", (event) => {
          if (disposed) return;
          if (
            [
              "ArrowLeft",
              "ArrowRight",
              "ArrowUp",
              "ArrowDown",
              "+",
              "=",
              "-",
              "Home",
            ].includes(event.key)
          ) {
            event.preventDefault();
            if (event.key === "Home") svg.call(zoom.transform, d3.zoomIdentity);
            else if (["+", "=", "-"].includes(event.key))
              svg.call(zoom.scaleBy, event.key === "-" ? 1 / 1.6 : 1.6);
            else
              svg.call(
                zoom.translateBy,
                event.key === "ArrowLeft"
                  ? 40
                  : event.key === "ArrowRight"
                    ? -40
                    : 0,
                event.key === "ArrowUp"
                  ? 40
                  : event.key === "ArrowDown"
                    ? -40
                    : 0,
              );
          }
        });
        renderSelection();
        observer = new ResizeObserver(resize);
        observer.observe(frame);
        resize();
        live.add(root);
        if (!removals) {
          removals = new MutationObserver(() => {
            for (const map of live) if (!map.isConnected) dispose(map);
          });
          removals.observe(document.body, { childList: true, subtree: true });
        }
      })
      .catch(() => {
        if (!disposed) {
          dispose(root);
          root.dataset.mapState = "error";
          root.replaceChildren(
            node("p", "business-map-status", "Review the map’s source data."),
          );
        }
      });
    return root;
  }
  window.RocotoBusinessMap = Object.freeze({ render, dispose, LIMITS });
})();
