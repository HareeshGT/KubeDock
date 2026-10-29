(() => {
  'use strict';
  const API = 'https://api.github.com/repos/HareeshGT/KubeDock/releases';
  const RELEASES_URL = 'https://github.com/HareeshGT/KubeDock/releases';
  const CACHE_KEY = 'kubedock-public-releases-v1';
  const CACHE_TTL = 10 * 60 * 1000;
  const $ = id => document.getElementById(id);
  const platformRoot = $('platform-cards');
  const releaseList = $('release-list');
  const status = $('release-status');
  const search = $('release-search');
  const filter = $('release-filter');
  const loadMore = $('load-more');
  let releases = [];
  let page = 1;
  let hasMore = true;
  let loading = false;
  let query = '';
  let filterMode = 'all';
  let cacheUsed = false;

  const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const formatBytes = bytes => {
    if (!Number.isFinite(Number(bytes)) || Number(bytes) < 0) return '';
    const units = ['B','KB','MB','GB'];
    let size = Number(bytes), unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
    return (unit === 0 ? size : size.toFixed(1)) + ' ' + units[unit];
  };
  const formatDate = value => {
    if (!value) return 'Date unavailable';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Date unavailable' : new Intl.DateTimeFormat(undefined,{year:'numeric',month:'short',day:'numeric'}).format(date);
  };
  const normalize = raw => ({
    id: String(raw.id),
    tag: String(raw.tag_name || 'untagged'),
    title: String(raw.name || raw.tag_name || 'KubeDock release'),
    date: raw.published_at || raw.created_at || null,
    url: raw.html_url || RELEASES_URL,
    body: String(raw.body || ''),
    prerelease: Boolean(raw.prerelease),
    draft: Boolean(raw.draft),
    assets: Array.isArray(raw.assets) ? raw.assets.filter(a => a && typeof a.browser_download_url === 'string' && /^https:\/\/github\.com\/HareeshGT\/KubeDock\/releases\/download\//.test(a.browser_download_url)).map(a => ({name:String(a.name || 'Download asset'),url:a.browser_download_url,size:Number(a.size) || 0,type:String(a.content_type || '')})) : []
  });
  const validPublicRelease = r => r && !r.draft;
  const isZip = a => /\.zip$/i.test(a.name);
  const isWindows = a => /windows|win32|win-x64|win-x86_64/i.test(a.name);
  const isMac = a => /macos|darwin|mac[-_ ]?os|\.dmg$/i.test(a.name);
  const isLinux = a => /linux|\.appimage$/i.test(a.name);
  const osType = asset => isWindows(asset) ? 'windows' : isMac(asset) ? 'macos' : isLinux(asset) ? 'linux' : 'other';
  const stableSorted = list => [...list].sort((a,b) => {
    const dateDiff = new Date(b.date || 0) - new Date(a.date || 0);
    if (Number.isFinite(dateDiff) && dateDiff !== 0) return dateDiff;
    return a.tag.localeCompare(b.tag, undefined, {numeric:true,sensitivity:'base'});
  });
  const latestStable = list => stableSorted(list.filter(r => !r.prerelease))[0]?.id || null;

  function setStatus(message, type = '') {
    if (!status) return;
    status.className = 'release-status' + (type ? ' ' + type : '');
    status.textContent = '';
    status.append(document.createTextNode(message + ' '));
    if (type === 'error' || type === 'success') {
      const link = document.createElement('a');
      link.href = RELEASES_URL; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = 'Open GitHub Releases';
      status.append(link);
    }
  }
  function assetMarkup(asset, compact = false) {
    const name = escapeHTML(asset.name);
    const size = formatBytes(asset.size);
    return compact
      ? '<a class="asset-link" href="' + escapeHTML(asset.url) + '" download="' + name + '" rel="noopener noreferrer"><span>↓</span><span>' + name + (size ? '<small class="asset-meta">' + escapeHTML(size) + '</small>' : '') + '</span><span class="asset-arrow">↗</span></a>'
      : '<div class="asset-item"><span class="asset-file-icon">▤</span><div class="asset-label"><div class="asset-name">' + name + '</div><div class="asset-size">' + escapeHTML(size || 'Size unavailable') + (asset.type ? ' · ' + escapeHTML(asset.type) : '') + '</div></div><a class="asset-download" href="' + escapeHTML(asset.url) + '" download="' + name + '" rel="noopener noreferrer" aria-label="Download ' + name + '">Download ↓</a></div>';
  }
  function renderPlatformCards() {
    if (!platformRoot) return;
    const current = stableSorted(releases.filter(r => !r.prerelease && r.assets.length));
    const platforms = [
      {id:'windows',name:'Windows',symbol:'⊞',detail:'Windows x86-64',match:a => isWindows(a)},
      {id:'macos',name:'macOS',symbol:'⌘',detail:'Apple Silicon and Intel',match:a => isMac(a)},
      {id:'linux',name:'Linux',symbol:'◈',detail:'Linux x86-64',match:a => isLinux(a)}
    ];
    platformRoot.innerHTML = platforms.map(p => {
      let selectedRelease = null, selectedAssets = [];
      for (const release of current) {
        const matches = release.assets.filter(p.match);
        if (matches.length) { selectedRelease = release; selectedAssets = matches; break; }
      }
      if (!selectedRelease) return '<article class="platform-card"><div class="platform-head"><span class="platform-symbol">' + p.symbol + '</span><span class="release-badge">No matching asset</span></div><h3>' + p.name + '</h3><p>' + p.detail + '</p><div class="platform-empty">No stable package detected in the public release data.</div><a class="text-link" href="' + RELEASES_URL + '" target="_blank" rel="noopener noreferrer">Check GitHub Releases ↗</a></article>';
      return '<article class="platform-card"><div class="platform-head"><span class="platform-symbol">' + p.symbol + '</span><span class="release-badge latest">' + (selectedRelease.id === latestStable(releases) ? 'LATEST STABLE' : 'STABLE') + '</span></div><h3>' + p.name + '</h3><p>' + p.detail + ' · ' + escapeHTML(selectedRelease.tag) + '</p><div class="platform-assets">' + selectedAssets.map(a => assetMarkup(a,true)).join('') + '</div><a class="text-link" href="' + escapeHTML(selectedRelease.url) + '" target="_blank" rel="noopener noreferrer">Release notes ↗</a></article>';
    }).join('');
  }
  function matchesRelease(release) {
    if (filterMode === 'stable' && release.prerelease) return false;
    if (filterMode === 'pre' && !release.prerelease) return false;
    if (!query) return true;
    const haystack = [release.tag, release.title, release.body, ...release.assets.map(a => a.name)].join(' ').toLowerCase();
    return haystack.includes(query);
  }
  function renderReleaseList() {
    if (!releaseList) return;
    const filtered = stableSorted(releases.filter(matchesRelease));
    if (!filtered.length) {
      releaseList.innerHTML = '<div class="release-status">No releases match this search and filter. Try another query or browse GitHub directly.</div>';
      loadMore.hidden = !hasMore || Boolean(query) || filterMode !== 'all';
      return;
    }
    const latest = latestStable(releases);
    releaseList.innerHTML = filtered.map((r, index) => {
      const badges = (r.id === latest ? '<span class="release-badge latest">LATEST STABLE</span>' : '') + (r.prerelease ? '<span class="release-badge pre">PRERELEASE</span>' : '<span class="release-badge">STABLE</span>');
      const assets = r.assets.length ? '<div class="asset-list">' + r.assets.map(a => assetMarkup(a,false)).join('') + '</div>' : '<p class="no-assets">This release has no downloadable assets attached.</p>';
      const notes = r.body.trim() ? escapeHTML(r.body) : 'No release notes were provided for this release.';
      return '<details class="release-card"' + (index === 0 ? ' open' : '') + '><summary class="release-summary"><span class="release-version">' + escapeHTML(r.tag) + '</span><div><div class="release-title">' + escapeHTML(r.title) + '</div><div class="release-date">' + escapeHTML(formatDate(r.date)) + '</div></div><span class="release-labels">' + badges + '</span><span class="release-chevron" aria-hidden="true">⌄</span></summary><div class="release-detail"><div class="release-actions"><a class="small-link" href="' + escapeHTML(r.url) + '" target="_blank" rel="noopener noreferrer">Full release notes ↗</a></div><p class="release-body">' + notes + '</p><h3 class="asset-section-title">Available assets (' + r.assets.length + ')</h3>' + assets + '</div></details>';
    }).join('');
    loadMore.hidden = !hasMore || Boolean(query) || filterMode !== 'all';
  }
  function renderAll() { renderPlatformCards(); renderReleaseList(); }

  async function fetchPage(pageNumber) {
    const response = await fetch(API + '?per_page=100&page=' + pageNumber, {headers:{'Accept':'application/vnd.github+json'}, cache:'no-store'});
    if (!response.ok) {
      const error = new Error(response.status === 403 || response.status === 429 ? 'GitHub API rate limit reached.' : 'GitHub release API returned HTTP ' + response.status + '.');
      error.status = response.status;
      throw error;
    }
    const data = await response.json();
    if (!Array.isArray(data)) throw new Error('GitHub returned an unexpected release response.');
    return data.map(normalize).filter(validPublicRelease);
  }
  function saveCache() {
    try { sessionStorage.setItem(CACHE_KEY, JSON.stringify({at:Date.now(),releases})); } catch (_) {}
  }
  function readCache() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(CACHE_KEY) || 'null');
      if (saved && Array.isArray(saved.releases)) return {fresh:Date.now() - saved.at < CACHE_TTL, data:saved.releases.map(normalize).filter(validPublicRelease)};
    } catch (_) {}
    return null;
  }
  async function load({reset = false, force = false} = {}) {
    if (loading) return;
    loading = true;
    if (reset) { page = 1; hasMore = true; releases = []; }
    const cached = readCache();
    if (!force && reset && cached?.fresh && cached.data.length) {
      releases = cached.data; hasMore = false; cacheUsed = true;
      setStatus('Showing recently cached public release data. Refresh to check GitHub again.', 'success');
      renderAll(); loading = false; return;
    }
    if (!releases.length) setStatus('Loading release information from GitHub…');
    try {
      const batch = await fetchPage(page);
      if (reset) releases = batch; else releases = releases.concat(batch.filter(item => !releases.some(existing => existing.id === item.id)));
      hasMore = batch.length === 100;
      page += 1;
      saveCache();
      cacheUsed = false;
      setStatus('Loaded ' + releases.length + ' public release' + (releases.length === 1 ? '' : 's') + ' from GitHub.', 'success');
      renderAll();
    } catch (error) {
      if (!releases.length && cached?.data.length) {
        releases = cached.data; hasMore = false; cacheUsed = true;
        setStatus(error.message + ' Showing cached release data instead.', 'error');
        renderAll();
      } else if (!releases.length) {
        platformRoot.innerHTML = '<div class="release-status error">Release data is temporarily unavailable. Use GitHub Releases to choose an actual package.</div>';
        setStatus(error.message + ' No cached release data is available.', 'error');
        releaseList.innerHTML = '<div class="release-status">We could not load the release list. Please retry or open GitHub Releases directly.</div>';
      } else {
        setStatus(error.message + ' Showing the releases already loaded.', 'error');
        renderAll();
      }
    } finally {
      loading = false;
    }
  }
  let searchTimer;
  search?.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { query = search.value.trim().toLowerCase(); renderReleaseList(); }, 100);
  });
  filter?.addEventListener('change', () => { filterMode = filter.value; renderReleaseList(); });
  loadMore?.addEventListener('click', async () => {
    if (!hasMore || loading) return;
    await load();
  });
  $('refresh-releases')?.addEventListener('click', () => { cacheUsed = false; load({reset:true,force:true}); });
  load({reset:true});
})();