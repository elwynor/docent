/* Local Library view: browse a folder of images and move them to and from the Frame.
 * Uses api(), toast(), thumbCache, loadCollections() and refreshArt() from index.html.
 * All names from disk are inserted as text, never as HTML. */

const LIBRARY_INTERVAL_LABELS = { 3: '3 minutes', 15: '15 minutes', 60: '1 hour', 720: '12 hours', 1440: '1 day' };

const lib = {
  info: null,
  collection: null,   // null = all images, '' = main folder, otherwise a folder name
  images: [],
  onTv: {},
  selected: new Set(),
  running: false,
  loadToken: 0,
  folder: null,       // folder currently shown in the picker
};

const $ = id => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function libraryViewActive() {
  return !$('libraryView').hidden;
}

function setView(view) {
  const local = view === 'library';
  $('tvView').hidden = local;
  $('libraryView').hidden = !local;
  $('jumpToCurrentFab').hidden = local;
  $('viewTv').setAttribute('aria-pressed', String(!local));
  $('viewLibrary').setAttribute('aria-pressed', String(local));
  try { localStorage.setItem('docentView', view); } catch (e) { console.debug('view not saved:', e); }
  if (local) loadLibrary();
}

function setLibraryStatus(text) {
  $('libraryStatus').textContent = text;
}

// --- Loading ---

async function loadLibrary() {
  try {
    lib.info = await api('/library', { timeout: 120000 });
  } catch (e) {
    // The page is read from disk on each visit, but the server only on start-up.
    setLibraryStatus(e.message.includes('"Not Found"')
      ? 'Docent has been updated since it was started. Quit it and start it again.'
      : 'Could not load the library: ' + e.message);
    return;
  }
  const { configured, available, root, collections } = lib.info;
  $('libraryFolder').textContent = configured ? root : 'No folder chosen';
  if (lib.collection && !collections.some(c => c.name === lib.collection)) lib.collection = null;
  renderLibraryCollections();
  if (!available) {
    lib.images = [];
    lib.selected.clear();
    renderLibraryGrid();
    setLibraryStatus(configured
      ? 'This folder cannot be reached. If it is on a network drive, check that it is connected.'
      : 'Choose the folder that holds your images. Sub-folders become collections.');
    return;
  }
  setLibraryStatus('');
  await loadLibraryImages();
  const job = await api('/library/status').catch(e => { console.debug('status failed:', e); return null; });
  if (job && job.state === 'running') pollLibraryJob();
  else refreshOnTv();
}

async function loadLibraryImages() {
  const token = ++lib.loadToken;
  const query = lib.collection === null ? '' : '?collection=' + encodeURIComponent(lib.collection);
  try {
    const data = await api('/library/images' + query, { timeout: 120000 });
    if (token !== lib.loadToken) return;
    lib.images = data.images;
  } catch (e) {
    if (token !== lib.loadToken) return;
    lib.images = [];
    setLibraryStatus('Could not list images: ' + e.message);
  }
  lib.selected = new Set(lib.images.map(i => i.path).filter(p => lib.selected.has(p)));
  renderLibraryGrid();
}

async function refreshOnTv() {
  if (lib.running || !lib.info?.available) return;
  $('libraryTvState').textContent = 'Checking what is on the TV…';
  try {
    const data = await api('/library/tv', { timeout: 300000 });
    lib.onTv = data.on_tv;
    $('libraryTvState').textContent = '';
  } catch (e) {
    console.debug('on-TV check failed:', e);
    lib.onTv = {};
    $('libraryTvState').textContent = 'Could not check the TV, so “On TV” badges are hidden.';
  }
  renderLibraryGrid();
}

// --- Rendering ---

function renderLibraryCollections() {
  const entries = [{ name: null, label: 'All images', count: lib.info.total }]
    .concat(lib.info.collections.map(c => ({ name: c.name, label: c.name || 'Main folder', count: c.count })));
  $('libraryCollections').replaceChildren(...(lib.info.available ? entries : []).map(entry => {
    const button = el('button', { type: 'button', className: 'library-collection' },
      el('span', { textContent: entry.label }), el('span', { textContent: String(entry.count) }));
    button.setAttribute('aria-pressed', String(entry.name === lib.collection));
    button.onclick = () => {
      lib.collection = entry.name;
      lib.selected.clear();
      renderLibraryCollections();
      loadLibraryImages();
    };
    return button;
  }));
}

function libraryTitle() {
  if (lib.collection === null) return 'All images';
  return lib.collection || 'Main folder';
}

function renderLibraryGrid() {
  $('libraryGrid').replaceChildren(...lib.images.map(image => {
    const card = el('button', { type: 'button', className: 'library-card', title: image.path },
      el('img', { loading: 'lazy', alt: '', src: '/api/library/thumb?path=' + encodeURIComponent(image.path) }),
      el('span', { className: 'library-card-name', textContent: image.name }));
    if (lib.onTv[image.path]) card.append(el('span', { className: 'library-badge', textContent: 'On TV' }));
    card.setAttribute('aria-pressed', String(lib.selected.has(image.path)));
    card.onclick = () => {
      if (lib.selected.has(image.path)) lib.selected.delete(image.path);
      else lib.selected.add(image.path);
      card.setAttribute('aria-pressed', String(lib.selected.has(image.path)));
      renderLibraryActions();
    };
    return card;
  }));
  renderLibraryActions();
}

