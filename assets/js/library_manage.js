/* Local Library file management: add, crop, rename, move, copy, delete, restore.
 * Builds on library.js (lib, $, el, loadLibrary, startLibraryJob, openActionDialog).
 * All names from disk are inserted as text, never as HTML. */

const mainFolderLabel = name => name || 'Main folder';
const baseName = path => path.split('/').pop();

/* The server's explanation of a failed request, without the JSON wrapping. */
function errorText(e) {
  try { return JSON.parse(e.message).detail || e.message; } catch { return e.message; }
}

/* Show a dialog; onConfirm runs after it closes. */
function openLibraryForm(title, nodes, confirmLabel, onConfirm, { danger = false } = {}) {
  const confirm = $('libraryDialogConfirm');
  $('libraryDialogTitle').textContent = title;
  $('libraryDialogBody').replaceChildren(...nodes);
  confirm.hidden = !onConfirm;
  confirm.textContent = confirmLabel || '';
  confirm.disabled = false;
  confirm.className = danger ? 'danger' : 'primary';
  confirm.onclick = () => {
    const act = onConfirm();
    if (act === false) return;  // the form isn't ready yet
    closeLibraryDialog();
    if (act) act();
  };
  $('libraryDialog').classList.add('active');
}

/* Make a change to the library, then re-read it. Returns the reply, or null. */
async function changeLibrary(path, body, done, method = 'POST') {
  let reply = null;
  try {
    reply = await api(path, { method, body, timeout: 300000 });
    if (done) toast(done, 'success');
  } catch (e) {
    toast(errorText(e), 'error', 8000);
  }
  lib.selected.clear();
  await loadLibrary();
  return reply;
}

// --- Toolbar state ---

function renderLibraryManage() {
  const count = lib.selected.size;
  const ready = Boolean(lib.info?.available) && !lib.running;
  const set = (id, enabled, title) => { $(id).disabled = !enabled; $(id).title = title; };
  const destination = mainFolderLabel(lib.collection);

  set('libraryAdd', ready, `Copy images from this computer into “${destination}”. You can also drag them onto the page.`);
  set('libraryRename', ready && count === 1, count === 1 ? 'Rename this file' : 'Select exactly one image to rename it.');
  set('libraryCrop', ready && count === 1, count === 1 ? 'Cut this picture to the TV’s 16:9 shape' : 'Select exactly one image to crop it.');
  set('libraryMove', ready && count > 0, 'Move the selected files to another collection');
  set('libraryCopy', ready && count > 0, 'Copy the selected files into another collection');
  set('libraryDelete', ready && count > 0, 'Delete the selected files from your library');

  const stale = [...lib.selected].filter(p => lib.outdated[p]);
  $('libraryUpdate').hidden = !stale.length;
  set('libraryUpdate', ready && stale.length > 0, 'Replace the old version on the TV with the cropped one');

  set('libraryNewCollection', ready, 'Create a new sub-folder');
  $('libraryRenameCollection').hidden = $('libraryDeleteCollection').hidden = !lib.collection;
  set('libraryRenameCollection', ready, `Rename the folder “${lib.collection}”`);
  set('libraryDeleteCollection', ready, `Delete “${lib.collection}” and the images in it`);
  set('libraryRestore', ready, 'Bring back images saved before a crop or delete');
  $('libraryBackups').checked = lib.info?.backups ?? true;
  $('libraryBackups').disabled = !ready;
}

// --- Adding ---

async function addLibraryFiles(fileList) {
  const files = [...fileList];
  if (!files.length || !lib.info?.available || lib.running) return;
  const collection = lib.collection || '';
  const added = [], failed = [];
  for (const [index, file] of files.entries()) {
    setLibraryStatus(`Adding ${index + 1} of ${files.length} to ${mainFolderLabel(collection)}…`);
    const form = new FormData();
    form.append('collection', collection);
    form.append('files', file);
    try {
      const reply = await api('/library/files', { method: 'POST', body: form, timeout: 300000 });
      added.push(...reply.added);
      failed.push(...reply.errors.map(x => `${x.name}: ${x.error}`));
    } catch (e) {
      failed.push(`${file.name}: ${errorText(e)}`);
    }
  }
  await loadLibrary();
  setLibraryStatus(`Added ${added.length} image(s).` + (failed.length ? ` Not added — ${failed.join('; ')}` : ''));
}

// --- Rename, move, copy ---

