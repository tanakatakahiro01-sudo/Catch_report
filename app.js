const app = document.querySelector("#app");
const headerControls = document.querySelector("#header-controls");
const numberFormatter = new Intl.NumberFormat("ja-JP");
const ENABLE_FISH_ILLUSTRATIONS = true;
const APP_ASSET_VERSION = "20260630-split-detail-load-v1";
const STATIC_STATISTICS_URL = new URL(
  `./data/statistics.json?v=${APP_ASSET_VERSION}`,
  window.location.href
).toString();
const STATIC_DETAIL_STATISTICS_URL = new URL(
  `./data/detail_statistics.json?v=${APP_ASSET_VERSION}`,
  window.location.href
).toString();
const STATIC_DETAIL_SPOTS_BASE_URL = new URL("./data/detail_spots/", window.location.href).toString();
const STATIC_NEXT_6H_BASE_URL = new URL("./data/next6h/", window.location.href).toString();
const STATIC_ILLUSTRATIONS_URL = new URL(
  `./data/fish_illustrations.json?v=${APP_ASSET_VERSION}`,
  window.location.href
).toString();
const STATIC_AFFILIATE_URL = new URL(
  `./data/affiliate_url.json?v=${APP_ASSET_VERSION}`,
  window.location.href
).toString();
const STATIC_AFFILIATE_FALLBACKS_URL = new URL(
  `./data/affiliate_fallbacks.json?v=${APP_ASSET_VERSION}`,
  window.location.href
).toString();
const API_STATISTICS_URL = new URL("./api/statistics", window.location.href).toString();
const API_DETAIL_STATISTICS_URL = new URL(
  "./api/detail-statistics",
  window.location.href
).toString();
let SPOTS = [];
let CATCH_RECORDS = [];
let SPOT_TOTAL_COUNTS = new Map();
let FISH_ILLUSTRATION_PATHS = {};
let FISH_AFFILIATE_URLS = {};
let FISH_AFFILIATE_FALLBACKS = {};
let DETAIL_MONTHLY_TOP3 = {};
let DETAIL_SPOT_FISH_TIME_COUNTS = {};
let DETAIL_STATISTICS_METADATA = {};
let DETAIL_NEXT_6H_TOP3 = {};
let DETAIL_STATISTICS_LOADED = false;
let NEXT_6H_STATISTICS_LOADED = false;
let next6hStatisticsPromise = null;
let detailStatisticsPromise = null;
const spotDetailStatisticsPromises = new Map();
let selectedSpotId = null;

const state = {
  fishQuery: "",
  month: "all",
  minSpotCount: 100
};

const mapView = {
  zoom: 1,
  centerX: 380,
  centerY: 310
};
const MAP_MAX_ZOOM = 256;
const TILE_SIZE = 256;
const AERIAL_TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg";
const MAP_STYLE_URL = "https://tiles.stadiamaps.com/styles/outdoors.json";
const MAIN_MAP_FRAME = { x: 165, y: 20, width: 570, height: 570 };
const MAIN_MAP_BOUNDS = { minLng: 128, maxLng: 146.5, minLat: 30, maxLat: 46 };
const AERIAL_MAP_FRAME = { x: 0, y: 0, width: 760, height: 620 };
const AERIAL_MAP_BOUNDS = { minLng: 122.5, maxLng: 154, minLat: 20, maxLat: 46 };
let mapZoomAnimation = null;
let mapResizeAnimation = null;
let activeMap = null;
let mapCameraState = null;

let fishNames = [];

function formatCount(value) {
  return numberFormatter.format(value);
}

function formatJapaneseDate(value) {
  if (!value || typeof value !== "string") return "";
  const [year, month, day] = value.split("-").map((part) => Number(part));
  if (!year || !month || !day) return value;
  return `${year}年${month}月${day}日`;
}

