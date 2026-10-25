(() => {
  const STORAGE_KEY = 'voiceink:settings';
  const VOCAB_STORAGE_KEY = 'voiceink:vocabulary';

  function loadLocalSettings() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return (parsed && typeof parsed === 'object') ? parsed : null;
    } catch (e) {
      return null;
    }
  }

  function saveLocalSettings(settings) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch (e) {
      console.error('Could not save settings locally', e);
    }
  }

  function loadLocalVocabulary() {
    try {
      const raw = localStorage.getItem(VOCAB_STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return (parsed && typeof parsed === 'object') ? parsed : null;
    } catch (e) {
      return null;
    }
  }

  function saveLocalVocabulary(vocabulary) {
    try {
      localStorage.setItem(VOCAB_STORAGE_KEY, JSON.stringify(vocabulary));
    } catch (e) {
      console.error('Could not save vocabulary locally', e);
    }
  }

  function currentSettingsPayload() {
    return {
      chars_per_minute: parseFloat(el.speedSlider.value),
      speech_rate_pct: parseInt(el.rateSlider.value, 10),
      voice: el.voiceSelect ? el.voiceSelect.value : undefined,
    };
  }

  const el = {
    inputArea: document.getElementById('input-area'),
    textDisplay: document.getElementById('text-display'),
    pasteBox: document.getElementById('paste-box'),
    btnLoadText: document.getElementById('btn-load-text'),
    filePicker: document.getElementById('file-picker'),
    loadError: document.getElementById('load-error'),
    sourceLabel: document.getElementById('source-label'),
    btnPlay: document.getElementById('btn-play'),
    btnPause: document.getElementById('btn-pause'),
    btnRestart: document.getElementById('btn-restart'),
    btnBack: document.getElementById('btn-back'),
    btnForward: document.getElementById('btn-forward'),
    seekSlider: document.getElementById('seek-slider'),
    progressLabel: document.getElementById('progress-label'),
    audio: document.getElementById('audio-player'),
    dropOverlay: document.getElementById('drop-overlay'),
    speedSlider: document.getElementById('speed-slider'),
    speedValue: document.getElementById('speed-value'),
    rateSlider: document.getElementById('rate-slider'),
    rateValue: document.getElementById('rate-value'),
    voiceSelect: document.getElementById('voice-select'),
    bufferDot: document.getElementById('buffer-dot'),
    bufferStatus: document.getElementById('buffer-status'),
    btnNew: document.getElementById('btn-new'),
    btnVocabulary: document.getElementById('btn-vocabulary'),
    vocabBackdrop: document.getElementById('vocab-backdrop'),
    vocabSidebar: document.getElementById('vocab-sidebar'),
    btnVocabClose: document.getElementById('btn-vocab-close'),
    btnVocabSave: document.getElementById('btn-vocab-save'),
    btnVocabReset: document.getElementById('btn-vocab-reset'),
    vocabSaveStatus: document.getElementById('vocab-save-status'),
    vocabHeadingTemplate: document.getElementById('vocab-heading-template'),
    inputLoadingOverlay: document.getElementById('input-loading-overlay'),
    inputLoadingText: document.getElementById('input-loading-text'),
  };

  (function applyStoredSettingsToForm() {
    const saved = loadLocalSettings();
    if (!saved) return;
    if (typeof saved.chars_per_minute === 'number') {
      el.speedSlider.value = saved.chars_per_minute;
      el.speedValue.textContent = Math.round(saved.chars_per_minute);
    }
    if (typeof saved.speech_rate_pct === 'number') {
      el.rateSlider.value = saved.speech_rate_pct;
      el.rateValue.textContent = `${saved.speech_rate_pct >= 0 ? '+' : ''}${saved.speech_rate_pct}%`;
    }
    if (saved.voice && el.voiceSelect) {
      el.voiceSelect.value = saved.voice;
    }
  })();

  let hasDoc = false;
  let totalChunks = 0;
  let unitsPerChunk = 6;
  let units = [];
  let clusterUnits = [];
  let unitToOrd = [];

  let chunkCache = {};
  let chunkInflight = {};
  let epoch = 0;

  let loadedChunk = -1;
  let loadedData = null;
  let currentUnit = 0;
  let isPlaying = false;
  let atEnd = false;
  let navigating = false;
  let navToken = 0;
  let scrubbing = false;
  let blockEls = [];
  let unitEls = [];
  let highlightedUnit = -1;
  let highlightedEls = [];

  function resetPlayback() {
    navToken++;
    el.audio.pause();
    el.audio.removeAttribute('src');
    el.audio.load();
    for (const d of Object.values(chunkCache)) URL.revokeObjectURL(d.url);
    chunkCache = {};
    chunkInflight = {};
    epoch++;
    loadedChunk = -1;
    loadedData = null;
    currentUnit = 0;
    isPlaying = false;
    atEnd = false;
    navigating = false;
    scrubbing = false;
    highlightedUnit = -1;
    highlightedEls = [];
    togglePlayButtons(false);
  }

  function buildClusterIndex() {
    clusterUnits = [];
    unitToOrd = new Array(units.length);
    let count = 0;
    for (let i = 0; i < units.length; i++) {
      if (units[i].kind === 'cluster') {
        unitToOrd[i] = count;
        clusterUnits.push(i);
        count++;
      } else {
        unitToOrd[i] = count;
      }
    }
    const last = Math.max(0, clusterUnits.length - 1);
    for (let i = 0; i < unitToOrd.length; i++) unitToOrd[i] = Math.min(unitToOrd[i], last);
  }

  async function startSessionFromResponse(resp) {
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({ error: 'Unknown error' }));
      showError(data.error || 'Failed to load document');
      return;
    }
    const data = await resp.json();
    resetPlayback();

    totalChunks = data.total_chunks;
    unitsPerChunk = data.units_per_chunk || 6;
    units = data.units;
    units.forEach((u, i) => { u.__index = i; });
    buildClusterIndex();
    hasDoc = true;

    el.sourceLabel.textContent = data.source_name;
    el.inputArea.style.display = 'none';
    el.textDisplay.style.display = 'block';
    renderBlocks(units);

    el.seekSlider.max = Math.max(0, clusterUnits.length - 1);
    el.seekSlider.value = 0;
    for (const b of [el.btnPlay, el.btnPause, el.btnRestart, el.btnBack, el.btnForward, el.seekSlider]) {
      b.disabled = false;
    }
    refreshPosition();
    setBufferStatus('idle');

    fetchChunk(0).catch(() => { });
  }

  function showError(msg) {
    el.loadError.textContent = msg;
  }

  function showInputLoading(message) {
    if (el.inputLoadingText) el.inputLoadingText.textContent = message || 'Reading document…';
    if (el.inputLoadingOverlay) el.inputLoadingOverlay.classList.add('active');

    el.btnLoadText.disabled = true;
    el.filePicker.disabled = true;
  }

  function hideInputLoading() {
    if (el.inputLoadingOverlay) el.inputLoadingOverlay.classList.remove('active');
    el.btnLoadText.disabled = false;
    el.filePicker.disabled = false;
  }

  async function closeDocument() {
    resetPlayback();
    hasDoc = false;
    totalChunks = 0;
    units = [];
    clusterUnits = [];
    unitToOrd = [];
    blockEls = [];
    unitEls = [];

    el.sourceLabel.textContent = '';
    el.textDisplay.style.display = 'none';
    el.textDisplay.innerHTML = '';
    el.inputArea.style.display = 'flex';
    el.pasteBox.value = '';
    el.filePicker.value = '';
    showError('');

    for (const b of [el.btnPlay, el.btnPause, el.btnRestart, el.btnBack, el.btnForward, el.seekSlider]) {
      b.disabled = true;
    }
    el.seekSlider.max = 0;
    el.seekSlider.value = 0;
    el.progressLabel.textContent = '—';
    setBufferStatus('idle');

    try {
      await fetch('/api/new', { method: 'POST' });
    } catch (e) {
      console.error(e);
    }
  }

  el.btnNew.addEventListener('click', closeDocument);

  function openVocabSidebar() {
    if (!el.vocabSidebar) return;
    el.vocabSidebar.classList.add('open');
    el.vocabSidebar.setAttribute('aria-hidden', 'false');
    if (el.vocabBackdrop) el.vocabBackdrop.classList.add('active');
  }

  function closeVocabSidebar() {
    if (!el.vocabSidebar) return;
    el.vocabSidebar.classList.remove('open');
    el.vocabSidebar.setAttribute('aria-hidden', 'true');
    if (el.vocabBackdrop) el.vocabBackdrop.classList.remove('active');
  }

  if (el.btnVocabulary) {
    el.btnVocabulary.addEventListener('click', openVocabSidebar);
  }
  if (el.btnVocabClose) {
    el.btnVocabClose.addEventListener('click', closeVocabSidebar);
  }
  if (el.vocabBackdrop) {
    el.vocabBackdrop.addEventListener('click', closeVocabSidebar);
  }
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && el.vocabSidebar && el.vocabSidebar.classList.contains('open')) {
      closeVocabSidebar();
    }
  });

  if (el.vocabSidebar) {
    el.vocabSidebar.querySelectorAll('.vocab-mute-checkbox').forEach((checkbox) => {
      checkbox.addEventListener('change', () => {
        const row = checkbox.closest('.vocab-punct-row');
        const nameInput = row ? row.querySelector('.vocab-punct-name') : null;
        if (nameInput) nameInput.disabled = checkbox.checked;
      });
    });
  }

  function setVocabStatus(message, kind) {
    if (!el.vocabSaveStatus) return;
    el.vocabSaveStatus.textContent = message;
    el.vocabSaveStatus.className = kind || '';
  }

  function collectVocabPayload() {
    const punctOverrides = {};
    if (el.vocabSidebar) {
      el.vocabSidebar.querySelectorAll('.vocab-punct-row').forEach((row) => {
        const symbol = row.dataset.symbol;
        const nameInput = row.querySelector('.vocab-punct-name');
        const muteCheckbox = row.querySelector('.vocab-mute-checkbox');
        if (!symbol || !nameInput) return;
        const defaultName = nameInput.placeholder;
        if (muteCheckbox && muteCheckbox.checked) {
          punctOverrides[symbol] = '';
        } else if (nameInput.value.trim() && nameInput.value.trim() !== defaultName) {
          punctOverrides[symbol] = nameInput.value.trim();
        }
      });
    }

    const cueOverrides = {};
    if (el.vocabSidebar) {
      el.vocabSidebar.querySelectorAll('.vocab-cue-row[data-key]').forEach((row) => {
        const key = row.dataset.key;
        const textInput = row.querySelector('.vocab-cue-text');
        if (!key || !textInput || key === 'heading_level_template') return;
        const defaultText = textInput.placeholder;
        if (textInput.value.trim() && textInput.value.trim() !== defaultText) {
          cueOverrides[key] = textInput.value.trim();
        }
      });
    }

    let headingLevelCueTemplate = null;
    if (el.vocabHeadingTemplate) {
      const val = el.vocabHeadingTemplate.value.trim();
      const defaultVal = el.vocabHeadingTemplate.placeholder;
      if (val && val !== defaultVal) {
        headingLevelCueTemplate = val;
      }
    }

    return {
      punct_overrides: punctOverrides,
      cue_overrides: cueOverrides,
      heading_level_cue_template: headingLevelCueTemplate,
    };
  }

  function applyVocabPayloadToForm(payload) {
    if (!el.vocabSidebar) return;
    (payload.punctuation || []).forEach((p) => {
      const row = el.vocabSidebar.querySelector(`.vocab-punct-row[data-symbol="${CSS.escape(p.symbol)}"]`);
      if (!row) return;
      const nameInput = row.querySelector('.vocab-punct-name');
      const muteCheckbox = row.querySelector('.vocab-mute-checkbox');
      if (nameInput) {
        nameInput.value = p.name;
        nameInput.placeholder = p.default_name;
        nameInput.disabled = !!p.muted;
      }
      if (muteCheckbox) muteCheckbox.checked = !!p.muted;
    });
    (payload.cues || []).forEach((c) => {
      const row = el.vocabSidebar.querySelector(`.vocab-cue-row[data-key="${CSS.escape(c.key)}"]`);
      if (!row) return;
      const textInput = row.querySelector('.vocab-cue-text');
      if (textInput) {
        textInput.value = c.text;
        textInput.placeholder = c.default_text;
      }
    });
    if (payload.heading_level_cue_template && el.vocabHeadingTemplate) {
      el.vocabHeadingTemplate.value = payload.heading_level_cue_template.value;
      el.vocabHeadingTemplate.placeholder = payload.heading_level_cue_template.default;
    }
  }

  if (el.btnVocabSave) {
    el.btnVocabSave.addEventListener('click', async () => {
      el.btnVocabSave.disabled = true;
      setVocabStatus('Saving…', '');
      try {
        const resp = await fetch('/api/vocabulary', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(collectVocabPayload()),
        });
        const data = await resp.json();
        if (!resp.ok) {
          setVocabStatus(data.error || 'Failed to save', 'error');
          return;
        }
        applyVocabPayloadToForm(data);
        saveLocalVocabulary(collectVocabPayload());
        setVocabStatus('Saved. Takes effect on the next document you load.', 'success');
      } catch (e) {
        console.error(e);
        setVocabStatus('Failed to save', 'error');
      } finally {
        el.btnVocabSave.disabled = false;
      }
    });
  }

  if (el.btnVocabReset) {
    el.btnVocabReset.addEventListener('click', async () => {
      el.btnVocabReset.disabled = true;
      setVocabStatus('Resetting…', '');
      try {
        const resp = await fetch('/api/vocabulary', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ punct_overrides: {}, cue_overrides: {}, heading_level_cue_template: null }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          setVocabStatus(data.error || 'Failed to reset', 'error');
          return;
        }
        applyVocabPayloadToForm(data);
        localStorage.removeItem(VOCAB_STORAGE_KEY);
        setVocabStatus('Reset to defaults.', 'success');
      } catch (e) {
        console.error(e);
        setVocabStatus('Failed to reset', 'error');
      } finally {
        el.btnVocabReset.disabled = false;
      }
    });
  }

  el.btnLoadText.addEventListener('click', async () => {
    const text = el.pasteBox.value;
    if (!text.trim()) {
      showError('Paste some text first.');
      return;
    }
    showError('');
    showInputLoading('Preparing narration…');
    try {
      const resp = await fetch('/api/load/text', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          settings: currentSettingsPayload(),
          vocabulary: loadLocalVocabulary() || {},
        }),
      });
      await startSessionFromResponse(resp);
    } finally {
      hideInputLoading();
    }
  });

  el.filePicker.addEventListener('change', async () => {
    if (!el.filePicker.files.length) return;
    await loadFile(el.filePicker.files[0]);
  });

  async function loadFile(file) {
    showError('');
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    showInputLoading(ext === 'pdf' ? 'Reading PDF, this can take a moment…' : 'Reading document…');
    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('settings', JSON.stringify(currentSettingsPayload()));
      formData.append('vocabulary', JSON.stringify(loadLocalVocabulary() || {}));
      const resp = await fetch('/api/load/file', { method: 'POST', body: formData });
      await startSessionFromResponse(resp);
    } finally {
      hideInputLoading();
    }
  }

  let dragCounter = 0;
  window.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragCounter++;
    el.dropOverlay.classList.add('active');
  });
  window.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
      dragCounter = 0;
      el.dropOverlay.classList.remove('active');
    }
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', async (e) => {
    e.preventDefault();
    dragCounter = 0;
    el.dropOverlay.classList.remove('active');
    if (e.dataTransfer.files.length) {
      await loadFile(e.dataTransfer.files[0]);
    }
  });

  function renderBlocks(unitList) {
    el.textDisplay.innerHTML = '';
    blockEls = [];
    unitEls = [];
    let currentBlockDiv = null;
    let currentBlockIndex = -1;

    for (const u of unitList) {
      if (u.kind === 'cue') continue;

      if (u.block_index !== currentBlockIndex) {
        currentBlockDiv = document.createElement('div');
        currentBlockDiv.className = 'block';
        if (u.block_type) currentBlockDiv.classList.add(u.block_type);
        if (u.block_type === 'heading' && u.block_level) {
          currentBlockDiv.classList.add(`h${u.block_level}`);
        }
        el.textDisplay.appendChild(currentBlockDiv);
        blockEls[u.block_index] = currentBlockDiv;
        currentBlockIndex = u.block_index;
      }
      if (currentBlockDiv) {
        const span = document.createElement('span');
        span.className = 'unit';
        span.textContent = (currentBlockDiv.childElementCount ? ' ' : '') + u.display_text;
        currentBlockDiv.appendChild(span);
        unitEls[u.__index] = span;
      }
    }
  }

  function highlightUnit(unitIdx) {
    if (unitIdx === highlightedUnit) return;
    highlightedUnit = unitIdx;
    for (const s of highlightedEls) s.classList.remove('active');
    highlightedEls = [];

    let i = unitIdx;
    while (i < units.length && !unitEls[i]) i++;

    const s = unitEls[i];
    if (!s) return;
    s.classList.add('active');
    highlightedEls.push(s);
    s.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function chunkOf(unitIdx) {
    return Math.floor(unitIdx / unitsPerChunk);
  }

  const CHUNK_RETRY_DELAYS_MS = [2000, 4000, 8000];

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function fetchChunk(index) {
    if (index < 0 || index >= totalChunks) return Promise.resolve(null);
    if (chunkCache[index]) return Promise.resolve(chunkCache[index]);
    if (chunkInflight[index]) return chunkInflight[index];

    const myEpoch = epoch;
    let failed = false;
    let p;
    setBufferStatus('buffering');

    const attemptOnce = async () => {
      const resp = await fetch(`/api/chunk/${index}`);
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({ error: 'Chunk failed' }));
        throw new Error(data.error || 'Chunk failed to generate');
      }
      const starts = (resp.headers.get('X-Unit-Starts') || '')
        .split(',').filter((s) => s !== '').map(Number);
      const blob = await resp.blob();
      return { url: URL.createObjectURL(blob), starts };
    };

    p = (async () => {
      let lastErr;
      for (let attempt = 0; attempt <= CHUNK_RETRY_DELAYS_MS.length; attempt++) {
        if (myEpoch !== epoch) throw new Error('stale');

        try {
          const data = await attemptOnce();
          if (myEpoch === epoch) chunkCache[index] = data;
          return data;
        } catch (err) {
          lastErr = err;
          const willRetry = attempt < CHUNK_RETRY_DELAYS_MS.length;
          console.error(err);
          if (willRetry) {
            const waitS = Math.round(CHUNK_RETRY_DELAYS_MS[attempt] / 1000);
            setBufferStatus('error', `${err.message || 'generation failed'} — retrying in ${waitS}s…`);
            await delay(CHUNK_RETRY_DELAYS_MS[attempt]);
          }
        }
      }
      failed = true;
      setBufferStatus('error', lastErr?.message || 'generation failed');
      throw lastErr;
    })();

    chunkInflight[index] = p;
    p.finally(() => {
      if (chunkInflight[index] === p) delete chunkInflight[index];
      if (Object.keys(chunkInflight).length) setBufferStatus('buffering');
      else if (!failed) setBufferStatus('ready');
    });
    return p;
  }

  function setBufferStatus(state, text) {
    el.bufferDot.className = `dot-${state}`;
    el.bufferStatus.textContent = text || state;
  }

  function whenMetadata() {
    if (el.audio.readyState >= 1) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        el.audio.removeEventListener('loadedmetadata', done);
        el.audio.removeEventListener('error', done);
        resolve();
      };
      el.audio.addEventListener('loadedmetadata', done);
      el.audio.addEventListener('error', done);
    });
  }

  function safePlay() {
    return el.audio.play().catch((e) => {
      if (e.name !== 'AbortError') console.error('Playback failed', e);
    });
  }

  function refreshPosition() {
    if (!units.length) return;
    const ord = unitToOrd[currentUnit] ?? 0;
    el.progressLabel.textContent = `${ord + 1} / ${clusterUnits.length}`;
    if (!scrubbing) el.seekSlider.value = ord;
    highlightUnit(currentUnit);
  }

  function snapToCueStart(idx) {
    while (idx > 0 && units[idx - 1].kind === 'cue') idx--;
    return idx;
  }

  async function seekToUnit(unitIdx) {
    if (!hasDoc || !units.length) return;
    unitIdx = Math.max(0, Math.min(units.length - 1, unitIdx));
    const token = ++navToken;
    navigating = true;
    atEnd = false;
    currentUnit = unitIdx;
    el.audio.pause();
    refreshPosition();

    const chunkIdx = chunkOf(unitIdx);
    let data;
    if (loadedChunk === chunkIdx && loadedData) {
      data = loadedData;
    } else {
      try {
        data = await fetchChunk(chunkIdx);
      } catch (e) {
        if (token === navToken) {
          navigating = false;
          isPlaying = false;
          togglePlayButtons(false);
        }
        return;
      }
      if (token !== navToken) return;
      el.audio.src = data.url;
      loadedChunk = chunkIdx;
      loadedData = data;
    }

    await whenMetadata();
    if (token !== navToken) return;

    const startMs = data.starts[unitIdx - chunkIdx * unitsPerChunk] || 0;
    el.audio.currentTime = startMs / 1000;
    navigating = false;

    if (isPlaying) await safePlay();

    fetchChunk(chunkIdx + 1).catch(() => { });
  }

  function seekToOrd(ord) {
    ord = Math.max(0, Math.min(clusterUnits.length - 1, ord));
    return seekToUnit(snapToCueStart(clusterUnits[ord]));
  }

  function jumpBy(delta) {
    if (!hasDoc) return;
    seekToOrd((unitToOrd[currentUnit] ?? 0) + delta);
  }

  setInterval(() => {
    if (navigating || !loadedData || loadedChunk < 0) return;
    const tMs = el.audio.currentTime * 1000 + 30;
    const starts = loadedData.starts;
    let i = 0;
    for (let k = 0; k < starts.length; k++) {
      if (starts[k] <= tMs) i = k; else break;
    }
    const u = loadedChunk * unitsPerChunk + i;
    if (u !== currentUnit) {
      currentUnit = u;
      refreshPosition();
    }
  }, 100);

  function onChunkEnded() {
    const next = (loadedChunk + 1) * unitsPerChunk;
    if (next >= units.length) {
      atEnd = true;
      isPlaying = false;
      togglePlayButtons(false);
      return;
    }
    seekToUnit(next);
  }

  el.audio.addEventListener('ended', () => {
    if (isPlaying) onChunkEnded();
  });

  async function play() {
    if (!hasDoc) return;
    isPlaying = true;
    togglePlayButtons(true);

    if (atEnd) { await seekToUnit(0); return; }
    if (navigating) return;
    if (loadedChunk >= 0 && loadedChunk === chunkOf(currentUnit)) {
      if (el.audio.ended) { onChunkEnded(); return; }
      await safePlay();
    } else {
      await seekToUnit(currentUnit);
    }
  }

  function pause() {
    isPlaying = false;
    el.audio.pause();
    togglePlayButtons(false);
  }

  function togglePlay() {
    if (isPlaying) pause(); else play();
  }

  el.btnPlay.addEventListener('click', play);
  el.btnPause.addEventListener('click', pause);
  el.btnRestart.addEventListener('click', () => {
    isPlaying = true;
    togglePlayButtons(true);
    seekToUnit(0);
  });
  el.btnBack.addEventListener('click', () => jumpBy(-1));
  el.btnForward.addEventListener('click', () => jumpBy(1));

  function togglePlayButtons(playing) {
    el.btnPlay.style.display = playing ? 'none' : 'flex';
    el.btnPause.style.display = playing ? 'flex' : 'none';
  }

  el.seekSlider.addEventListener('input', () => {
    scrubbing = true;
    const ord = Number(el.seekSlider.value);
    el.progressLabel.textContent = `${ord + 1} / ${clusterUnits.length}`;
    highlightUnit(clusterUnits[ord]);
  });
  el.seekSlider.addEventListener('change', () => {
    scrubbing = false;
    seekToOrd(Number(el.seekSlider.value));
    el.seekSlider.blur();
  });

  window.addEventListener('keydown', (e) => {
    if (!hasDoc) return;
    const tag = e.target.tagName;
    if (tag === 'TEXTAREA' || tag === 'INPUT') return;
    if (e.code === 'Space') {
      if (tag === 'BUTTON') return;
      e.preventDefault();
      togglePlay();
    } else if (e.code === 'ArrowLeft') {
      e.preventDefault();
      jumpBy(-1);
    } else if (e.code === 'ArrowRight') {
      e.preventDefault();
      jumpBy(1);
    }
  });

  async function pushConfig() {
    epoch++;
    chunkInflight = {};
    for (const idx of Object.keys(chunkCache)) {
      if (Number(idx) !== loadedChunk) {
        URL.revokeObjectURL(chunkCache[idx].url);
        delete chunkCache[idx];
      }
    }
    try {
      const payload = currentSettingsPayload();
      const resp = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({ error: 'Failed to update settings' }));
        console.error(data.error || 'Failed to update settings');
      } else {
        saveLocalSettings(payload);
      }
    } catch (e) {
      console.error(e);
    }
    if (hasDoc && loadedChunk >= 0) fetchChunk(loadedChunk + 1).catch(() => { });
  }

  el.speedSlider.addEventListener('input', () => {
    el.speedValue.textContent = el.speedSlider.value;
  });
  el.speedSlider.addEventListener('change', pushConfig);

  el.rateSlider.addEventListener('input', () => {
    const v = parseInt(el.rateSlider.value, 10);
    el.rateValue.textContent = `${v >= 0 ? '+' : ''}${v}%`;
  });
  el.rateSlider.addEventListener('change', pushConfig);

  if (el.voiceSelect) {
    el.voiceSelect.addEventListener('change', pushConfig);
  }

  (async function checkStartupFile() {
    let data;
    try {
      const resp = await fetch('/api/startup-file');
      data = await resp.json();
    } catch (e) {
      return;
    }
    if (!data || !data.path) return;
    showInputLoading('Opening document…');
    try {
      const resp = await fetch('/api/load/path', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: data.path }),
      });
      await startSessionFromResponse(resp);
    } catch (e) {
      console.error(e);
    } finally {
      hideInputLoading();
    }
  })();
})();