function nameField(value) {
  return el('input', { type: 'text', id: 'libraryName', value, maxLength: 150 });
}

function renameSelected() {
  const [path] = lib.selected;
  const file = baseName(path);
  const input = nameField(file.slice(0, file.lastIndexOf('.')));
  openLibraryForm('Rename Image', [el('p', { textContent: file }), input], 'Rename', () => {
    const name = input.value.trim();
    return name ? () => changeLibrary('/library/files/rename', { path, name }, 'Renamed') : false;
  });
  input.focus();
}

function transferSelected(kind) {
  const paths = [...lib.selected];
  const verb = kind === 'move' ? 'Move' : 'Copy';
  const names = ['', ...lib.info.collections.map(c => c.name).filter(Boolean)];
  const select = el('select', { id: 'libraryDestination' },
    ...names.map(name => new Option(mainFolderLabel(name), name)));
  openLibraryForm(`${verb} Images`,
    [el('p', { textContent: `${verb} ${paths.length} image(s) to:` }), select,
     el('p', { textContent: 'A file with the same name is kept; the new one gets a number.' })],
    verb, () => () => changeLibrary(`/library/files/${kind}`, { paths, collection: select.value },
      kind === 'move' ? 'Moved' : 'Copied'));
}

// --- Deleting ---

function backupNote() {
  return el('p', {
    textContent: lib.info.backups
      ? 'A copy is kept in the .backups folder, and you can restore it.'
      : 'Backups are off. This cannot be undone.',
  });
}

/* Ask about the TV copies, remove them if wanted, and only then delete locally. */
function confirmDelete(title, question, paths, remove) {
  const onTv = paths.filter(p => lib.onTv[p] || lib.outdated[p]);
  const box = el('input', { type: 'checkbox' });
  const nodes = [el('p', { textContent: question }), backupNote()];
  if (onTv.length) nodes.push(el('label', {}, box, ` Also remove ${onTv.length} image(s) from the TV`));
  openLibraryForm(title, nodes, 'Delete', () => () => {
    if (!box.checked) return remove();
    lib.afterJob = remove;
    return startLibraryJob('unload', { paths: onTv });
  }, { danger: true });
}

function deleteSelectedFiles() {
  const paths = [...lib.selected];
  confirmDelete('Delete Images', `Delete ${paths.length} image(s) from your library?`, paths,
    () => changeLibrary('/library/files/delete', { paths }, 'Deleted'));
}

// --- Collections ---

function newCollection() {
  const input = nameField('');
  openLibraryForm('New Collection', [input], 'Create', () => {
    const name = input.value.trim();
    if (!name) return false;
    return async () => {
      if (await changeLibrary('/library/collections', { name }, 'Collection created')) openCollection(name);
    };
  });
  input.focus();
}

function openCollection(name) {
  lib.collection = name;
  lib.selected.clear();
  renderLibraryCollections();
  loadLibraryImages();
}

function renameCollection() {
  const name = lib.collection;
  const input = nameField(name);
  openLibraryForm('Rename Collection', [input], 'Rename', () => {
    const wanted = input.value.trim();
    if (!wanted) return false;
    return async () => {
      const reply = await changeLibrary('/library/collections', { name, new_name: wanted }, 'Renamed', 'PUT');
      if (!reply) return;
      await loadCollections();
      openCollection(reply.name);
    };
  });
  input.focus();
}

function deleteCollection() {
  const name = lib.collection;
  const paths = lib.images.map(i => i.path);
  confirmDelete('Delete Collection', `Delete “${name}” and its ${paths.length} image(s)?`, paths, async () => {
    const reply = await changeLibrary('/library/collections/delete', { name }, 'Collection deleted');
    if (reply && !reply.folder_removed) {
      setLibraryStatus(`The images were deleted. The folder “${name}” was kept because it holds other files.`);
    }
  });
}

// --- Cropping ---

