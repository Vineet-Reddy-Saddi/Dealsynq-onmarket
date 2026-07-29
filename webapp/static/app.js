/* DealSynq — On-Market Retail viewer.
 *
 * Map strategy (the important part): the index holds ~283k listings, far more than a
 * browser can hold as markers. The server aggregates to a zoom-dependent grid and
 * returns a few hundred cells; past a zoom threshold it returns individual listings
 * instead. So the map always draws hundreds of things, never hundreds of thousands,
 * and the user sees the true national distribution rather than an arbitrary first-900.
 *
 * The sidebar is paged (40 at a time, appended on scroll) — rendering 900 cards at
 * once is what made the previous version janky.
 */
'use strict';

const $ = (s) => document.querySelector(s);
const money = (v) =>
  v == null ? null :
  v >= 1e9 ? '$' + (v / 1e9).toFixed(1) + 'B' :
  v >= 1e6 ? '$' + (v / 1e6).toFixed(v >= 1e7 ? 0 : 2) + 'M' :
  v >= 1e3 ? '$' + Math.round(v / 1e3) + 'K' : '$' + Math.round(v);
const num = (v) => (v == null ? null : Number(v).toLocaleString('en-US'));
/* Some feeds (CityFeet's JSON-LD especially) ship text that is already HTML-escaped,
 * so "6425 &amp; 6429" would render literally once we escape it again. Decode first,
 * then escape — decoding via textarea.value never executes markup, and every insertion
 * still goes through the escape below, so this stays XSS-safe. */
const _ta = document.createElement('textarea');
function decodeEntities(s) {
  if (!s.includes('&')) return s;
  _ta.innerHTML = s;
  return _ta.value;
}
const esc = (s) => decodeEntities(String(s ?? '')).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Some sources (Crexi's marketingDescription especially -- 35k+ rows) store their
 * description as broker-authored rich-text HTML, e.g. "<p>Prime Downtown...</p>". esc()
 * correctly escapes that for safe display, but escaped tags are still visible as
 * literal "<p>" text on the page -- technically safe, but unreadable. Strip markup here
 * via string replacement only (innerHTML is never touched, so this can't execute
 * anything even from an untrusted source); paragraph/line breaks become newlines first
 * so multi-paragraph text doesn't collapse into one run-on block. */
function stripHtml(s) {
  if (!s || !s.includes('<')) return s || '';
  const withBreaks = s
    .replace(/<\/(p|div|li|h[1-6])\s*>/gi, '\n')
    .replace(/<(br|hr)\s*\/?>/gi, '\n');
  return decodeEntities(withBreaks.replace(/<[^>]+>/g, '')).replace(/\n{3,}/g, '\n\n').trim();
}

const state = {
  txn: 'sale', sort: 'new', page: 0, total: 0,
  items: [], loading: false, hasMore: false,
  activePk: null, markers: new Map(), mapReq: 0, listReq: 0,
  // A drawn trade area (L.LatLngBounds) and its rectangle, when one is pinned.
  area: null, areaRect: null,
};

/* ---------------------------------------------------------------- map ---- */
const map = L.map('map', {
  zoomControl: true, preferCanvas: true,
  minZoom: 3, maxZoom: 19, worldCopyJump: true,
}).setView([39.5, -96], 4);

const BASE = {
  // Desaturated light basemap: the ACX palette is light, and a muted canvas keeps the
  // navy/green pins the most saturated thing on the map.
  street: L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
    maxZoom: 19, subdomains: 'abcd',
    attribution: '&copy; OpenStreetMap &copy; CARTO',
  }),
  sat: L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Esri' }),
};
BASE.street.addTo(map);
let baseKey = 'street';
const layer = L.layerGroup().addTo(map);

/** Cluster bubble: size and tone scale with count so density reads at a glance. */
function clusterIcon(n) {
  const s = n >= 5000 ? 64 : n >= 1000 ? 56 : n >= 250 ? 48 : n >= 50 ? 41 : 34;
  const tier = n >= 1000 ? 'c3' : n >= 100 ? 'c2' : 'c1';
  const label = n >= 10000 ? Math.round(n / 1000) + 'k'
    : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n;
  return L.divIcon({
    html: `<div class="cl ${tier}" style="width:${s}px;height:${s}px;line-height:${s}px">${label}</div>`,
    className: '', iconSize: [s, s], iconAnchor: [s / 2, s / 2],
  });
}