function renderLibraryActions() {
  const count = lib.selected.size;
  const what = count ? `${count} selected` : (lib.collection ? 'collection' : 'all');
  $('libraryTitle').textContent = `${libraryTitle()} (${lib.images.length})`;
  $('libraryLoad').textContent = `Load ${what}`;
  $('libraryUnload').textContent = `Unload ${what}`;
  const off = lib.running || !lib.images.length;
  ['libraryLoad', 'libraryUnload', 'libraryReplace'].forEach(id => { $(id).disabled = off; });
  // Hover text says exactly which images each action will affect.
  const images = count ? `the ${count} selected image(s)`
    : (lib.collection ? `every image in “${lib.collection}”` : 'every image listed here');
  $('libraryLoad').title = `Copy ${images} to the TV. Nothing already on the TV is removed.`;
  $('libraryUnload').title = `Remove ${images} from the TV. The files on your disk are not deleted.`;
  $('libraryReplace').title = `Make the TV hold only ${images}, removing other pictures, then start a slideshow. You can review the changes first.`;
  $('libraryClear').hidden = !count;
  $('libraryChooseFolder').disabled = lib.running;
}

function clearLibrarySelection() {
  lib.selected.clear();
  renderLibraryGrid();
}

function selectAllLibrary() {
  lib.images.forEach(i => lib.selected.add(i.path));
  renderLibraryGrid();
}

// --- Actions ---

/* The images an action applies to: the selection, else the open collection. */
function libraryTarget() {
  if (lib.selected.size) return { paths: [...lib.selected] };
  if (lib.collection) return { collection: lib.collection };
  return { paths: lib.images.map(i => i.path) };
}

function describeTarget() {
  if (lib.selected.size) return `${lib.selected.size} selected image(s)`;
  return `${libraryTitle()} (${lib.images.length} images)`;
}

function openActionDialog(action) {
  const titles = { load: 'Load onto the Frame', unload: 'Unload from the Frame', show: 'Replace TV Contents' };
  const target = libraryTarget();
  const body = $('libraryDialogBody');
  const confirm = $('libraryDialogConfirm');
  $('libraryDialogTitle').textContent = titles[action];
  confirm.textContent = titles[action];
  confirm.disabled = false;
  body.replaceChildren(el('p', { textContent: describeTarget() }));

  const form = el('div', { className: 'library-form' });
  const field = (label, control) => form.append(el('label', { htmlFor: control.id, textContent: label }), control);
  let matte, interval, shuffle, unknownBox, unknownIds = [];

  if (action !== 'unload') {
    matte = el('select', { id: 'libraryMatte' },
      ...Array.from($('uploadMatte').options, o => new Option(o.text, o.value)));
    const saved = lib.info.collections.find(c => c.name === target.collection)?.matte;
    if (saved) matte.value = saved;
    if (matte.selectedIndex === -1) matte.selectedIndex = 0;
  }
  if (action === 'show') {
    interval = el('select', { id: 'libraryInterval' },
      ...lib.info.intervals.map(m => new Option(LIBRARY_INTERVAL_LABELS[m] || `${m} minutes`, m, m === 15, m === 15)));
    shuffle = el('input', { id: 'libraryShuffle', type: 'checkbox', checked: true });
    field('Change every', interval);
    field('Shuffle', shuffle);
  }
  if (matte) {
    field('Matte for uploads', matte);
    body.append(form);
  }

  if (action === 'load') {
    body.append(el('p', { textContent: 'Images already on the TV are skipped. Nothing is removed.' }));
  } else if (action === 'unload') {
    body.append(el('p', { textContent: 'These images are removed from the TV. The files in your library are not changed.' }));
  } else {
    const summary = el('div', { className: 'library-summary', textContent: 'Comparing with your TV. This can take a minute the first time.' });
    summary.setAttribute('role', 'status');
    body.append(summary);
    confirm.disabled = true;
    api('/library/plan', { method: 'POST', body: target, timeout: 300000 }).then(plan => {
      if (!summary.isConnected) return;  // dialog was closed or reused
      summary.textContent = `Upload ${plan.upload.length}, keep ${plan.keep} already on the TV, ` +
        `remove ${plan.delete.length} that have a copy in your library. Then start the slideshow.`;
      unknownIds = plan.unknown;
      if (unknownIds.length) {
        unknownBox = el('input', { type: 'checkbox' });
        summary.append(
          el('p', { textContent: `${unknownIds.length} image(s) on the TV have no copy in your library. They stay on the TV, and appear in the slideshow, unless you tick the box. Deleting them cannot be undone.` }),
          el('div', { className: 'library-unknown' },
            ...unknownIds.map(id => el('img', { alt: id, src: '/api/thumbnail/' + encodeURIComponent(id) }))),
          el('label', {}, unknownBox, ' Also delete these from the TV'));
      }
      confirm.disabled = false;
    }).catch(e => {
      summary.textContent = 'Could not compare: ' + e.message;
    });
  }

  confirm.onclick = () => {
    const request = { ...target };
    if (matte) request.matte = matte.value || 'none';
    if (action === 'show') {
      request.interval = Number(interval.value);
      request.shuffle = shuffle.checked;
      request.delete_unknown = unknownBox?.checked ? unknownIds : [];
    }
    closeLibraryDialog();
    startLibraryJob(action, request);
  };
  $('libraryDialog').classList.add('active');
}