function cropSelected() {
  const [path] = lib.selected;
  const wasOnTv = Boolean(lib.onTv[path] || lib.outdated[path]);
  const image = el('img', { alt: baseName(path), draggable: false });
  const frame = el('div', { className: 'crop-frame', title: 'Drag to move' });
  const stage = el('div', { className: 'crop-stage' }, image, frame);
  const size = el('input', { type: 'range', min: 20, max: 100, value: 100, id: 'libraryCropSize' });
  // The box, as fractions of the picture's width and height.
  const box = { left: 0, top: 0, width: 1 };
  let widest = 0;
  const height = () => box.width * image.naturalWidth / (16 / 9) / image.naturalHeight;

  function draw() {
    box.width = widest * size.value / 100;
    box.left = Math.min(Math.max(box.left, 0), 1 - box.width);
    box.top = Math.min(Math.max(box.top, 0), Math.max(0, 1 - height()));
    Object.assign(frame.style, {
      left: `${box.left * 100}%`, top: `${box.top * 100}%`,
      width: `${box.width * 100}%`, height: `${height() * 100}%`,
    });
  }
  image.onload = () => {
    widest = Math.min(1, image.naturalHeight * (16 / 9) / image.naturalWidth);
    box.width = widest;
    box.left = (1 - box.width) / 2;
    box.top = (1 - height()) / 2;
    draw();
  };
  image.src = `/api/library/preview?path=${encodeURIComponent(path)}&v=${lib.version}`;
  size.oninput = () => {
    if (!widest) return;
    // Resize around the centre.
    const centre = { x: box.left + box.width / 2, y: box.top + height() / 2 };
    box.width = widest * size.value / 100;
    box.left = centre.x - box.width / 2;
    box.top = centre.y - height() / 2;
    draw();
  };
  frame.onpointerdown = start => {
    start.preventDefault();
    frame.setPointerCapture(start.pointerId);
    const from = { x: start.clientX, y: start.clientY, left: box.left, top: box.top };
    frame.onpointermove = move => {
      box.left = from.left + (move.clientX - from.x) / image.clientWidth;
      box.top = from.top + (move.clientY - from.y) / image.clientHeight;
      draw();
    };
    frame.onpointerup = frame.onpointercancel = () => { frame.onpointermove = null; };
  };

  openLibraryForm('Crop Image',
    [stage, el('label', { htmlFor: size.id, textContent: 'Size' }), size, backupNote()], 'Crop', () => {
      if (!widest) return false;
      return async () => {
        const reply = await changeLibrary('/library/crop', { path, ...box }, 'Cropped');
        if (reply && wasOnTv) offerTvUpdate(path);
      };
    });
}

function offerTvUpdate(path) {
  openLibraryForm('Update the TV?',
    [el('p', { textContent: 'The TV still shows the picture as it was before cropping. You can also do this later with “Update TV copy”.' })],
    'Choose Matte and Update', () => () => {
      lib.selected = new Set([path]);
      openActionDialog('update');
    });
}

// --- Backups ---

async function openRestore() {
  let backups;
  try {
    ({ backups } = await api('/library/backups', { timeout: 120000 }));
  } catch (e) {
    toast(errorText(e), 'error', 8000);
    return;
  }
  const rows = backups.map(entry => {
    const when = entry.saved.replace(/(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)\d\d/, '$1-$2-$3 $4:$5');
    return el('div', { className: 'restore-row' },
      el('img', { loading: 'lazy', alt: '', src: '/api/library/backups/thumb?id=' + encodeURIComponent(entry.id) }),
      el('span', { textContent: `${entry.name} — ${mainFolderLabel(entry.collection)}, saved ${when}` }),
      el('button', {
        type: 'button', textContent: 'Restore', title: 'Copy this back into its collection. Nothing is overwritten.',
        onclick: () => {
          closeLibraryDialog();
          changeLibrary('/library/backups/restore', { id: entry.id }, 'Restored');
        },
      }));
  });
  openLibraryForm('Restore From Backup',
    rows.length ? [el('div', { className: 'restore-list' }, ...rows)] : [el('p', { textContent: 'There are no backups yet.' })],
    null, null);
}

async function setBackups(on) {
  await changeLibrary('/library/options', { backups: on }, on ? 'Backups on' : 'Backups off', 'PUT');
}

// --- Drag and drop ---

(function acceptDrops() {
  const view = $('libraryView');
  const hasFiles = e => [...e.dataTransfer.types].includes('Files');
  view.addEventListener('dragover', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    view.classList.add('dragover');
  });
  view.addEventListener('dragleave', e => {
    if (!view.contains(e.relatedTarget)) view.classList.remove('dragover');
  });
  view.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    view.classList.remove('dragover');
    addLibraryFiles(e.dataTransfer.files);
  });
  $('libraryFileInput').addEventListener('change', async event => {
    await addLibraryFiles(event.target.files);
    event.target.value = '';
  });
})();

// library.js may already have drawn the view before this script loaded.
renderLibraryManage();