/** Price pin — the Zillow/LoopNet pattern: the number *is* the marker. */
function pinIcon(it, active) {
  const p = it.price_n;
  const isLease = it.transaction_type === 'lease';
  // A bare "—" reads as a rendering glitch rather than a deliberate "no listed price"
  // -- spell it out instead.
  const label = p == null ? 'Call' : (isLease ? '$' + Number(p).toFixed(0) : money(p));
  // Bold Montserrat digits run wider than this used to assume (7.4px/char left the
  // background pill a few px narrower than "$995K" actually renders at, so the text
  // visibly overran the pill's edge -- looked "cut off" rather than clipped, but
  // equally wrong). Padding also needs to be accounted for explicitly rather than
  // folded into a fudge factor.
  const PAD_X = 9, BORDER = 1.5, CHAR_W = 8.6;
  const w = Math.max(40, String(label).length * CHAR_W + PAD_X * 2 + BORDER * 2);
  return L.divIcon({
    html: `<div class="pin ${active ? 'on' : ''} ${isLease ? 'lease' : 'sale'}">${esc(label)}</div>`,
    className: '', iconSize: [w, 24], iconAnchor: [w / 2, 26],
  });
}

/* ------------------------------------------------------------ querying ---- */
const FILTER_IDS = ['status', 'state', 'source', 'subtype', 'priceMin', 'priceMax',
                    'sqftMin', 'sqftMax', 'capMin', 'yearMin'];

const SKELETON = Array.from({ length: 6 }, () => `
  <div class="skel" aria-hidden="true">
    <div class="sk sk-thumb"></div>
    <div>
      <div class="sk sk-line w1"></div>
      <div class="sk sk-line w2"></div>
      <div class="sk sk-line w3"></div>
    </div>
  </div>`).join('');

function params(extra = {}) {
  const p = new URLSearchParams();
  if (state.txn) p.set('txn', state.txn);
  const q = $('#q').value.trim();
  if (q) p.set('q', q);
  for (const id of FILTER_IDS) {
    const el = $('#' + id);
    const v = el.value;
    // Mark the control itself so an engaged filter is visible at a glance. `status`
    // always carries a value, so it is styled as the primary lens instead.
    if (id !== 'status') el.classList.toggle('on', !!v);
    if (v) p.set(id, v);
  }
  for (const [k, v] of Object.entries(extra)) if (v != null) p.set(k, v);
  return p;
}

/** Map bounds as "s,w,n,e", or null when the map has no usable size yet.
 *  If Leaflet initialises before the grid has laid out, getBounds() collapses to a
 *  single point and every query matches zero rows — so callers must treat null as
 *  "no bounds filter" rather than sending a degenerate box. */
function bboxStr() {
  // A drawn trade area outranks the viewport: the point of drawing one is to keep the
  // result set pinned to that area while you pan and zoom around inside it.
  if (state.area) {
    const b = state.area;
    return [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()]
      .map((n) => n.toFixed(5)).join(',');
  }
  const sz = map.getSize();
  if (!sz.x || !sz.y) return null;
  const b = map.getBounds();
  if (b.getNorth() - b.getSouth() < 1e-6) return null;
  return [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()]
    .map((n) => n.toFixed(5)).join(',');
}

/** Drag a rectangle on the map to pin the search to that trade area.
 *
 * Implemented directly on Leaflet's own events rather than pulling in Leaflet.draw --
 * a rectangle is a mousedown/mousemove/mouseup and the vendored bundle stays small.
 * Map dragging is suspended while arming so the drag draws instead of panning. */
function initAreaDraw() {
  let origin = null, rect = null;
  const btn = $('#draw');

  const stop = () => {
    btn.classList.remove('on');
    map.dragging.enable();
    map.getContainer().style.cursor = '';
    map.off('mousedown', onDown).off('mousemove', onMove).off('mouseup', onUp);
  };
  const onDown = (e) => {
    origin = e.latlng;
    rect = L.rectangle([origin, origin], { className: 'drawrect', weight: 2, interactive: false })
      .addTo(map);
  };
  const onMove = (e) => { if (origin && rect) rect.setBounds(L.latLngBounds(origin, e.latlng)); };
  const onUp = (e) => {
    if (!origin) return;
    const b = L.latLngBounds(origin, e.latlng);
    origin = null;
    stop();
    // A click rather than a drag: treat as a cancel, not a zero-area box that would
    // match nothing and look like the app broke.
    if (map.latLngToContainerPoint(b.getNorthEast())
           .distanceTo(map.latLngToContainerPoint(b.getSouthWest())) < 12) {
      if (rect) { map.removeLayer(rect); rect = null; }
      return;
    }
    if (state.areaRect) map.removeLayer(state.areaRect);
    state.area = b;
    state.areaRect = rect;
    rect = null;
    $('#areabar').hidden = false;
    refreshAll();
  };

  btn.addEventListener('click', () => {
    if (btn.classList.contains('on')) { stop(); return; }
    btn.classList.add('on');
    map.dragging.disable();
    map.getContainer().style.cursor = 'crosshair';
    map.on('mousedown', onDown).on('mousemove', onMove).on('mouseup', onUp);
  });

  $('#areaclear').addEventListener('click', () => {
    if (state.areaRect) map.removeLayer(state.areaRect);
    state.area = null; state.areaRect = null;
    $('#areabar').hidden = true;
    refreshAll();
  });
}