function closeLibraryDialog() {
  $('libraryDialog').classList.remove('active');
  $('libraryDialogBody').replaceChildren();
}

async function startLibraryJob(action, request) {
  lib.running = true;
  renderLibraryActions();
  setLibraryStatus('Starting…');
  try {
    await api('/library/' + action, { method: 'POST', body: request, timeout: 120000 });
  } catch (e) {
    lib.running = false;
    renderLibraryActions();
    setLibraryStatus('Could not start: ' + e.message);
    return;
  }
  pollLibraryJob();
}

function pollLibraryJob() {
  lib.running = true;
  renderLibraryActions();
  setTimeout(async () => {
    let job;
    try {
      job = await api('/library/status');
    } catch (e) {
      console.debug('library status failed:', e);
      pollLibraryJob();
      return;
    }
    if (job.state === 'running') {
      const count = job.total ? ` ${job.done} of ${job.total}` : '';
      setLibraryStatus(`${job.step}${count}… Keep Docent running and this computer awake.`);
      pollLibraryJob();
      return;
    }
    lib.running = false;
    lib.selected.clear();
    setLibraryStatus(job.message || '');
    // The TV can reuse IDs, so forget thumbnails cached in the browser.
    Object.keys(thumbCache).forEach(id => delete thumbCache[id]);
    await loadCollections();
    await refreshArt();
    toast(job.message, job.state === 'done' ? 'success' : 'error', 6000);
    renderLibraryGrid();
    refreshOnTv();
  }, 1500);
}

// --- Folder picker ---

/* Use the computer's own folder dialog when the browser is on the same machine
 * as Docent; otherwise fall back to the in-page folder list. */
async function openFolderPicker() {
  if (!lib.info) { await loadLibrary(); if (!lib.info) return; }
  if (lib.info?.native_picker) {
    const button = $('libraryChooseFolder');
    button.disabled = true;
    setLibraryStatus('Choose a folder in the dialog that just opened. It may be behind this window.');
    try {
      const data = await api('/library/pick', { method: 'POST', body: {}, timeout: 620000 });
      setLibraryStatus('');
      if (!data.cancelled) await showNewFolder();
      return;
    } catch (e) {
      console.debug('folder dialog failed:', e);
      setLibraryStatus('');
    } finally {
      button.disabled = lib.running;
    }
  }
  $('folderDialog').classList.add('active');
  await browseFolder(lib.info?.available ? lib.info.root : '');
}

function closeFolderPicker() {
  $('folderDialog').classList.remove('active');
}

async function browseFolder(path) {
  let data;
  try {
    data = await api('/library/browse?path=' + encodeURIComponent(path), { timeout: 60000 });
  } catch (e) {
    toast('Cannot open that folder', 'error');
    if (path) await browseFolder('');
    return;
  }
  lib.folder = data.path;
  $('folderPath').textContent = data.path;
  $('folderUp').disabled = !data.parent;
  $('folderUp').onclick = () => browseFolder(data.parent);
  $('folderPlaces').replaceChildren(...data.places.map(place =>
    el('button', { type: 'button', textContent: place, onclick: () => browseFolder(place) })));
  $('folderList').replaceChildren(...data.folders.map(name =>
    el('button', { type: 'button', className: 'library-collection', textContent: name,
      onclick: () => browseFolder(`${data.path.replace(/\/$/, '')}/${name}`) })));
  if (!data.folders.length) $('folderList').append(el('p', { textContent: 'No sub-folders here.' }));
}

async function useFolder() {
  try {
    await api('/library/settings', { method: 'PUT', body: { root: lib.folder }, timeout: 120000 });
  } catch (e) {
    toast('Could not use that folder: ' + e.message, 'error', 6000);
    return;
  }
  closeFolderPicker();
  await showNewFolder();
}

async function showNewFolder() {
  lib.collection = null;
  lib.selected.clear();
  lib.onTv = {};
  await loadLibrary();
}

(function restoreView() {
  let saved = null;
  try { saved = localStorage.getItem('docentView'); } catch (e) { console.debug('view not restored:', e); }
  if (saved === 'library') setView('library');
})();