function detailStatisticsPeriodText() {
  const oldest = formatJapaneseDate(DETAIL_STATISTICS_METADATA.oldest);
  const newest = formatJapaneseDate(DETAIL_STATISTICS_METADATA.newest);
  if (!oldest || !newest) return "";
  return `集計期間: ${oldest}〜${newest}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fishIllustrationUrl(name) {
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 120" role="img" aria-label="${name}">
      <rect width="160" height="120" rx="18" fill="#f7f5ee"/>
      <g transform="translate(26 22)" fill="none" stroke="#173b3c" stroke-linecap="round" stroke-linejoin="round">
        <path stroke-width="6.4" d="M20 40c22-29 54-35 84-10-26 31-58 36-84 10Z"></path>
        <path stroke-width="6.4" d="m23 40-15-15v30l15-15Z"></path>
        <circle cx="85" cy="30" r="4.2" fill="#173b3c" stroke="none"></circle>
      </g>
    </svg>
  `.trim();
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function currentTargetIllustrationUrl(name) {
  return FISH_ILLUSTRATION_PATHS[name] || fishIllustrationUrl(name);
}

function removeActiveMap() {
  if (!activeMap) return;
  mapCameraState = {
    center: activeMap.getCenter().toArray(),
    zoom: activeMap.getZoom(),
    bearing: activeMap.getBearing(),
    pitch: activeMap.getPitch()
  };
  activeMap.remove();
  activeMap = null;
}

function setHeaderControlsVisible(visible) {
  headerControls.hidden = !visible;
  if (!visible) headerControls.innerHTML = "";
}

function setPageScrollLocked(locked) {
  document.body.classList.toggle("map-scroll-locked", locked);
}

window.addEventListener(
  "resize",
  () => {
    if (mapResizeAnimation) cancelAnimationFrame(mapResizeAnimation);
    mapResizeAnimation = requestAnimationFrame(() => {
      mapResizeAnimation = null;
      if (activeMap) activeMap.resize();
      updateMapView();
    });
  },
  { passive: true }
);

function recordsForSpot(spotId) {
  const spot = SPOTS.find((item) => item.id === spotId);
  if (!spot) return [];
  return CATCH_RECORDS.filter((record) => record.spot_id === spotId);
}

function sumCounts(records) {
  return records.reduce((sum, record) => sum + record.count, 0);
}

function filteredRecords() {
  return CATCH_RECORDS.filter((record) => {
    const matchesFish =
      state.fishQuery === "" || record.fish_name.includes(state.fishQuery);
    const matchesMonth = state.month === "all" || record.month === Number(state.month);
    return matchesFish && matchesMonth;
  });
}

function aggregateSpots(records) {
  const counts = new Map();
  records.forEach((record) => {
    counts.set(record.spot_id, (counts.get(record.spot_id) || 0) + record.count);
  });
  return SPOTS.filter(
    (spot) => counts.has(spot.id) && (SPOT_TOTAL_COUNTS.get(spot.id) || 0) >= state.minSpotCount
  ).map((spot) => ({
    ...spot,
    count: counts.get(spot.id),
    totalCount: SPOT_TOTAL_COUNTS.get(spot.id) || 0
  }));
}

function projectPoint(lat, lng) {
  let bounds = MAIN_MAP_BOUNDS;
  let frame = MAIN_MAP_FRAME;

  if (lat < 30 && lng < 133) {
    bounds = { minLng: 122.5, maxLng: 131, minLat: 23.5, maxLat: 30 };
    frame = { x: 20, y: 465, width: 145, height: 125 };
  } else if (lat < 30 || lng > 146.5) {
    bounds = { minLng: 136, maxLng: 154, minLat: 20, maxLat: 30 };
    frame = { x: 20, y: 55, width: 145, height: 90 };
  }

  return {
    x: frame.x + ((lng - bounds.minLng) / (bounds.maxLng - bounds.minLng)) * frame.width,
    y: frame.y + ((bounds.maxLat - lat) / (bounds.maxLat - bounds.minLat)) * frame.height
  };
}

function svgPointToLatLng(x, y, frame = MAIN_MAP_FRAME, bounds = MAIN_MAP_BOUNDS) {
  return {
    lat:
      bounds.maxLat - ((y - frame.y) / frame.height) * (bounds.maxLat - bounds.minLat),
    lng:
      bounds.minLng + ((x - frame.x) / frame.width) * (bounds.maxLng - bounds.minLng)
  };
}

function latLngToWorldPixel(lat, lng, zoom) {
  const sinLat = Math.sin((Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI) / 180);
  const scale = TILE_SIZE * 2 ** zoom;
  return {
    x: ((lng + 180) / 360) * scale,
    y: (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * scale
  };
}

function aerialTileZoom() {
  return Math.max(5, Math.min(11, Math.round(5 + Math.log2(mapView.zoom) * 0.72)));
}

function aerialTileUrl(zoom, x, y) {
  return AERIAL_TILE_URL.replace("{z}", zoom).replace("{x}", x).replace("{y}", y);
}

function aerialPointToSvg(lat, lng, view, worldNorthWest, worldWidth, worldHeight, zoom) {
  const worldPoint = latLngToWorldPixel(lat, lng, zoom);
  return {
    x: view.x + ((worldPoint.x - worldNorthWest.x) / worldWidth) * view.width,
    y: view.y + ((worldPoint.y - worldNorthWest.y) / worldHeight) * view.height
  };
}

function markerRadius() {
  return 18;
}

function markerColor(count, colorRanks) {
  const colors = ["#2c7bb6", "#00a6ca", "#f9d057", "#f28e2b", "#d62828"];
  if (colorRanks.size === 1) return colors[2];
  const ratio = colorRanks.get(count) / (colorRanks.size - 1);
  const index = Math.min(colors.length - 1, Math.floor(ratio * colors.length));
  return colors[index];
}

function mapViewBox() {
  const width = 760 / mapView.zoom;
  const height = 620 / mapView.zoom;
  const x = Math.max(0, Math.min(760 - width, mapView.centerX - width / 2));
  const y = Math.max(0, Math.min(620 - height, mapView.centerY - height / 2));
  mapView.centerX = x + width / 2;
  mapView.centerY = y + height / 2;
  return { x, y, width, height };
}

function markerScreenRadius(baseRadius) {
  const scale = Math.min(1, 0.16 + Math.log2(mapView.zoom) * 0.21);
  return Math.max(2.5, baseRadius * scale);
}

function updateLabelPositions(map) {
  map.querySelectorAll(".spot-label").forEach((label) => {
    const radius = markerScreenRadius(Number(label.dataset.baseRadius));
    const offset = (radius + 7) / mapView.zoom;
    const lineHeight = 13 / mapView.zoom;
    const x = Number(label.dataset.x);
    const y = Number(label.dataset.y);
    const placement = Number(label.dataset.placement) % 4;
    if (placement === 0) {
      label.setAttribute("x", (x + offset).toFixed(2));
      label.setAttribute("y", (y + 4 / mapView.zoom).toFixed(2));
      label.setAttribute("text-anchor", "start");
    } else if (placement === 1) {
      label.setAttribute("x", (x - offset).toFixed(2));
      label.setAttribute("y", (y + 4 / mapView.zoom).toFixed(2));
      label.setAttribute("text-anchor", "end");
    } else if (placement === 2) {
      label.setAttribute("x", x.toFixed(2));
      label.setAttribute("y", (y - offset).toFixed(2));
      label.setAttribute("text-anchor", "middle");
    } else {
      label.setAttribute("x", x.toFixed(2));
      label.setAttribute("y", (y + offset + lineHeight).toFixed(2));
      label.setAttribute("text-anchor", "middle");
    }
    label.setAttribute("font-size", (11 / mapView.zoom).toFixed(2));
    label.setAttribute("stroke-width", (3 / mapView.zoom).toFixed(2));
  });
}

function updateStandardMarkerPositions(map) {
  map.querySelectorAll(".spot-marker").forEach((marker) => {
    marker.setAttribute("cx", marker.dataset.standardX);
    marker.setAttribute("cy", marker.dataset.standardY);
  });
  map.querySelectorAll(".spot-label").forEach((label) => {
    label.dataset.x = label.dataset.standardX;
    label.dataset.y = label.dataset.standardY;
  });
}

function updateMapView() {
  const map = document.querySelector(".japan-map");
  if (!map) return;

  const view = mapViewBox();
  map.setAttribute(
    "viewBox",
    `${view.x.toFixed(2)} ${view.y.toFixed(2)} ${view.width.toFixed(2)} ${view.height.toFixed(2)}`
  );
  map.querySelectorAll(".spot-marker").forEach((marker) => {
    const radius = markerScreenRadius(Number(marker.dataset.baseRadius));
    marker.setAttribute("r", (radius / mapView.zoom).toFixed(5));
  });
  map.classList.toggle("show-spot-labels", mapView.zoom >= 8);
  if (state.mapLayer === "aerial") {
    updateAerialTiles(view);
  } else {
    updateStandardMarkerPositions(map);
    updateLabelPositions(map);
    updateAerialTiles(view);
  }
}

function updateAerialTiles(view = mapViewBox()) {
  const layer = document.querySelector(".aerial-tile-layer");
  const map = document.querySelector(".japan-map");
  if (!layer || !map) return;

  layer.hidden = state.mapLayer !== "aerial";
  map.classList.toggle("is-aerial-mode", state.mapLayer === "aerial");
  if (state.mapLayer !== "aerial") {
    layer.replaceChildren();
    layer.dataset.tileKey = "";
    return;
  }

  const rect = map.getBoundingClientRect();
  if (!rect.width || !rect.height) return;

  const northWest = svgPointToLatLng(view.x, view.y, AERIAL_MAP_FRAME, AERIAL_MAP_BOUNDS);
  const southEast = svgPointToLatLng(
    view.x + view.width,
    view.y + view.height,
    AERIAL_MAP_FRAME,
    AERIAL_MAP_BOUNDS
  );
  const zoom = aerialTileZoom();
  const worldNorthWest = latLngToWorldPixel(northWest.lat, northWest.lng, zoom);
  const worldSouthEast = latLngToWorldPixel(southEast.lat, southEast.lng, zoom);
  const worldWidth = Math.max(1, worldSouthEast.x - worldNorthWest.x);
  const worldHeight = Math.max(1, worldSouthEast.y - worldNorthWest.y);
  map.querySelectorAll(".spot-marker").forEach((marker) => {
    const point = aerialPointToSvg(
      Number(marker.dataset.lat),
      Number(marker.dataset.lng),
      view,
      worldNorthWest,
      worldWidth,
      worldHeight,
      zoom
    );
    marker.setAttribute("cx", point.x.toFixed(2));
    marker.setAttribute("cy", point.y.toFixed(2));
  });
  map.querySelectorAll(".spot-label").forEach((label) => {
    const point = aerialPointToSvg(
      Number(label.dataset.lat),
      Number(label.dataset.lng),
      view,
      worldNorthWest,
      worldWidth,
      worldHeight,
      zoom
    );
    label.dataset.x = point.x.toFixed(2);
    label.dataset.y = point.y.toFixed(2);
  });
  updateLabelPositions(map);
  const maxTile = 2 ** zoom;
  const minTileX = Math.floor(worldNorthWest.x / TILE_SIZE) - 1;
  const maxTileX = Math.floor(worldSouthEast.x / TILE_SIZE) + 1;
  const minTileY = Math.max(0, Math.floor(worldNorthWest.y / TILE_SIZE) - 1);
  const maxTileY = Math.min(maxTile - 1, Math.floor(worldSouthEast.y / TILE_SIZE) + 1);
  const tileKey = [
    zoom,
    minTileX,
    maxTileX,
    minTileY,
    maxTileY,
    Math.round(worldNorthWest.x),
    Math.round(worldNorthWest.y),
    Math.round(worldWidth),
    Math.round(worldHeight),
    Math.round(rect.width),
    Math.round(rect.height)
  ].join(":");
  if (layer.dataset.tileKey === tileKey) return;
  layer.dataset.tileKey = tileKey;

  const fragment = document.createDocumentFragment();
  for (let tileX = minTileX; tileX <= maxTileX; tileX += 1) {
    const wrappedTileX = ((tileX % maxTile) + maxTile) % maxTile;
    for (let tileY = minTileY; tileY <= maxTileY; tileY += 1) {
      const image = document.createElement("img");
      image.src = aerialTileUrl(zoom, wrappedTileX, tileY);
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.style.left = `${((tileX * TILE_SIZE - worldNorthWest.x) / worldWidth) * rect.width}px`;
      image.style.top = `${((tileY * TILE_SIZE - worldNorthWest.y) / worldHeight) * rect.height}px`;
      image.style.width = `${(TILE_SIZE / worldWidth) * rect.width + 1}px`;
      image.style.height = `${(TILE_SIZE / worldHeight) * rect.height + 1}px`;
      fragment.appendChild(image);
    }
  }
  layer.replaceChildren(fragment);
}

function setMapZoom(nextZoom, focusX = mapView.centerX, focusY = mapView.centerY) {
  if (mapZoomAnimation) {
    cancelAnimationFrame(mapZoomAnimation);
    mapZoomAnimation = null;
  }
  const previous = mapViewBox();
  const zoom = Math.max(1, Math.min(MAP_MAX_ZOOM, nextZoom));
  if (zoom === mapView.zoom) return;

  const nextWidth = 760 / zoom;
  const nextHeight = 620 / zoom;
  const relativeX = (focusX - previous.x) / previous.width;
  const relativeY = (focusY - previous.y) / previous.height;
  mapView.zoom = zoom;
  mapView.centerX = focusX + (0.5 - relativeX) * nextWidth;
  mapView.centerY = focusY + (0.5 - relativeY) * nextHeight;
  updateMapView();
}

function animateMapZoom(nextZoom, focusX = mapView.centerX, focusY = mapView.centerY) {
  if (mapZoomAnimation) cancelAnimationFrame(mapZoomAnimation);

  const previous = mapViewBox();
  const start = {
    zoom: mapView.zoom,
    centerX: mapView.centerX,
    centerY: mapView.centerY
  };
  const zoom = Math.max(1, Math.min(MAP_MAX_ZOOM, nextZoom));
  if (zoom === start.zoom) return;

  const nextWidth = 760 / zoom;
  const nextHeight = 620 / zoom;
  const relativeX = (focusX - previous.x) / previous.width;
  const relativeY = (focusY - previous.y) / previous.height;
  const target = {
    centerX: focusX + (0.5 - relativeX) * nextWidth,
    centerY: focusY + (0.5 - relativeY) * nextHeight
  };
  const startedAt = performance.now();
  const duration = 180;

  const tick = (now) => {
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    mapView.zoom = start.zoom * Math.pow(zoom / start.zoom, eased);
    mapView.centerX = start.centerX + (target.centerX - start.centerX) * eased;
    mapView.centerY = start.centerY + (target.centerY - start.centerY) * eased;
    updateMapView();
    if (progress < 1) {
      mapZoomAnimation = requestAnimationFrame(tick);
    } else {
      mapZoomAnimation = null;
    }
  };

  mapZoomAnimation = requestAnimationFrame(tick);
}

function selectOptions(values, selected, allLabel) {
  return [
    `<option value="all"${selected === "all" ? " selected" : ""}>${allLabel}</option>`,
    ...values.map(
      (value) =>
        `<option value="${value}"${selected === String(value) ? " selected" : ""}>${value}</option>`
    )
  ].join("");
}

function currentMapStyleUrl() {
  return MAP_STYLE_URL;
}

function mapAttributionMarkup() {
  return `
    <a href="https://stadiamaps.com/" target="_blank" rel="noreferrer">© Stadia Maps</a>
    <a href="https://openmaptiles.org/" target="_blank" rel="noreferrer">© OpenMapTiles</a>
    <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">© OpenStreetMap</a>
  `;
}

function visibleSpotLimitForZoom(zoom, totalSpots) {
  if (totalSpots <= 0) return 0;
  if (zoom < 5.4) return Math.min(totalSpots, 180);
  if (zoom < 6.2) return Math.min(totalSpots, 360);
  if (zoom < 7) return Math.min(totalSpots, 720);
  if (zoom < 7.8) return Math.min(totalSpots, 1200);
  if (zoom < 8.6) return Math.min(totalSpots, 1600);
  return totalSpots;
}

function visibleFeatureCollectionForZoom(allFeatures, zoom) {
  const limit = visibleSpotLimitForZoom(zoom, allFeatures.length);
  return {
    limit,
    data: {
      type: "FeatureCollection",
      features: allFeatures.slice(0, limit)
    }
  };
}

function applyVisibleSpotFilter(allFeatures) {
  if (!activeMap || !activeMap.isStyleLoaded()) return;
  const source = activeMap.getSource("catch-spots");
  if (!source) return;
  const { limit, data } = visibleFeatureCollectionForZoom(allFeatures, activeMap.getZoom());
  source.setData(data);
  updateHeaderResultCount(limit);
}

function renderMapLibreMap(spotResults, colorRanks) {
  const container = document.querySelector("#catch-map");
  if (!container) return;
  if (!window.maplibregl) {
    container.innerHTML = `
      <div class="empty-state map-load-error">
        <strong>地図ライブラリを読み込めませんでした</strong>
        <span>インターネット接続を確認してください。</span>
      </div>
    `;
    return;
  }

  const rankedSpots = [...spotResults].sort((a, b) => {
    if (b.count !== a.count) return b.count - a.count;
    if (b.totalCount !== a.totalCount) return b.totalCount - a.totalCount;
    return String(a.id).localeCompare(String(b.id), "ja");
  });

  const rankMap = new Map(rankedSpots.map((spot, index) => [String(spot.id), index + 1]));
  const features = rankedSpots
    .filter((spot) => Number.isFinite(Number(spot.lat)) && Number.isFinite(Number(spot.lng)))
    .map((spot) => ({
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [Number(spot.lng), Number(spot.lat)]
      },
      properties: {
        id: String(spot.id),
        name: spot.spot_name,
        count: spot.count,
        totalCount: spot.totalCount,
        color: markerColor(spot.count, colorRanks),
        visibilityRank: rankMap.get(String(spot.id)) || Number.MAX_SAFE_INTEGER
      }
    }));
  const initialFeatures = visibleFeatureCollectionForZoom(
    features,
    mapCameraState?.zoom ?? 4.7
  );

  const addCatchLayers = () => {
    if (!activeMap || !activeMap.isStyleLoaded()) return;

    if (activeMap.getLayer("catch-spot-labels")) activeMap.removeLayer("catch-spot-labels");
    if (activeMap.getLayer("catch-spots")) activeMap.removeLayer("catch-spots");
    if (activeMap.getLayer("catch-spots-halo")) activeMap.removeLayer("catch-spots-halo");
    if (activeMap.getSource("catch-spots")) activeMap.removeSource("catch-spots");

    activeMap.addSource("catch-spots", {
      type: "geojson",
      data: initialFeatures.data
    });

    activeMap.addLayer({
      id: "catch-spots-halo",
      type: "circle",
      source: "catch-spots",
      paint: {
        "circle-radius": [
          "interpolate",
          ["linear"],
          ["zoom"],
          4,
          5,
          8,
          7,
          12,
          10,
          16,
          14
        ],
        "circle-color": "#ffffff",
        "circle-opacity": 0.9
      }
    });

    activeMap.addLayer({
      id: "catch-spots",
      type: "circle",
      source: "catch-spots",
      paint: {
        "circle-radius": [
          "interpolate",
          ["linear"],
          ["zoom"],
          4,
          3.8,
          8,
          5.5,
          12,
          8,
          16,
          11
        ],
        "circle-color": ["get", "color"],
        "circle-stroke-color": "rgba(23, 59, 60, 0.38)",
        "circle-stroke-width": 1.2,
        "circle-opacity": 0.94
      }
    });

    activeMap.addLayer({
      id: "catch-spot-labels",
      type: "symbol",
      source: "catch-spots",
      minzoom: 9,
      layout: {
        "text-field": ["get", "name"],
        "text-size": 12,
        "text-offset": [0, 1.15],
        "text-anchor": "top",
        "text-allow-overlap": false
      },
      paint: {
        "text-color": "#173b3c",
        "text-halo-color": "#fffef9",
        "text-halo-width": 1.6
      }
    });

    applyVisibleSpotFilter(features);
  };

  activeMap = new maplibregl.Map({
    container,
    style: currentMapStyleUrl(),
    center: mapCameraState?.center || [137.8, 37.2],
    zoom: mapCameraState?.zoom ?? 4.7,
    bearing: mapCameraState?.bearing ?? 0,
    pitch: mapCameraState?.pitch ?? 0,
    minZoom: 4,
    maxZoom: 16,
    attributionControl: false
  });

  activeMap.on("load", () => {
    addCatchLayers();

    if (!mapCameraState && features.length > 0) {
      const bounds = new maplibregl.LngLatBounds();
      features.forEach((feature) => bounds.extend(feature.geometry.coordinates));
      activeMap.fitBounds(bounds, {
        padding: { top: 70, right: 40, bottom: 70, left: 40 },
        maxZoom: 8,
        duration: 0
      });
    }
  });

  activeMap.on("style.load", addCatchLayers);
  activeMap.on("moveend", () => {
    mapCameraState = {
      center: activeMap.getCenter().toArray(),
      zoom: activeMap.getZoom(),
      bearing: activeMap.getBearing(),
      pitch: activeMap.getPitch()
    };
    applyVisibleSpotFilter(features);
  });

  activeMap.on("click", "catch-spots", (event) => {
    const feature = event.features?.[0];
    if (!feature?.properties?.id) return;
    selectedSpotId = String(feature.properties.id);
    renderSpotBottomSheet();
  });

  activeMap.on("mouseenter", "catch-spots", () => {
    activeMap.getCanvas().style.cursor = "pointer";
  });

  activeMap.on("mouseleave", "catch-spots", () => {
    activeMap.getCanvas().style.cursor = "";
  });
}

function renderMapScreen() {
  setPageScrollLocked(true);
  const records = filteredRecords();
  const spotResults = aggregateSpots(records);
  const spotCounts = spotResults.map((spot) => spot.count);
  const affiliateMonth = currentActiveMonth();
  const nationwideAffiliateCards = nationwideMonthAffiliateCards(10);
  const colorRanks = new Map(
    [...new Set(spotCounts)].sort((a, b) => a - b).map((count, index) => [count, index])
  );
  removeActiveMap();
  selectedSpotId = null;

  app.innerHTML = `
    <section class="map-page">
      <section class="map-shell" aria-label="日本地図と釣りポイント">
        <div class="map-canvas">
          <div id="catch-map" class="catch-map" aria-label="釣果ポイント地図"></div>
          <div class="map-attribution" aria-label="地図データの帰属表示">
            ${mapAttributionMarkup()}
          </div>
          <div class="color-legend map-legend-overlay" aria-label="マーカーの色は釣果件数を表します">
            <span class="legend-title">釣果件数</span>
            <div class="legend-scale" aria-hidden="true">
              <span>少ない</span>
              <i class="legend-swatch level-1"></i>
              <i class="legend-swatch level-2"></i>
              <i class="legend-swatch level-3"></i>
              <i class="legend-swatch level-4"></i>
              <i class="legend-swatch level-5"></i>
              <span>多い</span>
            </div>
          </div>
          ${
            spotResults.length === 0
              ? `<div class="empty-state"><strong>該当するデータがありません</strong><span>条件を変更して確認してください。</span></div>`
              : ""
          }
          <section class="map-affiliate-panel" aria-label="全国のおすすめタックル">
            ${renderAffiliateCards(nationwideAffiliateCards, { variant: "inline" })}
          </section>
          <div id="spot-bottom-sheet" class="spot-bottom-sheet" hidden></div>
        </div>
      </section>
    </section>
  `;

  updateHeaderResultCount(visibleSpotLimitForZoom(mapCameraState?.zoom ?? 4.7, spotResults.length));
  renderMapLibreMap(spotResults, colorRanks);
  renderSpotBottomSheet();
}

function renderHeaderControls() {
  headerControls.innerHTML = `
    <div class="filter-fields">
      <label>
        <span>魚種</span>
        <input
          id="fish-filter"
          type="search"
          list="fish-filter-options"
          value="${escapeHtml(state.fishQuery)}"
          placeholder="魚種を検索"
          autocomplete="off"
          spellcheck="false"
        />
        <datalist id="fish-filter-options">
          ${fishNames.map((name) => `<option value="${escapeHtml(name)}"></option>`).join("")}
        </datalist>
      </label>
      <label>
        <span>月</span>
        <select id="month-filter">
          ${selectOptions(
            Array.from({ length: 12 }, (_, index) => `${index + 1}月`),
            state.month === "all" ? "all" : `${state.month}月`,
            "すべての月"
          )}
        </select>
      </label>
      <button class="reset-button" id="reset-filters" type="button">条件をクリア</button>
    </div>
    <p class="filter-result-count" id="filter-result-count"></p>
  `;
  document.querySelector("#fish-filter").addEventListener("input", (event) => {
    state.fishQuery = event.target.value.trim();
    renderMapScreen();
  });
  document.querySelector("#month-filter").addEventListener("change", (event) => {
    state.month = event.target.value === "all" ? "all" : event.target.value.replace("月", "");
    renderMapScreen();
  });
  document.querySelector("#reset-filters").addEventListener("click", () => {
    state.fishQuery = "";
    state.month = "all";
    renderHeaderControls();
    renderMapScreen();
  });
}

function updateHeaderResultCount(count) {
  const result = document.querySelector("#filter-result-count");
  if (result) result.textContent = `表示ポイント: ${formatCount(count)}件`;
}

function aggregateFish(records) {
  const totals = new Map();
  records.forEach((record) => {
    totals.set(record.fish_name, (totals.get(record.fish_name) || 0) + record.count);
  });
  return [...totals.entries()]
    .map(([fish, count]) => ({ fish, count }))
    .sort((a, b) => b.count - a.count);
}

function countFor(records, fish, month) {
  return sumCounts(
    records.filter((record) => record.fish_name === fish && record.month === month)
  );
}

function monthlyTopFishForSpot(spotId, month) {
  return (DETAIL_MONTHLY_TOP3[spotId] && DETAIL_MONTHLY_TOP3[spotId][String(month)]) || [];
}

function seasonalPeriodForDate(date) {
  const day = date.getDate();
  if (day <= 10) return "early";
  if (day <= 20) return "middle";
  return "late";
}

function nextSixHourStatisticsUrl(date = new Date()) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const period = seasonalPeriodForDate(date);
  const hour = String(date.getHours()).padStart(2, "0");
  return `${STATIC_NEXT_6H_BASE_URL}${month}-${period}-${hour}.json?v=${APP_ASSET_VERSION}`;
}