/** Resolve once the map actually has dimensions.
 *
 * Belt-and-suspenders: a single ResizeObserver + one fallback timeout worked in local
 * testing but was still racy on a slower host (Render's free tier cold-starts well
 * behind a local dev server) -- the container could reach its final size after the
 * observer had already fired once with a stale reading, leaving Leaflet's cached size
 * at 0x0 with no further correction. Four independent triggers now race to be the one
 * that calls invalidateSize(): the ResizeObserver, `window.load` (fires only once
 * every subresource -- fonts, tiles' first request -- has settled), a short poll for
 * the first few seconds, and a last-resort timeout. */
function whenSized() {
  return new Promise((resolve) => {
    const el = document.getElementById('map');
    const ok = () => { const s = map.getSize(); return s.x > 0 && s.y > 0; };
    let done = false;
    const settle = () => {
      if (done) return;
      map.invalidateSize();
      if (ok()) { done = true; cleanup(); resolve(); }
    };
    if (ok()) return resolve();

    const ro = new ResizeObserver(settle);
    ro.observe(el);
    window.addEventListener('load', settle);
    const poll = setInterval(settle, 250);
    const stop = setTimeout(() => { done = true; cleanup(); resolve(); }, 6000);

    function cleanup() {
      ro.disconnect();
      window.removeEventListener('load', settle);
      clearInterval(poll);
      clearTimeout(stop);
    }
  });
}

/* Keep Leaflet's cached size in step with the container for the whole session, not just
 * at boot. The container can change without a window resize (viewport emulation, the
 * sidebar breakpoint switching to rows), and a stale cached size makes getBounds()
 * report a viewport that no longer matches what is drawn — so the map would query a
 * different area than the user is looking at. */
let sizeTimer = null;
new ResizeObserver(() => {
  clearTimeout(sizeTimer);
  sizeTimer = setTimeout(() => {
    const before = map.getSize();
    map.invalidateSize();
    const after = map.getSize();
    if (before.x !== after.x || before.y !== after.y) scheduleMap();
  }, 150);
}).observe(document.getElementById('map'));

let mapTimer = null;
function scheduleMap() {
  clearTimeout(mapTimer);
  mapTimer = setTimeout(loadMap, 220);   // debounce pan/zoom bursts
}

async function loadMap() {
  const id = ++state.mapReq;
  $('#maploading').hidden = false;
  const p = params({ zoom: Math.round(map.getZoom()), bbox: bboxStr() });
  let d;
  try {
    d = await (await fetch('/api/clusters?' + p)).json();
  } catch { $('#maploading').hidden = true; return; }
  if (id !== state.mapReq) return;       // a newer request already won
  $('#maploading').hidden = true;

  layer.clearLayers();
  state.markers.clear();

  if (d.mode === 'clusters') {
    for (const c of d.items) {
      if (c.lat == null) continue;
      L.marker([c.lat, c.lng], { icon: clusterIcon(c.n) })
        .on('click', () => map.setView([c.lat, c.lng], Math.min(map.getZoom() + 3, 14)))
        .addTo(layer);
    }
    hint(d.total ? `${num(d.total)} listings · click a cluster or zoom in for individual properties`
                 : 'No listings match these filters');
  } else {
    // Multiple listings at the exact same coordinates are common here -- a property
    // cross-listed on several marketplaces, or a lease listing split into several
    // spaces at one address. Stacked exactly, only the top marker is visible or
    // clickable. Spread duplicates in a small pixel-radius ring around the true point
    // (computed in screen space via the map's own projection, so the separation looks
    // the same at any zoom level, rather than a fixed lat/lng offset that would shrink
    // to nothing zoomed out and balloon zoomed in).
    // Grouped by *screen position*, not by coordinate. Rounding lat/lng only catches
    // listings at literally the same point, but what makes a marker unreachable is
    // sharing pixels: two addresses a few metres apart differ in the 5th decimal place
    // yet land on the same pixel, and a live check over Manhattan found 34 such stacks,
    // some four deep, where every marker but the top one was unclickable.
    const CELL = 22;   // px; below roughly half a pin's width markers visibly collide
    const groups = new Map();
    for (const it of d.items) {
      if (it.lat_r == null) continue;
      const p = map.latLngToContainerPoint([it.lat_r, it.lng_r]);
      const key = Math.round(p.x / CELL) + ':' + Math.round(p.y / CELL);
      (groups.get(key) || groups.set(key, []).get(key)).push(it);
    }
    for (const group of groups.values()) {
      const n = group.length;
      const center = map.latLngToContainerPoint([group[0].lat_r, group[0].lng_r]);
      group.forEach((it, i) => {
        let latlng;
        if (n === 1) {
          latlng = [it.lat_r, it.lng_r];
        } else {
          const angle = (2 * Math.PI * i) / n;
          // Circle-packing math, not a guessed constant: for n pins evenly spaced on a
          // ring of radius R, the gap between neighbours is 2R*sin(pi/n).
          //
          // Hard-capped, though. That formula grows without bound in n, and once
          // markers are grouped by screen cell a busy block can hold twenty of them --
          // which asked for a 224px ring and drew pins whole streets away from their
          // real address. A marker in the wrong place is a worse lie than an
          // overlapping one, so past the cap the ring stops growing and some residual
          // overlap is accepted; that density is what zooming in is for.
          const PIN_W = 60, GAP = 10, MAX_R = 30;
          const radius = Math.min(MAX_R,
            Math.max(20, (PIN_W + GAP) / (2 * Math.sin(Math.PI / n))));
          const pt = L.point(center.x + radius * Math.cos(angle), center.y + radius * Math.sin(angle));
          latlng = map.containerPointToLatLng(pt);
        }
        const m = L.marker(latlng, { icon: pinIcon(it, false), riseOnHover: true })
          .on('click', () => openDetail(it.pk))
          .on('mouseover', () => highlightCard(it.pk, true))
          .on('mouseout', () => highlightCard(it.pk, false))
          .addTo(layer);
        state.markers.set(it.pk, m);
      });
    }
    hint(d.capped
      ? `Showing ${num(d.items.length)} of ${num(d.total)} here — zoom in for the rest`
      : '');
  }
}

