/* AquaFlow fleet pages - shared map + formatting helpers.
 * Tiles: standard OpenStreetMap raster tiles (no API key needed).
 * Policy: https://operations.osmfoundation.org/policies/tiles/
 *   - attribution is always shown
 *   - no bulk downloading / prefetching; tiles come from the browser only
 *   - the page must send a Referer (see <meta name="referrer"> in fleet_base.html)
 */
(function (global) {
    'use strict';

    const OSM_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';

    function esc(v) {
        if (v === null || v === undefined) return '';
        return String(v).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
    }

    /** Create a Leaflet map with OSM tiles, tile-failure notice and auto invalidateSize(). */
    function createMap(elId, opts) {
        opts = opts || {};
        const el = document.getElementById(elId);
        const map = L.map(elId, { zoomControl: true, worldCopyJump: true }).setView(opts.center || [20.5937, 78.9629], opts.zoom || 5);

        const banner = document.getElementById(opts.bannerId || (elId + 'Banner'));
        let errors = 0, loaded = 0;
        const layer = L.tileLayer(OSM_URL, { maxZoom: 19, attribution: OSM_ATTR, crossOrigin: true }).addTo(map);
        layer.on('tileload', () => { loaded++; if (banner && loaded > 0) { errors = 0; banner.classList.remove('show'); } });
        layer.on('tileerror', () => {
            errors++;
            if (banner && errors >= 3 && loaded === 0) {
                banner.textContent = 'Map tiles could not be loaded (network or OpenStreetMap unavailable). Markers and delivery data are still shown.';
                banner.classList.add('show');
            }
        });

        // Keep the map correctly sized when the container changes (layout, rotation, panel resize).
        if (window.ResizeObserver && el) { new ResizeObserver(() => map.invalidateSize()).observe(el); }
        window.addEventListener('load', () => map.invalidateSize());
        window.addEventListener('pageshow', () => map.invalidateSize());
        setTimeout(() => map.invalidateSize(), 250);
        return map;
    }

    function truckIcon(color, dim) {
        return L.divIcon({
            className: '',
            html: `<div style="background:${color};opacity:${dim ? 0.65 : 1};width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 4px ${color}33,0 2px 6px rgba(10,26,34,.25);"><i class="fa-solid fa-truck" style="color:#fff;font-size:12px;"></i></div>`,
            iconSize: [26, 26], iconAnchor: [13, 13], popupAnchor: [0, -14]
        });
    }

    /** "12 s ago", "3 min ago"... from an age in seconds computed on the server. */
    function fmtAge(sec) {
        if (sec === null || sec === undefined) return null;
        if (sec < 60) return sec + ' s ago';
        if (sec < 3600) return Math.floor(sec / 60) + ' min ago';
        if (sec < 86400) return Math.floor(sec / 3600) + ' h ago';
        return Math.floor(sec / 86400) + ' d ago';
    }

    const FRESH = {
        live: { cls: 'b-green', icon: 'fa-signal', label: 'Live' },
        stale: { cls: 'b-amber', icon: 'fa-clock', label: 'Stale' },
        unavailable: { cls: 'b-grey', icon: 'fa-location-crosshairs', label: 'No location' }
    };
    function freshBadge(f) {
        const x = FRESH[f] || FRESH.unavailable;
        return `<span class="badge ${x.cls}"><i class="fa-solid ${x.icon}"></i>${x.label}</span>`;
    }

    /** Honest one-line description of how fresh a location is. */
    function freshText(f, ageSec, label) {
        if (f === 'live') return 'Live GPS · updated ' + (fmtAge(ageSec) || 'just now');
        if (f === 'stale') return 'Last known location · ' + (label || 'unknown time') + (ageSec != null ? ' (' + fmtAge(ageSec) + ')' : '');
        return 'No GPS location received from this driver';
    }

    global.AquaFleet = { createMap, truckIcon, esc, fmtAge, freshBadge, freshText, FRESH };
})(window);
