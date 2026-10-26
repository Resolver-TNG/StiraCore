(function(){
  const listEl = document.getElementById('entry-list');
  const searchEl = document.getElementById('search');
  const tabs = document.querySelectorAll('.tab-btn');
  let layer = 'episodic';
  let query = '';
  let searchTimer = null;
  const expandedKeys = new Set();
  let lastKey = null; // fetchEntries() resolves to whether the entries changed (null: failed)

  function escapeHtml(str){
    return String(str).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  function truncate(str, len){
    str = String(str);
    return str.length > len ? str.slice(0, len) + '...' : str;
  }

  function renderEntries(entries){
    if(!entries.length){
      listEl.innerHTML = '<div class="empty">' + escapeHtml(t('memory.no_entries')) + '</div>';
      return;
    }
    listEl.innerHTML = entries.map(e => {
      const open = expandedKeys.has(e.key);
      const content = open ? escapeHtml(e.content) : escapeHtml(truncate(e.content, 120));
      const meta = e.metadata ? JSON.stringify(e.metadata) : '';
      return `<div class="entry ${open?'open':''}" data-key="${escapeHtml(e.key)}">
        <div class="entry-key">${escapeHtml(e.key)}</div>
        <div class="entry-content ${open?'expanded':''}">${content}</div>
        <div class="entry-meta">${escapeHtml(meta)}</div>
      </div>`;
    }).join('');
  }

  async function fetchEntries(){
    try{
      const params = new URLSearchParams({layer, limit: 50});
      if(query) params.set('q', query);
      const res = await fetch('/api/memory/browser?' + params.toString());
      // #601 E6: an error answer (e.g. 400 {"error": ...}) is a failure, not "no entries".
      if(!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if(!Array.isArray(data)) throw new Error('not a list');
      renderEntries(data);
      const key = JSON.stringify(data);
      const changed = key !== lastKey;
      lastKey = key;
      return changed;
    }catch(err){
      listEl.innerHTML = '<div class="empty">' + escapeHtml(t('memory.load_error')) + '</div>';
      return null;
    }
  }

  tabs.forEach(btn => {
    btn.addEventListener('click', () => {
      tabs.forEach(b => { b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); });
      btn.classList.add('active');
      btn.setAttribute('aria-pressed', 'true');
      layer = btn.dataset.layer;
      expandedKeys.clear();
      fetchEntries();
    });
  });

  searchEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      query = searchEl.value.trim();
      fetchEntries();
    }, 300);
  });

  listEl.addEventListener('click', (e) => {
    const entry = e.target.closest('.entry');
    if(!entry) return;
    const key = entry.dataset.key;
    if(expandedKeys.has(key)) expandedKeys.delete(key);
    else expandedKeys.add(key);
    fetchEntries();
  });

  // #369: not while the tab is hidden, slower while nothing changes, backed off on failure.
  if (typeof StiraPoll !== 'undefined') {
    StiraPoll.keep(fetchEntries, 10000, 30000, undefined, document);
  } else {
    fetchEntries();
    setInterval(fetchEntries, 10000);
  }
})();