function hint(text) {
  const el = $('#maphint');
  el.textContent = text || '';
  el.hidden = !text;
}

/* ------------------------------------------------------------- sidebar ---- */
async function loadList(reset) {
  if (state.loading) return;
  state.loading = true;
  const id = ++state.listReq;
  if (reset) {
    state.page = 0; state.items = [];
    // Only on a first page, and only if the list is currently empty -- re-filtering an
    // already-populated list should not flash skeletons over results that are about to
    // be replaced in a single frame.
    if (!$('#cards').children.length) $('#cards').innerHTML = SKELETON;
  }
  const p = params({ sort: state.sort, page: state.page, size: 40, bbox: bboxStr() });
  let d;
  try {
    const res = await fetch('/api/listings?' + p);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    d = await res.json();
  } catch {
    state.loading = false;
    if (id !== state.listReq) return;      // superseded; a newer request owns the UI now
    // A dropped request used to return silently, leaving the list blank and the count
    // showing whatever the previous response said -- indistinguishable from a genuine
    // "no results", which is exactly how this surfaced: an empty sidebar reading "No
    // listings match" beside a map that had loaded hundreds of pins from its own
    // separate request. Free-tier cold starts make that first fetch a real failure case,
    // so say so, and offer a way out.
    if (reset) {
      $('#cards').innerHTML =
        '<div class="empty">Couldn’t load listings. <button type="button" class="linkbtn" id="sbretry">Retry</button></div>';
      $('#sbretry')?.addEventListener('click', () => loadList(true));
    }
    $('#sbcount').textContent = 'Couldn’t load';
    return;
  }
  if (id !== state.listReq) { state.loading = false; return; }

  // Clear only once a winning response is in hand. Clearing before the fetch opened a
  // window where a superseded or failed request emptied the list and nothing refilled it.
  if (reset) $('#cards').innerHTML = '';
  state.total = d.total; state.hasMore = d.hasMore;
  state.items.push(...d.items);
  renderCards(d.items);
  $('#count').textContent = `${num(d.total)} listings`;
  $('#sbcount').textContent = d.total
    ? `${num(d.total)} ${state.area ? 'in drawn area' : 'in view'}`
    : 'No listings match';
  if (state.area) $('#areatext').textContent = `${num(d.total)} listings in drawn area`;
  $('#sbmore').hidden = !d.hasMore;
  $('#sbmore').textContent = d.hasMore ? 'Scroll for more…' : '';
  if (reset && !d.total) {
    $('#cards').innerHTML = '<div class="empty">Nothing here. Try zooming out or clearing filters.</div>';
  }
  state.loading = false;
  // The sentinel may still be on screen (short page, or the observer fired while this
  // request was in flight) — re-check rather than wait for another intersection change.
  if (typeof maybeLoadMore === 'function') requestAnimationFrame(maybeLoadMore);
}