function spotDetailStatisticsUrl(spotId) {
  return `${STATIC_DETAIL_SPOTS_BASE_URL}${encodeURIComponent(spotId)}.json?v=${APP_ASSET_VERSION}`;
}

function nextSixHourTopFishForSpot(spotId, date = new Date()) {
  const items = DETAIL_NEXT_6H_TOP3[spotId] || [];
  return items
    .map((item) => {
      if (Array.isArray(item)) {
        return { fish_name: item[0], count: Number(item[1]) || 0 };
      }
      return item;
    })
    .filter((item) => item && typeof item.fish_name === "string" && item.fish_name.length > 0);
}

async function loadNext6hStatistics() {
  if (next6hStatisticsPromise) return next6hStatisticsPromise;
  next6hStatisticsPromise = (async () => {
    const response = await fetch(nextSixHourStatisticsUrl(), { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`6時間集計データがHTTP ${response.status}を返しました。`);
    }
    DETAIL_NEXT_6H_TOP3 = await response.json();
    NEXT_6H_STATISTICS_LOADED = true;
    return DETAIL_NEXT_6H_TOP3;
  })();
  try {
    return await next6hStatisticsPromise;
  } catch (error) {
    next6hStatisticsPromise = null;
    NEXT_6H_STATISTICS_LOADED = false;
    throw error;
  }
}

