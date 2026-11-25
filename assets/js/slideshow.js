/* Slideshow bar, shared by both views. The TV runs the slideshow itself.
 * Uses api() and toast() from index.html, and el()/$ from library.js. */

const slideshow = { last: null, busy: false, retry: null };

function renderSlideshow(status) {
  const every = status?.running ? (LIBRARY_INTERVAL_LABELS[status.interval] || `${status.interval} minutes`) : '';
  $('slideshowState').textContent = !status ? 'Slideshow: unknown. Could not read it from the TV; trying again…'
    : status.running ? `Slideshow: running, every ${every}${status.shuffle ? ', shuffled' : ''}`
    : 'Slideshow: off';
  if (status) {
    const select = $('slideshowInterval');
    // Follow the TV only when it changes, so a choice being made isn't overwritten.
    const key = `${status.running}|${status.interval}|${status.shuffle}`;
    if (status.running && key !== slideshow.last) {
      select.value = status.interval;
      $('slideshowShuffle').checked = status.shuffle;
    }
    slideshow.last = key;
  }
  $('slideshowStart').textContent = status?.running ? 'Apply' : 'Start slideshow';
  // With the state unknown, leave both buttons usable.
  $('slideshowStart').disabled = slideshow.busy;
  $('slideshowStop').disabled = slideshow.busy || status?.running === false;
}

async function refreshSlideshow() {
  clearTimeout(slideshow.retry);
  if (slideshow.busy) return;
  try {
    renderSlideshow(await api('/slideshow', { timeout: 90000 }));
  } catch (e) {
    console.debug('slideshow status failed:', e);
    renderSlideshow(null);
    slideshow.retry = setTimeout(refreshSlideshow, 5000);
  }
}

async function setSlideshow(duration) {
  slideshow.busy = true;
  $('slideshowStart').disabled = $('slideshowStop').disabled = true;
  $('slideshowState').textContent = 'Slideshow: updating…';
  let status = null;
  try {
    status = await api('/slideshow', {
      method: 'POST', body: { duration, shuffle: $('slideshowShuffle').checked }, timeout: 90000,
    });
    toast(duration ? 'Slideshow started' : 'Slideshow stopped', 'success');
  } catch (e) {
    console.debug('slideshow change failed:', e);
    toast('Could not change the slideshow', 'error', 6000);
  }
  slideshow.busy = false;
  if (status) renderSlideshow(status);
  else await refreshSlideshow();
}

function startSlideshow() {
  setSlideshow(Number($('slideshowInterval').value));
}

refreshSlideshow();
// The slideshow can also be changed from the TV remote or a phone.
setInterval(() => { if (!document.hidden) refreshSlideshow(); }, 60000);