function renderCards(items) {
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const el = document.createElement('article');
    el.className = 'card';
    el.dataset.pk = it.pk;
    const price = it.price_n == null
      ? '<span class="muted">Price on request</span>'
      : it.transaction_type === 'lease'
        ? `$${Number(it.price_n).toFixed(2)}<small>/SF/yr</small>`
        : money(it.price_n);
    const bits = [
      it.sqft_n ? num(it.sqft_n) + ' SF' : null,
      it.cap_rate ? Number(it.cap_rate).toFixed(2) + '% cap' : null,
      // Multi-space listings are split into one record per space, so say which.
      Number(it.space_count) > 1 ? `1 of ${it.space_count} spaces` : null,
      it.property_subtype || null,
    ].filter(Boolean);
    const gone = !!(it.delisted_on && it.delisted_on !== '');
    if (gone) el.classList.add('gone');
    el.innerHTML = `
      <div class="thumb">${it.img
        ? `<img loading="lazy" src="${esc(it.img)}" alt="" onerror="this.remove()">`
        : '<div class="noimg">No photo</div>'}
        <span class="badge ${esc(it.transaction_type)}">${it.transaction_type === 'lease' ? 'Lease' : 'Sale'}</span>
        ${gone ? `<span class="badge off">Off-market ${esc(String(it.delisted_on).slice(0, 10))}</span>` : ''}
      </div>
      <div class="cbody">
        <div class="cprice">${price}</div>
        <div class="cname">${esc(it.name || it.address || 'Untitled listing')}</div>
        <div class="caddr">${esc([it.address, it.city, it.state].filter(Boolean).join(', '))}</div>
        <div class="cbits">${bits.map((b) => `<span>${esc(b)}</span>`).join('')}</div>
        <div class="csrc">${esc(it.source_site)}</div>
      </div>`;
    el.addEventListener('click', () => openDetail(it.pk));
    el.addEventListener('mouseenter', () => focusPin(it.pk, true));
    el.addEventListener('mouseleave', () => focusPin(it.pk, false));
    frag.appendChild(el);
  }
  $('#cards').appendChild(frag);
}

/* card <-> pin synchronisation, both directions */
function focusPin(pk, on) {
  const m = state.markers.get(pk);
  if (!m) return;
  const it = state.items.find((x) => x.pk === pk);
  if (it) m.setIcon(pinIcon(it, on));
  m.setZIndexOffset(on ? 1000 : 0);
}
function highlightCard(pk, on) {
  const el = document.querySelector(`.card[data-pk="${pk}"]`);
  if (!el) return;
  el.classList.toggle('hot', on);
  if (on) el.scrollIntoView({ block: 'nearest' });
}

/* Infinite scroll via a sentinel at the end of the list. An IntersectionObserver is
 * used instead of scroll-offset arithmetic because it re-evaluates on content and size
 * changes too, so appending a page (which moves the sentinel) reliably re-arms it. */
const moreObserver = new IntersectionObserver((entries) => {
  if (!entries.some((e) => e.isIntersecting)) return;
  maybeLoadMore();
}, { root: $('#sidebar'), rootMargin: '400px' });
moreObserver.observe($('#sbmore'));
// Second trigger: a plain scroll listener. Both funnel into the same guarded call, so
// firing twice is harmless, and the list still pages if either mechanism misbehaves.
$('#sidebar').addEventListener('scroll', maybeLoadMore, { passive: true });

/** Load the next page if the sentinel is on screen and we aren't already busy.
 *  Called both from the observer and after every load: the observer only fires on a
 *  *change* in intersection, so if it fires while a load is in flight the request is
 *  dropped and never retried. Re-checking after each load closes that gap (and keeps
 *  filling when one page doesn't reach the bottom of a tall sidebar). */
function maybeLoadMore() {
  if (!state.hasMore || state.loading) return;
  const el = $('#sbmore'), root = $('#sidebar');
  if (el.hidden) return;
  const r = el.getBoundingClientRect(), rr = root.getBoundingClientRect();
  if (r.top <= rr.bottom + 400) {
    state.page += 1;
    loadList(false);
  }
}

/* -------------------------------------------------------------- detail ---- */
async function openDetail(pk, fromHistory) {
  state.activePk = pk;
  if (!fromHistory) syncUrl(true);       // Back should close the drawer
  $('#drawer').hidden = false; $('#scrim').hidden = false;
  $('#dbody').innerHTML = '<div class="dloading">Loading property…</div>';
  let d;
  try { d = await (await fetch('/api/listing/' + pk)).json(); }
  catch { $('#dbody').innerHTML = '<div class="dloading">Could not load this listing.</div>'; return; }
  if (state.activePk !== pk) return;
  $('#dbody').innerHTML = detailHtml(d);
  $('#dbody').scrollTop = 0;
  wireGallery();
}