function renderNextSixHourTop3(items, options = {}) {
  const { compact = false, showRank = false } = options;
  if (!items.length) {
    return `<p class="${compact ? "next-6h-empty next-6h-empty-compact" : "next-6h-empty"}">この時間帯の実績はまだありません</p>`;
  }
  const classNames = ["next-6h-list"];
  if (compact) classNames.push("next-6h-list-compact");
  if (!showRank) classNames.push("next-6h-list-unranked");
  return `
    <ul class="${classNames.join(" ")}">
      ${items
        .map(
          (item, index) => `
            <li>
              ${showRank ? `<span>${index + 1}</span>` : ""}
              <strong>${escapeHtml(item.fish_name)}</strong>
            </li>
          `
        )
        .join("")}
    </ul>
  `;
}

function renderPopupMiniTimeGraph(timeCounts, label) {
  const width = 169;
  const height = 55;
  const paddingX = 32;
  const paddingY = 5;
  const chartRight = 5;
  const chartWidth = width - paddingX - chartRight;
  const chartHeight = 36;
  const counts = Array.isArray(timeCounts) ? timeCounts.slice(0, 72) : [];
  while (counts.length < 72) counts.push(0);
  const smoothedCounts = counts.map((_, index, array) => {
    const start = Math.max(0, index - 3);
    const end = Math.min(array.length, index + 4);
    const windowValues = array.slice(start, end);
    const sum = windowValues.reduce((acc, value) => acc + value, 0);
    return sum / windowValues.length;
  });
  const maxCount = Math.max(...smoothedCounts, 1);
  const points = smoothedCounts.map((count, index) => {
    const x = paddingX + (chartWidth * index) / 71;
    const y = paddingY + chartHeight - (count / maxCount) * chartHeight;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const currentBucket = (new Date().getHours() * 60 + new Date().getMinutes()) / 20;
  const nowX = paddingX + (chartWidth * Math.min(71, currentBucket)) / 71;
  return `
    <svg class="popup-mini-graph" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(
      label
    )} の簡易時間帯グラフ">
      <text x="16" y="${paddingY + chartHeight / 2}" text-anchor="middle" dominant-baseline="middle" class="popup-mini-graph-y-label">釣果</text>
      <line x1="${paddingX}" y1="${paddingY}" x2="${paddingX}" y2="${
        paddingY + chartHeight
      }" class="popup-mini-graph-y-axis"></line>
      <line x1="${paddingX}" y1="${paddingY + chartHeight}" x2="${width - chartRight}" y2="${
        paddingY + chartHeight
      }" class="popup-mini-graph-base"></line>
      <polyline points="${points.join(" ")}" class="popup-mini-graph-line"></polyline>
      <line x1="${nowX.toFixed(2)}" y1="${paddingY}" x2="${nowX.toFixed(2)}" y2="${
        paddingY + chartHeight
      }" class="popup-mini-graph-now"></line>
      <text x="${paddingX}" y="${height - 2}" text-anchor="start" class="popup-mini-graph-axis-label">0時</text>
      <text x="${width / 2}" y="${height - 2}" text-anchor="middle" class="popup-mini-graph-axis-label">12時</text>
      <text x="${width - chartRight}" y="${height - 2}" text-anchor="end" class="popup-mini-graph-axis-label">24時</text>
    </svg>
  `;
}

function renderPopupNextSixHourGraphs(spotId, items) {
  if (!items.length) {
    return '<p class="next-6h-empty next-6h-empty-compact">この時間帯の実績はまだありません</p>';
  }
  if (!DETAIL_SPOT_FISH_TIME_COUNTS[spotId]) {
    return '<p class="next-6h-empty next-6h-empty-compact">グラフを読み込んでいます</p>';
  }
  const timeCountsByFish = DETAIL_SPOT_FISH_TIME_COUNTS[spotId] || {};
  return `
    <ul class="popup-mini-graph-list">
      ${items
        .map((item) => {
          const timeCounts = timeCountsByFish[item.fish_name] || [];
          return `
            <li>
              <strong>${escapeHtml(item.fish_name)}</strong>
              ${renderPopupMiniTimeGraph(timeCounts, item.fish_name)}
            </li>
          `;
        })
        .join("")}
    </ul>
  `;
}

async function loadDetailStatistics() {
  if (detailStatisticsPromise) return detailStatisticsPromise;
  detailStatisticsPromise = (async () => {
    let response = await fetch(STATIC_DETAIL_STATISTICS_URL, { cache: "no-store" });
    if (!response.ok) {
      response = await fetch(API_DETAIL_STATISTICS_URL, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`詳細集計データがHTTP ${response.status}を返しました。`);
      }
    }
    const payload = await response.json();
    DETAIL_MONTHLY_TOP3 = payload.spot_month_top3 || {};
    DETAIL_SPOT_FISH_TIME_COUNTS = payload.spot_fish_time_counts || {};
    DETAIL_STATISTICS_METADATA = payload.metadata || {};
    DETAIL_STATISTICS_LOADED = true;
    return payload;
  })();
  try {
    return await detailStatisticsPromise;
  } catch (error) {
    detailStatisticsPromise = null;
    DETAIL_STATISTICS_LOADED = false;
    throw error;
  }
}

async function loadSpotDetailStatistics(spotId) {
  if (DETAIL_SPOT_FISH_TIME_COUNTS[spotId] && DETAIL_MONTHLY_TOP3[spotId]) {
    return {
      spot_month_top3: DETAIL_MONTHLY_TOP3[spotId],
      spot_fish_time_counts: DETAIL_SPOT_FISH_TIME_COUNTS[spotId],
      metadata: DETAIL_STATISTICS_METADATA
    };
  }
  if (spotDetailStatisticsPromises.has(spotId)) {
    return spotDetailStatisticsPromises.get(spotId);
  }
  const promise = (async () => {
    const response = await fetch(spotDetailStatisticsUrl(spotId), { cache: "no-store" });
    if (!response.ok) {
      await loadDetailStatistics();
      return {
        spot_month_top3: DETAIL_MONTHLY_TOP3[spotId] || {},
        spot_fish_time_counts: DETAIL_SPOT_FISH_TIME_COUNTS[spotId] || {},
        metadata: DETAIL_STATISTICS_METADATA
      };
    }
    const payload = await response.json();
    DETAIL_MONTHLY_TOP3[spotId] = payload.spot_month_top3 || {};
    DETAIL_SPOT_FISH_TIME_COUNTS[spotId] = payload.spot_fish_time_counts || {};
    DETAIL_STATISTICS_METADATA = payload.metadata || DETAIL_STATISTICS_METADATA;
    return payload;
  })();
  spotDetailStatisticsPromises.set(spotId, promise);
  try {
    return await promise;
  } catch (error) {
    spotDetailStatisticsPromises.delete(spotId);
    throw error;
  }
}

function affiliateLinksForFish(fish) {
  const visited = new Set();
  let currentFish = fish;
  while (currentFish && !visited.has(currentFish)) {
    visited.add(currentFish);
    const entry = FISH_AFFILIATE_URLS[currentFish];
    if (entry && entry.status === "ok" && Array.isArray(entry.affiliate_links)) {
      return entry.affiliate_links;
    }
    currentFish = FISH_AFFILIATE_FALLBACKS[currentFish] || "";
  }
  return [];
}

function expandAffiliateFallbackGroups(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
  const expanded = {};
  Object.entries(payload).forEach(([canonicalFish, linkedFish]) => {
    if (!Array.isArray(linkedFish)) return;
    linkedFish.forEach((fish) => {
      if (typeof fish !== "string" || fish.length === 0) return;
      expanded[fish] = canonicalFish;
    });
  });
  return expanded;
}

function shuffleCards(items) {
  const result = [...items];
  for (let index = result.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [result[index], result[swapIndex]] = [result[swapIndex], result[index]];
  }
  return result;
}

function pickAffiliateCards(fish, limit = 2) {
  const links = affiliateLinksForFish(fish);
  if (links.length === 0) return [];
  return shuffleCards(links)
    .slice(0, limit)
    .map((item) => {
      const fallbackMediumImageUrl = Array.isArray(item.medium_image_urls)
        ? item.medium_image_urls.find((value) => typeof value === "string" && value.length > 0) || ""
        : "";
      return {
        fish,
        name: item.item_name || fish,
        imageUrl: item.image_url || fallbackMediumImageUrl,
        url: item.affiliate_url
      };
    });
}

function renderNoImageMarkup(label = "NoImage", extraClass = "") {
  return `
    <div class="affiliate-noimage${extraClass ? ` ${extraClass}` : ""}">
      <span>${escapeHtml(label)}</span>
    </div>
  `;
}

function renderAffiliateCards(cards, options = {}) {
  const { variant = "map" } = options;
  if (!cards.length) {
    return '<p class="affiliate-empty">該当するリンクはまだありません。</p>';
  }
  const marqueeDurationSeconds = Math.max(27, cards.length * 6.6);
  const cardMarkup = cards
    .map(
      (card) => `
        <article class="affiliate-card affiliate-card-${variant}">
          <a class="affiliate-card-image-link" href="${escapeHtml(card.url)}" target="_blank" rel="nofollow sponsored noopener noreferrer" aria-label="${escapeHtml(card.name)}">
            ${
              card.imageUrl
                ? `<div class="affiliate-card-image-wrap"><img class="affiliate-card-image" src="${escapeHtml(card.imageUrl)}" alt="${escapeHtml(card.name)}" loading="lazy" decoding="async" referrerpolicy="no-referrer"></div>`
                : `<div class="affiliate-card-image-wrap affiliate-card-image-fallback">${renderNoImageMarkup()}</div>`
            }
          </a>
          <a class="affiliate-card-title" href="${escapeHtml(card.url)}" target="_blank" rel="nofollow sponsored noopener noreferrer">${escapeHtml(card.name)}</a>
        </article>
      `
    )
    .join("");
  return `
    <div class="affiliate-marquee affiliate-marquee-${variant}">
      <div class="affiliate-grid affiliate-grid-${variant}" style="--affiliate-marquee-duration:${marqueeDurationSeconds}s">
        ${cardMarkup}
        ${cards.length > 1 ? cardMarkup : ""}
      </div>
    </div>
  `;
}

function renderFishAffiliateGroups(items) {
  const groups = items
    .map((item) => ({
      fish: item.fish_name,
      cards: pickAffiliateCards(item.fish_name, 2)
    }))
    .filter((group) => group.cards.length > 0);
  if (!groups.length) {
    return '<p class="current-target-empty">おすすめアイテムはまだありません</p>';
  }
  return `
    <div class="fish-affiliate-groups">
      ${groups
        .map(
          (group) => `
            <section class="fish-affiliate-group">
              <h3>${escapeHtml(group.fish)}</h3>
              <div class="fish-affiliate-cards">
                ${group.cards
                  .map(
                    (card) => `
                      <article class="affiliate-card affiliate-card-detail">
                        <a class="affiliate-card-image-link" href="${escapeHtml(card.url)}" target="_blank" rel="nofollow sponsored noopener noreferrer" aria-label="${escapeHtml(card.name)}">
                          ${
                            card.imageUrl
                              ? `<div class="affiliate-card-image-wrap"><img class="affiliate-card-image" src="${escapeHtml(card.imageUrl)}" alt="${escapeHtml(card.name)}" loading="lazy" decoding="async" referrerpolicy="no-referrer"></div>`
                              : `<div class="affiliate-card-image-wrap affiliate-card-image-fallback">${renderNoImageMarkup()}</div>`
                          }
                        </a>
                        <a class="affiliate-card-title" href="${escapeHtml(card.url)}" target="_blank" rel="nofollow sponsored noopener noreferrer">${escapeHtml(card.name)}</a>
                      </article>
                    `
                  )
                  .join("")}
              </div>
            </section>
          `
        )
        .join("")}
    </div>
  `;
}

function renderSingleAffiliateCard(card, fish) {
  if (!card) {
    return `
      <div class="histogram-affiliate-card histogram-affiliate-card-empty">
        <div class="histogram-affiliate-image-wrap">
          ${renderNoImageMarkup("NoImage", "affiliate-noimage-histogram")}
        </div>
        <p class="histogram-affiliate-name">${escapeHtml(fish)}</p>
      </div>
    `;
  }

  return `
    <a class="histogram-affiliate-card" href="${escapeHtml(
      card.url
    )}" target="_blank" rel="nofollow sponsored noopener noreferrer" aria-label="${escapeHtml(
      card.name
    )}">
      <div class="histogram-affiliate-image-wrap">
        ${
          card.imageUrl
            ? `<img class="histogram-affiliate-image" src="${escapeHtml(
                card.imageUrl
              )}" alt="${escapeHtml(card.name)}" loading="lazy" decoding="async" referrerpolicy="no-referrer">`
            : renderNoImageMarkup("NoImage", "affiliate-noimage-histogram")
        }
      </div>
      <p class="histogram-affiliate-name">${escapeHtml(card.name)}</p>
    </a>
  `;
}

function renderHistogramAffiliateCards(cards, fish) {
  if (!cards.length) {
    return renderSingleAffiliateCard(null, fish);
  }
  return `
    <div class="histogram-affiliate-grid">
      ${cards.map((card) => renderSingleAffiliateCard(card, fish)).join("")}
    </div>
  `;
}

function isMobileViewport() {
  return typeof window !== "undefined" && window.matchMedia("(max-width: 560px)").matches;
}

function renderHourlyHistogramSvg(timeCounts, label) {
  const isMobileHistogram = isMobileViewport();
  const width = isMobileHistogram ? 500 : 540;
  const height = isMobileHistogram ? 230 : 188;
  const chartLeft = isMobileHistogram ? 42 : 32;
  const chartRight = isMobileHistogram ? 12 : 18;
  const chartTop = isMobileHistogram ? 34 : 34;
  const chartBottom = isMobileHistogram ? 42 : 35;
  const chartWidth = width - chartLeft - chartRight;
  const chartHeight = height - chartTop - chartBottom;
  const pointsPerDay = Array.isArray(timeCounts) && timeCounts.length === 72 ? 72 : 24;
  const normalizedCounts =
    pointsPerDay === 72
      ? timeCounts.slice(0, 72)
      : timeCounts.slice(0, 24).flatMap((count) => Array.from({ length: 3 }, () => count));
  while (normalizedCounts.length < pointsPerDay) normalizedCounts.push(0);
  const movingAverageWindow = 6;
  const smoothedCounts = normalizedCounts.map((_, index, array) => {
    const leftWindow = Math.floor((movingAverageWindow - 1) / 2);
    const rightWindow = Math.ceil((movingAverageWindow - 1) / 2);
    const start = Math.max(0, index - leftWindow);
    const end = Math.min(array.length, index + rightWindow + 1);
    const windowValues = array.slice(start, end);
    const sum = windowValues.reduce((acc, value) => acc + value, 0);
    return sum / windowValues.length;
  });
  const maxCount = Math.max(...smoothedCounts, 1);
  const yTicks = [0.25, 0.5, 0.75, 1];
  const xLabels = [
    { bucket: 0, text: "0時" },
    { bucket: 9, text: "3時" },
    { bucket: 18, text: "6時" },
    { bucket: 27, text: "9時" },
    { bucket: 36, text: "12時" },
    { bucket: 45, text: "15時" },
    { bucket: 54, text: "18時" },
    { bucket: 63, text: "21時" },
    { bucket: 71, text: "23時" }
  ];
  const gradientId = `histogram-gradient-${label.replace(/[^\w-]/g, "").toLowerCase() || "fish"}`;
  const glowId = `histogram-glow-${label.replace(/[^\w-]/g, "").toLowerCase() || "fish"}`;

  const verticalHourLines = Array.from({ length: 24 }, (_, hour) => {
    const bucket = hour * 3;
    const x = chartLeft + (chartWidth * bucket) / (pointsPerDay - 1);
    const lineClass =
      hour % 3 === 0
        ? "histogram-grid-line histogram-grid-line-vertical histogram-grid-line-major"
        : "histogram-grid-line histogram-grid-line-vertical";
    return `<line x1="${x.toFixed(2)}" y1="${chartTop}" x2="${x.toFixed(2)}" y2="${(
      chartTop + chartHeight
    ).toFixed(2)}" class="${lineClass}"></line>`;
  }).join("");

  const points = smoothedCounts.map((count, bucket) => {
    const x = chartLeft + (chartWidth * bucket) / (pointsPerDay - 1);
    const y = chartTop + chartHeight - (count / maxCount) * chartHeight;
    return { x, y, bucket, count };
  });

  const linePath =
    points.length < 3
      ? points
          .map((point, index) =>
            `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`
          )
          .join(" ")
      : [
          (() => {
            const smoothness = 0.62;
            return [
          `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`,
          ...points.slice(1, -1).map((point, index) => {
            const nextPoint = points[index + 2];
            const smoothX = (point.x + (nextPoint.x - point.x) * smoothness).toFixed(2);
            const smoothY = (point.y + (nextPoint.y - point.y) * smoothness).toFixed(2);
            return `Q ${point.x.toFixed(2)} ${point.y.toFixed(2)} ${smoothX} ${smoothY}`;
          }),
          `Q ${points[points.length - 2].x.toFixed(2)} ${points[points.length - 2].y.toFixed(
            2
          )} ${points[points.length - 1].x.toFixed(2)} ${points[points.length - 1].y.toFixed(2)}`
            ].join(" ");
          })()
        ]
          .join("");
  const areaPath = `${linePath} L ${points[points.length - 1].x.toFixed(2)} ${(
    chartTop + chartHeight
  ).toFixed(2)} L ${points[0].x.toFixed(2)} ${(chartTop + chartHeight).toFixed(2)} Z`;

  const labels = xLabels
    .map(({ bucket, text }) => {
      const x = chartLeft + (chartWidth * bucket) / (pointsPerDay - 1);
      return `<text x="${x.toFixed(2)}" y="${height - (isMobileHistogram ? 10 : 8)}" text-anchor="middle" class="histogram-axis-label">${text}</text>`;
    })
    .join("");
  const now = new Date();
  const currentBucket = (now.getHours() * 60 + now.getMinutes()) / 20;
  const nowX = chartLeft + (chartWidth * Math.min(pointsPerDay - 1, currentBucket)) / (pointsPerDay - 1);
  const nowLabelY = chartTop - 10;
  const nowMarker = `
    <line x1="${nowX.toFixed(2)}" y1="${chartTop}" x2="${nowX.toFixed(2)}" y2="${(
      chartTop + chartHeight
    ).toFixed(2)}" class="histogram-now-line"></line>
    <text x="${nowX.toFixed(2)}" y="${nowLabelY}" text-anchor="middle" class="histogram-now-label">現在</text>
  `;

  return `
    <svg class="time-histogram-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(
      label
    )} の時間帯別釣果線グラフ">
      <defs>
        <linearGradient id="${gradientId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="rgba(22, 135, 131, 0.34)"></stop>
          <stop offset="55%" stop-color="rgba(22, 135, 131, 0.12)"></stop>
          <stop offset="100%" stop-color="rgba(22, 135, 131, 0.02)"></stop>
        </linearGradient>
        <filter id="${glowId}" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="4" result="blur"></feGaussianBlur>
          <feMerge>
            <feMergeNode in="blur"></feMergeNode>
            <feMergeNode in="SourceGraphic"></feMergeNode>
          </feMerge>
        </filter>
      </defs>
      <rect x="0.5" y="0.5" width="${width - 1}" height="${height - 1}" rx="16" class="histogram-panel"></rect>
      ${verticalHourLines}
      <text x="${chartLeft / 2}" y="${chartTop + chartHeight / 2}" text-anchor="middle" dominant-baseline="middle" class="histogram-y-axis-label">釣果</text>
      <line x1="${chartLeft}" y1="${chartTop}" x2="${chartLeft}" y2="${chartTop + chartHeight}" class="histogram-axis-line"></line>
      <line x1="${chartLeft}" y1="${chartTop + chartHeight}" x2="${width - chartRight}" y2="${
        chartTop + chartHeight
      }" class="histogram-axis-line"></line>
      <path d="${areaPath}" class="histogram-area" fill="url(#${gradientId})"></path>
      <path d="${linePath}" class="histogram-line-shadow" filter="url(#${glowId})"></path>
      <path d="${linePath}" class="histogram-line"></path>
      ${nowMarker}
      ${labels}
    </svg>
  `;
}