const factRow = (label, value) =>
  value == null || value === '' ? '' :
  `<div class="fact"><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;

// Trade-area demographics, rendered as one row per ring so the 1/3/5-mile figures can be
// read against each other -- the useful comparison for retail siting is how fast the
// catchment grows with distance, not any single ring in isolation.
const DEMOG_ROWS = [
  ['Population', 'pop', (v) => num(v)],
  ['Households', 'households', (v) => num(v)],
  // Deliberately "Avg." not "Median": this is aggregate income / households. See the
  // aggregation note in scripts/build_demographics.py.
  ['Avg. household income', 'avg_household_income', (v) => '$' + num(v)],
  ['Employees', 'employees', (v) => num(v)],
  ['Median age', 'approx_median_age', (v) => Number(v).toFixed(1)],
];

function demographicsHtml(dem) {
  if (!dem) return '';
  const rings = [1, 3, 5].filter((r) => dem['pop_' + r + 'mi']);
  if (!rings.length) return '';
  const head = rings.map((r) => `<th>${r} mi</th>`).join('');
  const body = DEMOG_ROWS.map(([label, key, fmt]) => {
    const cells = rings.map((r) => {
      const v = dem[key + '_' + r + 'mi'];
      return `<td>${v == null || v === '' ? '—' : esc(fmt(v))}</td>`;
    }).join('');
    return `<tr><th scope="row">${esc(label)}</th>${cells}</tr>`;
  }).join('');
  return `<section class="dsec">
      <h3>Trade area demographics</h3>
      <div class="demwrap"><table class="dem">
        <thead><tr><th scope="col"></th>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table></div>
      <div class="demsrc">US Census — 2020 Centers of Population, ACS 2019–2023 5-year estimates</div>
    </section>`;
}

function detailHtml(d) {
  const e = d.detail || {};
  const isLease = d.transaction_type === 'lease';
  const imgs = Array.isArray(e.image_urls) && e.image_urls.length
    ? e.image_urls : (d.img ? [d.img] : []);
  // Lease rates are $/SF/yr and are often published as a range ("$8 - $14"), which we
  // keep verbatim rather than collapsing to one number — but it still needs its unit.
  const rateStr = e.lease_rate_yearly
    ? (/\/SF/i.test(e.lease_rate_yearly) ? e.lease_rate_yearly : e.lease_rate_yearly + ' /SF/yr')
    : null;
  const price = d.price_n != null
    ? (isLease ? `$${Number(d.price_n).toFixed(2)} /SF/yr` : money(d.price_n))
    : (rateStr || 'Price on request');

  // The enricher appends broker highlights to description; split them back apart so
  // the marketing bullets render as a list instead of a wall of text.
  let desc = stripHtml(e.description || '');
  let highlights = [];
  const hi = desc.indexOf('Highlights:');
  if (hi >= 0) {
    highlights = desc.slice(hi + 11).split('\n-')
      .map((s) => s.replace(/^[-\s]+/, '').trim()).filter(Boolean);
    desc = desc.slice(0, hi).trim();
  }

  const facts = [
    factRow('Price', price),
    // Per-SF figures are shown exactly: abbreviating $1,056/SF to "$1K" reads as a
    // different number and is useless for comparing assets.
    isLease ? factRow('Rate / month', e.lease_rate_monthly)
            : factRow('Price / SF', e.price_per_sqft
                ? '$' + Number(e.price_per_sqft).toLocaleString('en-US', { maximumFractionDigits: 2 })
                : null),
    factRow('Cap rate', e.cap_rate ? Number(e.cap_rate).toFixed(2) + '%' : null),
    factRow('Building size', (e.sqft || d.sqft_n) ? num(e.sqft || d.sqft_n) + ' SF' : null),
    // A handful of enriched rows (1,325 across 4 sources, a source-side parsing
    // artifact) carry an absurd acreage like 9.18e-6 -- displaying that raw would dump
    // ~20 digits into the fact card. Round for display, and treat anything outside a
    // sane range (no real retail lot is under ~0.001ac or over 10,000ac) as absent
    // rather than show a number known to be wrong.
    factRow('Lot size', (e.lot_size_acres && e.lot_size_acres >= 0.001 && e.lot_size_acres <= 10000)
      ? Number(e.lot_size_acres).toFixed(2) + ' ac' : null),
    factRow('Year built', e.year_built),
    factRow('Year renovated', e.year_renovated),
    factRow('Stories', e.stories),
    factRow('Parking', e.parking_spaces ? num(e.parking_spaces) + ' spaces' : null),
    factRow('Units', e.units),
    factRow('Suites', e.num_suites),
    factRow('Tenancy', e.tenancy),
    factRow('Occupancy', e.occupancy),
    factRow('Lease type', e.lease_type),
    isLease ? factRow('Lease term', e.sale_condition) : factRow('Sale condition', e.sale_condition),
    factRow('Zoning', e.zoning),
    factRow('APN / parcel', e.apn),
    isLease ? '' : factRow('Ownership', e.ownership),
    factRow('Property type', d.property_subtype),
  ].filter(Boolean).join('');

  const brokers = e.broker_names
    ? e.broker_names.split(';').map((b) => {
        const m = b.match(/^(.*?)\s*\((.*?)\)\s*$/);
        const name = (m ? m[1] : b).trim();
        const parts = (m ? m[2] : '').split('/').map((s) => s.trim()).filter(Boolean);
        return `<li><span class="bn">${esc(name)}</span>${parts.map((p) =>
          p.includes('@')
            ? `<a href="mailto:${esc(p)}">${esc(p)}</a>`
            : `<a href="tel:${esc(p.replace(/\D/g, ''))}">${esc(p)}</a>`).join('')}</li>`;
      }).join('')
    : '';

  return `
    <div class="dhero">
      ${imgs.length
        ? `<img id="gmain" src="${esc(imgs[0])}" alt="" onerror="this.style.display='none'">
           ${imgs.length > 1 ? `<div class="gcount" id="gcount">1 / ${imgs.length}</div>` : ''}`
        : '<div class="noimg big">No photos available</div>'}
    </div>
    ${imgs.length > 1 ? `<div class="gstrip" id="gstrip">${imgs.slice(0, 24).map((u, i) =>
      `<img data-i="${i}" class="${i === 0 ? 'on' : ''}" loading="lazy" src="${esc(u)}" alt="" onerror="this.remove()">`
    ).join('')}</div>` : ''}

    <div class="dhead">
      <div class="dprice">${esc(price)}</div>
      <h2>${esc(d.name || d.address || 'Untitled listing')}</h2>
      <div class="daddr">${esc([d.address, d.city, d.state, d.zip].filter(Boolean).join(', '))}</div>
      <div class="dtags">
        <span class="tag ${esc(d.transaction_type)}">${isLease ? 'For Lease' : 'For Sale'}</span>
        ${d.property_subtype ? `<span class="tag">${esc(d.property_subtype)}</span>` : ''}
        <span class="tag src">${esc(d.source_site)}</span>
        ${d.detail ? '' : '<span class="tag warn">Card data only — not yet enriched</span>'}
      </div>
    </div>

    ${facts ? `<section class="dsec"><h3>Property facts</h3><dl class="facts">${facts}</dl></section>` : ''}
    ${highlights.length ? `<section class="dsec"><h3>Investment highlights</h3>
      <ul class="hl">${highlights.map((h) => `<li>${esc(h)}</li>`).join('')}</ul></section>` : ''}
    ${desc ? `<section class="dsec"><h3>Description</h3><p class="ddesc">${esc(desc)}</p></section>` : ''}
    ${demographicsHtml(d.demographics)}
    ${brokers ? `<section class="dsec"><h3>Listing contacts</h3>
      <ul class="brokers">${brokers}</ul>
      ${e.brokerage ? `<div class="bfirm">${esc(e.brokerage)}</div>` : ''}</section>` : ''}

    <section class="dsec dactions">
      <a class="btn primary" href="${esc(d.source_url)}" target="_blank" rel="noopener">View original listing ↗</a>
      ${d.lat_r ? `<a class="btn" href="https://www.google.com/maps/search/?api=1&query=${d.lat_r},${d.lng_r}" target="_blank" rel="noopener">Open in Maps</a>` : ''}
    </section>

    <div class="dmeta">
      ${e.num_images ? `${e.num_images} photos · ` : ''}
      ${Array.isArray(e.comp_ids) && e.comp_ids.length ? `${e.comp_ids.length} comparables · ` : ''}
      Source: ${esc(d.source_site)}${d.listed_on ? ' · listed ' + esc(String(d.listed_on).slice(0, 10)) : ''}
    </div>`;
}

function wireGallery() {
  const strip = $('#gstrip'), main = $('#gmain');
  if (!strip || !main) return;
  strip.addEventListener('click', (ev) => {
    const t = ev.target.closest('img[data-i]');
    if (!t) return;
    main.src = t.src;
    main.style.display = '';
    strip.querySelectorAll('img').forEach((i) => i.classList.toggle('on', i === t));
    const c = $('#gcount');
    if (c) c.textContent = `${Number(t.dataset.i) + 1} / ${strip.children.length}`;
  });
}

function closeDetail(fromHistory) {
  $('#drawer').hidden = true; $('#scrim').hidden = true; state.activePk = null;
  if (!fromHistory) syncUrl(false);
}
$('#dclose').addEventListener('click', () => closeDetail());
$('#scrim').addEventListener('click', () => closeDetail());
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#drawer').hidden) closeDetail(); });

/* ------------------------------------------------------------ deep links --- */
/* The whole view lives in the URL so a search or a specific property can be shared and
 * survives a refresh — the drawer used to be purely ephemeral. `replaceState` is used
 * for filter changes (they shouldn't each add a history entry) and `pushState` only for
 * opening a property, so Back closes the drawer rather than undoing filter edits. */
let restoring = false;

function syncUrl(push) {
  if (restoring) return;
  const p = params();
  p.delete('bbox');
  const c = map.getCenter();
  p.set('at', `${c.lat.toFixed(4)},${c.lng.toFixed(4)},${Math.round(map.getZoom())}`);
  if (state.sort !== 'new') p.set('sort', state.sort);
  if (state.activePk) p.set('pk', state.activePk);
  const url = '?' + p.toString();
  if (push) history.pushState({ pk: state.activePk }, '', url);
  else history.replaceState({ pk: state.activePk }, '', url);
}

function applyUrl() {
  const p = new URLSearchParams(location.search);
  restoring = true;
  const txn = p.get('txn');
  if (txn !== null) {
    state.txn = txn;
    [...$('#txn').children].forEach((b) => b.classList.toggle('on', b.dataset.txn === txn));
  }
  if (p.get('q')) $('#q').value = p.get('q');
  for (const id of FILTER_IDS) {
    const v = p.get(id);
    if (v != null && $('#' + id)) $('#' + id).value = v;
  }
  if (p.get('sort')) { state.sort = p.get('sort'); $('#sort').value = state.sort; }
  const at = p.get('at');
  if (at) {
    const [lat, lng, z] = at.split(',').map(Number);
    if (Number.isFinite(lat) && Number.isFinite(lng)) map.setView([lat, lng], z || 4);
  }
  restoring = false;
  return p.get('pk');
}

window.addEventListener('popstate', async () => {
  const pk = applyUrl();
  refreshAll();
  if (pk) await openDetail(Number(pk), true); else closeDetail(true);
});

/* --------------------------------------------------------------- wiring --- */
function refreshAll() { loadMap(); loadList(true); syncUrl(false); }

$('#txn').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-txn]');
  if (!b) return;
  [...$('#txn').children].forEach((x) => x.classList.toggle('on', x === b));
  state.txn = b.dataset.txn;
  refreshAll();
});

let qTimer;
$('#q').addEventListener('input', () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(refreshAll, 320);
});
for (const id of FILTER_IDS) $('#' + id).addEventListener('change', refreshAll);
$('#sort').addEventListener('change', () => { state.sort = $('#sort').value; loadList(true); });
$('#reset').addEventListener('click', () => {
  $('#q').value = '';
  for (const id of FILTER_IDS) $('#' + id).value = '';
  $('#status').value = 'active';    // status has no empty option; restore its default
  refreshAll();
});
$('#basemap').addEventListener('click', () => {
  map.removeLayer(BASE[baseKey]);
  baseKey = baseKey === 'street' ? 'sat' : 'street';
  BASE[baseKey].addTo(map);
  $('#basemap').textContent = baseKey === 'street' ? 'Satellite' : 'Street';
});
$('#fit').addEventListener('click', async () => {
  const p = params({ zoom: 4, bbox: '-90,-180,90,180' });
  try {
    const d = await (await fetch('/api/clusters?' + p)).json();
    const pts = d.items.filter((i) => i.lat != null).map((i) => [i.lat, i.lng]);
    if (pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [40, 40] });
  } catch { /* keep current view */ }
});

map.on('moveend zoomend', () => {
  scheduleMap();
  if ($('#moveSearch').checked) loadList(true);
  syncUrl(false);          // keep the shareable position current
});

/* ---------------------------------------------------------------- boot ---- */
(async function boot() {
  await whenSized();       // never query with a collapsed bbox
  initAreaDraw();

  // Restore whatever the URL describes before the first query, then open the property
  // it names (so a shared link lands directly on that listing).
  const pk = applyUrl();
  refreshAll();             // map + list data first -- don't block it on /api/stats below
  if (pk) openDetail(Number(pk), true);

  try {
    const s = await (await fetch('/api/stats')).json();
    if (s.lifecycle) {
      const L = s.lifecycle;
      const opt = (v, label) => {
        const o = [...$('#status').options].find((x) => x.value === v);
        if (o) o.textContent = label;
      };
      opt('active', `On market (${num(L.active)})`);
      opt('new', `New this week (${num(L.new)})`);
      opt('delisted', `Went off-market (${num(L.delisted)})`);
    }
    const fill = (id, vals) => {
      const el = $('#' + id);
      for (const v of vals) {
        const o = document.createElement('option');
        o.value = typeof v === 'string' ? v : v.site;
        o.textContent = typeof v === 'string' ? v : `${v.site} (${num(v.n)})`;
        el.appendChild(o);
      }
    };
    fill('state', s.states);
    fill('source', s.sources);
    fill('subtype', s.subtypes);
  } catch { /* filters degrade to defaults; map still works */ }
})();