function currentActiveMonth() {
  return state.month === "all" ? new Date().getMonth() + 1 : Number(state.month);
}

function nationwideMonthAffiliateCards(limit = 10) {
  const month = currentActiveMonth();
  const ranking = aggregateFish(CATCH_RECORDS.filter((record) => record.month === month));
  return ranking
    .slice(0, limit)
    .flatMap((item) => pickAffiliateCards(item.fish, 2))
    .slice(0, limit * 2);
}

function heatmapIntensity(count, maxCount) {
  if (count === 0 || maxCount <= 0) return 0;
  const normalized = count / maxCount;
  const level = Math.max(1, Math.min(7, Math.ceil(Math.pow(normalized, 0.52) * 7)));
  return [0, 0.08, 0.18, 0.32, 0.5, 0.68, 0.84, 1][level];
}

function renderSpotPopupContent(spotId) {
  const spot = SPOTS.find((item) => item.id === spotId);
  const records = recordsForSpot(spotId);
  if (!spot || records.length === 0) return "";

  const total = sumCounts(records);
  const nextSixHourItems = nextSixHourTopFishForSpot(spotId);
  const nextSixHourContent = NEXT_6H_STATISTICS_LOADED
    ? renderPopupNextSixHourGraphs(spotId, nextSixHourItems)
    : '<p class="next-6h-empty next-6h-empty-compact">時間帯データを読み込んでいます</p>';

  return `
    <section class="spot-sheet-card">
      <header class="spot-popup-header">
        <div>
          <p>${escapeHtml(spot.prefecture)}</p>
          <h3>${escapeHtml(spot.spot_name)}</h3>
        </div>
        <strong>${formatCount(total)}件</strong>
      </header>
      <div class="spot-popup-next-6h">
        <div class="spot-popup-next-6h-heading">
          <h4>直近6時間で狙える魚</h4>
          <p class="popup-mini-graph-legend"><span></span>現在時刻</p>
        </div>
        ${nextSixHourContent}
      </div>
      <a class="spot-popup-link" href="#/spot/${encodeURIComponent(spotId)}">詳細を見る</a>
    </section>
  `;
}

function renderSpotBottomSheet() {
  const container = document.querySelector("#spot-bottom-sheet");
  if (!container) return;
  if (!selectedSpotId) {
    container.hidden = true;
    container.innerHTML = "";
    return;
  }
  const content = renderSpotPopupContent(selectedSpotId);
  if (!content) {
    container.hidden = true;
    container.innerHTML = "";
    return;
  }
  container.hidden = false;
  container.innerHTML = `
    <div class="spot-sheet-backdrop" data-close-spot-sheet="true"></div>
    <section class="spot-sheet" aria-label="ポイント概要">
      <button class="spot-sheet-close" type="button" aria-label="閉じる" data-close-spot-sheet="true">×</button>
      <div class="spot-sheet-handle" aria-hidden="true"></div>
      ${content}
    </section>
  `;
  container.querySelectorAll("[data-close-spot-sheet='true']").forEach((element) => {
    element.addEventListener("click", () => {
      selectedSpotId = null;
      renderSpotBottomSheet();
    });
  });
  if (!DETAIL_SPOT_FISH_TIME_COUNTS[selectedSpotId]) {
    const requestedSpotId = selectedSpotId;
    loadSpotDetailStatistics(requestedSpotId)
      .then(() => {
        if (selectedSpotId === requestedSpotId) renderSpotBottomSheet();
      })
      .catch(() => {
        DETAIL_SPOT_FISH_TIME_COUNTS[requestedSpotId] = {};
      });
  }
}

function renderDetailScreen(spotId) {
  selectedSpotId = spotId;
  setPageScrollLocked(false);
  removeActiveMap();
  setHeaderControlsVisible(false);
  const spot = SPOTS.find((item) => item.id === spotId);
  if (!spot) {
    renderNotFound();
    return;
  }

  app.innerHTML = `
    <article class="detail-page">
      <nav class="breadcrumb" aria-label="パンくず">
        <a href="#/">全国の釣果分布</a>
        <span aria-hidden="true">/</span>
        <span>${spot.spot_name}</span>
      </nav>

      <header class="detail-hero">
        <div>
          <p class="eyebrow">${spot.prefecture} / POINT DATA</p>
          <h1>${spot.spot_name}</h1>
        </div>
      </header>

      <section class="not-found detail-loading">
        <p class="eyebrow">LOADING DETAIL</p>
        <h1>詳細統計を読み込んでいます</h1>
      </section>
    </article>
  `;

  loadSpotDetailStatistics(spotId)
    .catch(() => {
      DETAIL_MONTHLY_TOP3[spotId] = {};
      DETAIL_SPOT_FISH_TIME_COUNTS[spotId] = {};
    })
    .finally(() => {
      if (selectedSpotId !== spotId) return;

      const records = recordsForSpot(spotId);
      const total = sumCounts(records);
      const allFishRanking = aggregateFish(records);
      const fishRanking = allFishRanking.slice(0, 10);
      const currentMonth = new Date().getMonth() + 1;
      const heatmapMax = Math.max(...records.map((record) => record.count), 1);
      const currentMonthRanking = monthlyTopFishForSpot(spotId, currentMonth);
      const nextSixHourItems = nextSixHourTopFishForSpot(spotId);
      const detailPeriodText = detailStatisticsPeriodText();

      const heatmapRows = fishRanking
        .map((item) => {
          const monthCells = Array.from({ length: 12 }, (_, index) => {
            const month = index + 1;
            const count = countFor(records, item.fish, month);
            const intensity = heatmapIntensity(count, heatmapMax);
            const toneClass = intensity >= 0.62 ? " heat-cell-strong" : "";
            return `<td class="heat-cell${toneClass}" style="--intensity:${intensity.toFixed(
              2
            )}" aria-label="${item.fish} ${month}月 ${count}件"><span class="heat-cell-fill" aria-hidden="true"></span><span class="heat-cell-value">${count}</span></td>`;
          }).join("");
          return `<tr><th scope="row"><span class="heatmap-fish-name">${item.fish}</span><span class="heatmap-fish-total">${formatCount(item.count)}件</span></th>${monthCells}</tr>`;
        })
        .join("");

      const timeCountsByFish = DETAIL_SPOT_FISH_TIME_COUNTS[spotId] || {};
      const nextSixHourGraphItems = nextSixHourItems.length
        ? nextSixHourItems
            .map((item) => {
              const timeSource = timeCountsByFish[item.fish_name];
              const timeCounts = Array.isArray(timeSource)
                ? timeSource.slice(0, 72)
                : Array.from({ length: 72 }, () => 0);
              while (timeCounts.length < 72) timeCounts.push(0);
              return `
                <li class="time-histogram-card time-histogram-card-compact">
                  <div class="time-histogram-main">
                    <div class="time-histogram-header">
                      <div class="time-histogram-title">
                        ${
                          ENABLE_FISH_ILLUSTRATIONS
                            ? `<img class="time-histogram-illustration" src="${currentTargetIllustrationUrl(
                                item.fish_name
                              )}" alt="${item.fish_name}">`
                            : ""
                        }
                        <div>
                          <strong>${escapeHtml(item.fish_name)}</strong>
                        </div>
                      </div>
                    </div>
                    ${renderHourlyHistogramSvg(timeCounts, item.fish_name)}
                  </div>
                </li>
              `;
            })
            .join("")
        : '<li class="current-target-empty">この時間帯の実績はまだありません</li>';

      const currentMonthAffiliateCards = currentMonthRanking
        .flatMap((item) => pickAffiliateCards(item.fish_name, 2))
        .slice(0, 6);
      const currentMonthItems = renderFishAffiliateGroups(currentMonthRanking);

      app.innerHTML = `
        <article class="detail-page">
          <nav class="breadcrumb" aria-label="パンくず">
            <a href="#/">全国の釣果分布</a>
            <span aria-hidden="true">/</span>
            <span>${spot.spot_name}</span>
          </nav>

          <header class="detail-hero">
            <div>
              <p class="eyebrow">${spot.prefecture} / POINT DATA</p>
              <h1>${spot.spot_name}</h1>
            </div>
            <div class="total-card">
              <span>集計対象件数</span>
              <strong>${formatCount(total)}<small>件</small></strong>
            </div>
          </header>

          <section class="data-section current-target-section">
            <div class="section-heading">
              <div>
                <p class="section-number">01</p>
                <h2>直近6時間で狙える魚</h2>
              </div>
              <p>${escapeHtml(detailPeriodText || "全期間の釣果データを集計")}</p>
            </div>
            <ol class="time-histogram-list">${nextSixHourGraphItems}</ol>
          </section>

          <section class="data-section current-target-section">
            <div class="section-heading">
              <div>
                <p class="section-number">02</p>
                <h2>今月狙える魚におすすめ</h2>
              </div>
              <p>${currentMonth}月の投稿件数ベース</p>
            </div>
            <div class="current-month-affiliate-panel">${currentMonthItems}</div>
          </section>

          <section class="data-section heatmap-section">
            <div class="section-heading">
              <div>
                <p class="section-number">03</p>
                <h2>月別釣果実績</h2>
              </div>
              <div class="heat-legend"><span>件数</span><i></i><i></i><i></i><i></i><i></i><i></i><i></i><span>多</span></div>
            </div>
            <div class="table-wrap heatmap-wrap">
              <table class="heatmap-table">
                <thead>
                  <tr>
                    <th>魚種</th>
                    ${Array.from({ length: 12 }, (_, index) => `<th class="heatmap-month">${index + 1}月</th>`).join("")}
                  </tr>
                </thead>
                <tbody>${heatmapRows}</tbody>
              </table>
            </div>
            <p class="table-note">${escapeHtml(detailPeriodText || "全期間の釣果データを集計")}。各月の件数を色と数値で示しています。</p>
          </section>

          <div class="affiliate-section">
            ${renderAffiliateCards(currentMonthAffiliateCards, { variant: "detail-fixed" })}
          </div>

          <a class="back-link" href="#/"><span aria-hidden="true">←</span> 地図に戻る</a>
        </article>
      `;
    });
}

function renderNotFound() {
  selectedSpotId = null;
  setPageScrollLocked(false);
  removeActiveMap();
  setHeaderControlsVisible(false);
  app.innerHTML = `
    <section class="not-found">
      <p class="eyebrow">404 / NOT FOUND</p>
      <h1>ポイントが見つかりません</h1>
      <a class="back-link" href="#/">地図に戻る</a>
    </section>
  `;
}

function route() {
  const match = window.location.hash.match(/^#\/spot\/([^/]+)$/);
  if (match) {
    renderDetailScreen(match[1]);
  } else {
    selectedSpotId = null;
    setHeaderControlsVisible(true);
    renderHeaderControls();
    renderMapScreen();
  }
  window.scrollTo(0, 0);
  app.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);

async function loadOptionalAssets() {
  try {
    const illustrationResponse = await fetch(STATIC_ILLUSTRATIONS_URL, {
      cache: "no-store"
    });
    if (illustrationResponse.ok) {
      FISH_ILLUSTRATION_PATHS = await illustrationResponse.json();
    }
  } catch (_error) {
    FISH_ILLUSTRATION_PATHS = {};
  }

  try {
    const affiliateFallbackResponse = await fetch(STATIC_AFFILIATE_FALLBACKS_URL, {
      cache: "no-store"
    });
    if (affiliateFallbackResponse.ok) {
      FISH_AFFILIATE_FALLBACKS = expandAffiliateFallbackGroups(
        await affiliateFallbackResponse.json()
      );
    }
  } catch (_error) {
    FISH_AFFILIATE_FALLBACKS = {};
  }

  try {
    const affiliateResponse = await fetch(STATIC_AFFILIATE_URL, {
      cache: "no-store"
    });
    if (affiliateResponse.ok) {
      const affiliatePayload = await affiliateResponse.json();
      FISH_AFFILIATE_URLS = affiliatePayload.fish_affiliate_urls || {};
    }
  } catch (_error) {
    FISH_AFFILIATE_URLS = {};
  }
}

async function loadStatistics() {
  app.innerHTML = `
    <section class="not-found">
      <p class="eyebrow">LOADING DATA</p>
      <h1>集計データを読み込んでいます</h1>
    </section>
  `;

  try {
    let response = await fetch(STATIC_STATISTICS_URL, { cache: "no-store" });
    if (!response.ok) {
      response = await fetch(API_STATISTICS_URL, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`統計データがHTTP ${response.status}を返しました。`);
      }
    }
    const payload = await response.json();
    SPOTS = payload.spots || [];
    CATCH_RECORDS = payload.catches || [];
    SPOT_TOTAL_COUNTS = new Map();
    CATCH_RECORDS.forEach((record) => {
      SPOT_TOTAL_COUNTS.set(
        record.spot_id,
        (SPOT_TOTAL_COUNTS.get(record.spot_id) || 0) + record.count
      );
    });
    try {
      await loadNext6hStatistics();
    } catch (_error) {
      DETAIL_NEXT_6H_TOP3 = {};
      NEXT_6H_STATISTICS_LOADED = false;
    }
    fishNames = [...new Set(CATCH_RECORDS.map((record) => record.fish_name))].sort(
      (a, b) => a.localeCompare(b, "ja")
    );

    renderHeaderControls();
    route();
    loadOptionalAssets();
  } catch (error) {
    app.innerHTML = `
      <section class="not-found">
        <p class="eyebrow">DATA ERROR</p>
        <h1>集計データを読み込めませんでした</h1>
        <p>${error.message}</p>
        <p><code>python3 server.py</code> でサイトを起動してください。</p>
      </section>
    `;
  }
}

loadStatistics();
