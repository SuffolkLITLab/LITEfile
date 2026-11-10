/* Fictional, local-only interaction model for LITEfile issue #419.
 * No uploads, network requests, models, analytics, storage, or court services.
 * Source identity is a document ID; array position is deliberately independent.
 */
(() => {
  'use strict';
  const scenarios = {
    mixed: {
      suggestion: 'motion',
      reason: 'Its opening page is titled “Motion to extend time” and asks the court for more time. The first uploaded file is labeled as an exhibit.',
      documents: [
        { id: 'exhibit', name: 'Exhibit A — prior case.pdf', pages: 12, size: '1.8 MB', kind: 'exhibit' },
        { id: 'motion', name: 'Motion to extend time.pdf', pages: 4, size: '248 KB', kind: 'motion' },
        { id: 'service', name: 'Proof of service.pdf', pages: 2, size: '184 KB', kind: 'service' }
      ]
    },
    combined: {
      suggestion: 'packet',
      reason: 'Its opening page is titled “Motion to extend time.” The filename says it includes exhibits, so check which pages belong to the motion.',
      documents: [
        { id: 'packet', name: 'Motion with exhibits.pdf', pages: 18, size: '2.6 MB', kind: 'packet' },
        { id: 'service', name: 'Proof of service.pdf', pages: 2, size: '184 KB', kind: 'service' }
      ]
    },
    ambiguous: {
      suggestion: null,
      documents: [
        { id: 'motion', name: 'document-01.pdf', pages: 4, size: '248 KB', kind: 'motion' },
        { id: 'alternate', name: 'document-02.pdf', pages: 5, size: '322 KB', kind: 'alternate' },
        { id: 'exhibit', name: 'document-03.pdf', pages: 12, size: '1.8 MB', kind: 'exhibit' }
      ]
    },
    unreadable: {
      suggestion: null,
      documents: [
        { id: 'scan', name: 'scan-001.pdf', pages: 6, size: '3.2 MB', kind: 'unreadable' },
        { id: 'exhibit', name: 'Exhibit A — prior case.pdf', pages: 12, size: '1.8 MB', kind: 'exhibit' }
      ]
    }
  };
  const query = new URLSearchParams(window.location.search);
  const state = {
    concept: query.get('concept') === 'manual' ? 'manual' : 'assisted',
    scenario: Object.hasOwn(scenarios, query.get('scenario')) ? query.get('scenario') : 'mixed',
    phase: 'choose', order: [], selected: null, previewId: null, previewPage: 1,
    rangeMode: 'all', from: '1', to: '', aiOff: false, status: '', result: null,
    generation: 0, edits: {}, manual: {}, completedManually: false
  };
  const main = document.getElementById('main');
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const current = () => scenarios[state.scenario];
  const documentById = id => current().documents.find(d => d.id === id);
  const selectedDocument = () => documentById(state.selected);
  const rangeLabel = source => source.from === source.to ? `page ${source.from}` : `pages ${source.from}–${source.to}`;
  const announce = message => { document.getElementById('announcer').textContent = message; };
  const focus = id => { const element = document.getElementById(id); if (element) element.focus({preventScroll:true}); };
  const pageFocus = () => { main.focus({preventScroll:true}); main.scrollIntoView({block:'start',behavior:'auto'}); };
  const updateUrl = () => {
    const params = new URLSearchParams({concept:state.concept,scenario:state.scenario});
    try { window.history.replaceState(null, '', `${window.location.pathname}?${params}`); } catch (_) { /* Also works when opened as a local file. */ }
  };

  function reset({clearEdits = true} = {}) {
    state.generation += 1;
    state.phase = 'choose';
    state.order = current().documents.map(d => d.id);
    state.selected = state.concept === 'assisted' && !state.aiOff ? current().suggestion : null;
    state.previewId = state.selected || state.order[0];
    state.previewPage = 1;
    state.rangeMode = selectedDocument()?.kind === 'packet' ? 'range' : 'all';
    state.from = '1'; state.to = ''; state.status = ''; state.result = null;
    state.completedManually = false;
    if (clearEdits) { state.edits = {}; state.manual = {}; }
    updateUrl(); render();
  }

  function scope() {
    const doc = selectedDocument();
    if (!doc) return {valid:false,error:'Choose a document before starting the scan.'};
    if (state.rangeMode === 'all') return {valid:true,from:1,to:doc.pages,count:doc.pages};
    if (!String(state.from).trim() || !String(state.to).trim()) return {valid:false,error:'Enter the first and last page to read.'};
    const from = Number(state.from), to = Number(state.to);
    if (!Number.isInteger(from) || !Number.isInteger(to) || from < 1 || to > doc.pages || from > to) {
      return {valid:false,error:`Choose whole page numbers from 1 to ${doc.pages}, with the first page before or equal to the last.`};
    }
    return {valid:true,from,to,count:to-from+1};
  }

  function progress() {
    const review = ['results','conflict','unreadable','missing','manual','complete'].includes(state.phase);
    return `<nav class="workflow-progress" aria-label="Prototype filing steps"><ol>
      <li class="complete"><span class="step-number" aria-hidden="true">✓</span>Upload documents</li><li class="step-divider" aria-hidden="true"></li>
      <li class="${review?'complete':'current'}" ${!review?'aria-current="step"':''}><span class="step-number" aria-hidden="true">${review?'✓':'2'}</span>Choose a source</li><li class="step-divider" aria-hidden="true"></li>
      <li class="${review?'current':''}" ${review?'aria-current="step"':''}><span class="step-number" aria-hidden="true">3</span>Review case details</li>
    </ol></nav>`;
  }

  function suggestion() {
    if (state.concept !== 'assisted') return '';
    if (state.aiOff) return `<div class="notice neutral"><strong>You can choose the document yourself</strong>AI is off. Choose a document below, or enter the case details yourself.</div>`;
    if (state.scenario === 'ambiguous') return `<div class="notice"><strong>Which document are you filing now?</strong>Both opening pages look like possible main documents. Preview them and choose the one that describes your request. We haven’t selected one.</div>`;
    if (state.scenario === 'unreadable') return `<div class="notice"><strong>We couldn’t suggest a document</strong>The scan’s opening page isn’t readable. Choose a source yourself, or enter the case details without scanning.</div>`;
    const doc = documentById(current().suggestion);
    return `<div class="recommendation"><div class="label"><span aria-hidden="true">✦</span> Suggested document <span class="sample-tag">Simulated</span></div>
      <strong>${escape(doc.name)}</strong><p>${escape(current().reason)}</p>
      <div class="recommendation-actions">${state.selected !== doc.id?`<button class="text-button" data-action="use-suggestion">Use this document</button>`:''}<button class="text-button" data-action="choose-other">Choose another document</button></div>
      <details class="reason-details"><summary>What does the quick check look at?</summary><p>The proposed check uses filenames and a limited amount of opening-page text from each candidate. It only suggests where to start. Your confirmed document and page range are used for the later case-detail scan.</p><p>This prototype uses a hand-authored suggestion; no model is running.</p></details></div>`;
  }

  function fileRows() {
    return state.order.map((id, index) => {
      const doc = documentById(id), selected = state.selected === id;
      const label = selected ? 'For case suggestions' : state.concept==='assisted' && !state.aiOff && current().suggestion===id ? 'Suggested' : '';
      return `<li class="document-row ${selected?'selected':''} ${state.previewId===id?'previewing':''}">
        <div class="doc-main"><input type="radio" name="source" id="source-${id}" value="${id}" ${selected?'checked':''} aria-describedby="meta-${id}" required>
          <span class="file-icon" aria-hidden="true"><span>PDF</span></span>
          <label class="doc-label" for="source-${id}"><strong>${escape(doc.name)}</strong><span class="doc-meta" id="meta-${id}">${doc.pages} pages · ${doc.size}</span>${label?`<span class="doc-badge">${label}</span>`:''}</label>
          <button class="preview-btn" id="preview-${id}" data-action="preview" data-id="${id}" aria-label="Preview ${escape(doc.name)}">Preview <span aria-hidden="true">↗</span></button>
        </div>${state.concept==='manual'?`<div class="document-tools"><span>Position ${index+1} of ${state.order.length}</span><span class="reorder-buttons"><button id="up-${id}" data-action="move" data-id="${id}" data-direction="-1" aria-label="Move ${escape(doc.name)} up" ${index===0?'disabled':''}>↑</button><button id="down-${id}" data-action="move" data-id="${id}" data-direction="1" aria-label="Move ${escape(doc.name)} down" ${index===state.order.length-1?'disabled':''}>↓</button></span></div>`:''}
      </li>`;
    }).join('');
  }

  function scopeControls() {
    const doc = selectedDocument();
    if (!doc) return `<div class="scope-empty">Choose one document to see which pages we’ll read.</div>`;
    const selectedScope = scope();
    return `<div class="scope-panel"><fieldset><legend>Which pages should we read?</legend>
      <label class="scope-choice"><input id="scope-all" name="scope" type="radio" value="all" ${state.rangeMode==='all'?'checked':''}>Entire file <span class="small-muted">(${doc.pages} pages)</span></label>
      <label class="scope-choice"><input id="scope-range" name="scope" type="radio" value="range" ${state.rangeMode==='range'?'checked':''}>Choose pages — useful when this file includes exhibits</label>
      ${state.rangeMode==='range'?`<div class="range-fields"><label for="range-from">From</label><input type="number" id="range-from" min="1" max="${doc.pages}" step="1" value="${escape(state.from)}" aria-describedby="range-error scope-help"><label for="range-to">through</label><input type="number" id="range-to" min="1" max="${doc.pages}" step="1" value="${escape(state.to)}" placeholder="${doc.kind==='packet'?'3':doc.pages}" aria-describedby="range-error scope-help"><span class="small-muted">of ${doc.pages}</span></div><p class="validation-error" id="range-error" aria-live="polite">${selectedScope.valid?'':escape(selectedScope.error)}</p>`:''}
      </fieldset><p class="scope-help" id="scope-help">Use the document’s own pages, before its exhibits. <strong>All ${doc.pages} pages stay in your filing.</strong> This only changes the text used for suggestions.</p></div>`;
  }

  function pageData(doc, page) {
    const exhibit = doc.kind==='exhibit' || (doc.kind==='packet' && page>=4);
    const unreadable = doc.kind==='unreadable';
    let title = exhibit ? (doc.kind==='packet' && page===4?'Exhibit A — prior case':'Complaint from a prior case') : doc.kind==='service'?'Proof of service':doc.kind==='alternate'?'Motion to dismiss':'Motion to extend time';
    if (doc.kind==='packet' && page===3) title = 'Signature and requested relief';
    const text = exhibit ? 'This document is attached as an exhibit. It describes a separate, earlier case and is included only as background.' : doc.kind==='service' ? 'I certify that I provided the documents listed here to the other party in this case.' : doc.kind==='alternate' ? 'The responding party asks the court to dismiss the complaint for the reasons explained below.' : page===3 ? 'For these reasons, I ask the court to extend the time to respond. The exhibits that follow provide background information.' : 'I ask the court for additional time to respond in this case. This motion explains the request and the reasons for it.';
    const hasCaption = page===1 || (doc.kind==='packet' && page===4);
    return {exhibit,unreadable,hasCaption,title,text,caseName:exhibit?'Rivera v. Chen':'Taylor v. Morgan',caseNumber:exhibit?'DEMO-2023-00617':'DEMO-2026-01842'};
  }

  function preview() {
    const doc = documentById(state.previewId), page = state.previewPage, data = pageData(doc,page);
    return `<aside class="panel preview-panel" aria-label="Fictional document preview"><div class="preview-head"><h2 id="preview-heading" tabindex="-1">Document preview</h2><span class="sample-tag">Fictional sample</span></div><p class="preview-name">${escape(doc.name)}</p>
      <div class="preview-stage"><div class="paper ${data.exhibit?'exhibit':''} ${data.unreadable?'unreadable':''}">
        <div class="paper-watermark">Sample for design review only</div>
        ${data.unreadable?`<div class="blur-lines" aria-hidden="true"><div class="paper-court">EXAMPLE COUNTY COURT</div><div class="paper-caption"><span>Unreadable caption</span><span>Case no. ????</span></div><h3>Scanned document</h3>${'<div class="paper-line"></div>'.repeat(7)}</div><p style="margin-top:30px;text-align:center;font-family:inherit;font-size:12px">The text is too faint to read.</p>`:`<div class="paper-court">EXAMPLE COUNTY COURT</div>${data.hasCaption?`<div class="paper-caption"><span>${escape(data.caseName)}</span><span class="case-no">Case no.<br>${escape(data.caseNumber)}</span></div>`:'<div class="paper-caption"><span>Continuation page</span><span class="case-no">Caption on the opening page</span></div>'}<h3>${escape(data.title)}</h3><p>${escape(data.text)}</p><div class="paper-line"></div><div class="paper-line"></div><div class="paper-line short"></div>${!data.exhibit?'<div class="paper-signature">Alex Taylor</div>':''}`}
        <div class="paper-footer">Fictional document · Page ${page} of ${doc.pages}</div>
      </div></div>
      <div class="preview-pagination"><button id="previous-page" data-action="page" data-direction="-1" aria-label="Previous preview page" ${page===1?'disabled':''}>←</button><div class="page-chooser"><label for="preview-page">Page</label><select id="preview-page">${Array.from({length:doc.pages},(_,i)=>`<option value="${i+1}" ${i+1===page?'selected':''}>${i+1}</option>`).join('')}</select><span>of ${doc.pages}</span></div><button id="next-page" data-action="page" data-direction="1" aria-label="Next preview page" ${page===doc.pages?'disabled':''}>→</button></div>
      <div class="preview-callout">${data.unreadable?'You can enter the case details yourself if a scan cannot be read.':data.exhibit?'<strong>This page describes a different case.</strong> Check whether it is an exhibit before using it for suggestions.':doc.kind==='packet'?'<strong>Check the boundary:</strong> this sample’s motion ends on page 3. The prior case’s records start on page 4.':'Look for the document that tells the court what you are asking for in this filing.'}</div>
    </aside>`;
  }

  function scanSummary() {
    const doc = selectedDocument(), selectedScope = scope();
    if (!doc) return '<strong>Choose a document to continue</strong><p>No case-detail scan has started.</p>';
    return `<strong>${selectedScope.valid?'We’ll read':'Set the page range for'}: ${escape(doc.name)}${selectedScope.valid?` · ${rangeLabel(selectedScope)}`:''}</strong><p>All ${current().documents.length} files stay in your filing. Other files won’t be used for these case suggestions.</p>`;
  }

  function chooseScreen() {
    return `${progress()}<div class="heading-row"><div><span class="eyebrow">${state.concept==='assisted'?'Concept A · Suggest, then confirm':'Concept B · Choose and organize'}</span><h1>Which document should we read?</h1><p class="lede">Choose the document that tells the court what you are asking for, such as a complaint or motion. We’ll use it to suggest case details.</p></div><span class="ready-count"><b>${current().documents.length} files</b> ready to review</span></div>
      ${state.status?`<div class="source-status" role="status">${escape(state.status)}</div>`:''}
      <div class="work-area"><section class="panel selection-panel" aria-label="Choose the document for case suggestions">${suggestion()}
        <div class="panel-heading"><h2>Your documents</h2><small>${state.concept==='manual'?'Choose one':'Check the selection'}</small></div>
        <fieldset id="document-choice"><legend class="sr-only">Document for case suggestions</legend><ol class="file-list">${fileRows()}</ol></fieldset>
        <p class="file-list-help">${state.concept==='manual'?'Use the arrows to organize your files. Changing the order won’t change the document selected for suggestions.':'An exhibit can describe a different case. You can select any document if the suggestion doesn’t fit.'}</p>
        ${scopeControls()}
        <label class="ai-setting"><input id="ai-off" type="checkbox" ${state.aiOff?'checked':''}><span>Do not use AI to read my documents</span></label>
      </section>${preview()}</div>
      <section class="panel action-panel" aria-label="Confirm the source before scanning"><div class="scan-summary" id="scan-summary">${scanSummary()}</div><div class="action-buttons"><button class="primary" id="scan-button" data-action="scan" ${scope().valid?'':'disabled'}>${state.concept==='assisted'?'Confirm and scan':'Read selected document'}<span aria-hidden="true">→</span></button></div></section>
      <div class="below-actions"><button class="text-button" data-action="manual-entry">Enter case details myself</button><p>${state.aiOff?'AI is off. This prototype simulates the local rules path.':'You’ll review every suggestion before continuing.'}</p></div>`;
  }

  function receipt() {
    const r = state.result, doc = documentById(r.docId);
    return `<aside class="panel source-receipt"><h2>Where this came from</h2><div class="receipt-section"><span class="receipt-label">Document we read</span><p class="source-file">${escape(doc.name)}</p><p>${rangeLabel(r)} · ${r.count} ${r.count===1?'page':'pages'} in the scan</p><button class="text-button" data-action="change-source">Change document or pages</button></div><div class="receipt-section"><span class="receipt-label">Kept in your filing</span><p>All ${current().documents.length} complete files, including every exhibit.</p><ul class="receipt-list">${state.order.map(id=>`<li>${escape(documentById(id).name)}</li>`).join('')}</ul></div><p>${state.aiOff?'AI is off. Results illustrate the local rules path.':'Only the confirmed source and pages are used for these case details.'}</p><p style="margin-top:9px">This is a simulated scan of fictional documents.</p></aside>`;
  }

  function resultFields() {
    const fields = [['caseName','Case name'],['caseNumber','Case number'],['documentTitle','Document title']];
    return fields.map(([key,label])=>`<div class="result-field"><label for="result-${key}">${label}</label><input id="result-${key}" data-result-field="${key}" type="text" value="${escape(Object.hasOwn(state.edits,key)?state.edits[key]:state.result.values[key])}" maxlength="160"><p class="provenance">${Object.hasOwn(state.edits,key)?'<span class="edit-kept">Your edit is kept.</span>':`From ${escape(documentById(state.result.docId).name)} · page ${state.result.evidencePage}`}</p></div>`).join('');
  }

  function resultsScreen() {
    return `${progress()}<div class="heading-row"><div><span class="eyebrow">Sample results · Check before continuing</span><h1>Review the case details</h1><p class="lede">These suggestions came from the document and pages you chose. Check the details and make any changes.</p></div></div><div class="results-layout"><section class="panel result-card"><div class="result-banner"><span aria-hidden="true">✓</span><span><strong>One source, selected pages.</strong> The rest of the filing was kept out of this case-detail scan.</span></div>${resultFields()}<div class="review-actions"><button class="secondary" data-action="change-source">Change source</button><button class="primary" data-action="finish">Keep these details <span aria-hidden="true">→</span></button></div><p class="review-note">A suggested document title does not choose the court’s filing code. You would review that later in LITEfile.</p></section>${receipt()}</div>`;
  }

  function issueScreen() {
    const conflict = state.phase==='conflict', unreadable = state.phase==='unreadable';
    const title = conflict?'These pages describe more than one case':unreadable?'We couldn’t read this scan':'We couldn’t find the case caption';
    const text = conflict?'The selected pages contain the motion and records from a prior case. Choose just the motion’s pages, or enter the case details yourself.':unreadable?'The text is too faint to create useful suggestions. You can choose another document or enter the details yourself.':'The selected pages do not include a case caption in this sample. Change the page range to include the opening page, or enter the details yourself.';
    return `${progress()}<div class="heading-row"><div><span class="eyebrow">Sample scan · Needs your help</span><h1>${title}</h1><p class="lede">${text}</p></div></div><div class="results-layout"><section class="panel result-card"><div class="notice"><strong>No case details have been applied</strong>${conflict?'The prototype surfaces this conflict instead of choosing a case for you.':'The prototype stops here instead of switching to another source automatically.'}</div>${conflict?'<div class="conflict-caption"><strong>Taylor v. Morgan</strong><span>Motion · pages 1–3 · current filing in this sample</span></div><div class="conflict-caption"><strong>Rivera v. Chen</strong><span>Exhibit A · pages 4–18 · records from another case</span></div><p class="small-muted">A real system may not detect every conflicting caption. Checking the source and page boundary before scanning remains necessary.</p>':''}<div class="review-actions"><button class="secondary" data-action="manual-entry">Enter details myself</button><button class="primary" data-action="change-source">${conflict?'Choose the motion’s pages':'Change document or pages'} <span aria-hidden="true">→</span></button></div></section>${receipt()}</div>`;
  }

  function manualScreen() {
    return `${progress()}<section class="panel empty-panel"><span class="eyebrow">Continue without scanning</span><h1 style="margin-top:9px">Enter the case details yourself</h1><p>All ${current().documents.length} files stay in your filing. We won’t scan them for case suggestions.</p><form id="manual-form" class="form-fields"><div class="result-field"><label for="manual-caseName">Case name</label><input id="manual-caseName" name="caseName" type="text" placeholder="For example, Taylor v. Morgan" value="${escape(state.manual.caseName)}" maxlength="160" required></div><div class="result-field"><label for="manual-caseNumber">Case number, if you have one</label><input id="manual-caseNumber" name="caseNumber" type="text" value="${escape(state.manual.caseNumber)}" maxlength="160"></div><div class="result-field"><label for="manual-documentTitle">What document are you filing?</label><input id="manual-documentTitle" name="documentTitle" type="text" placeholder="For example, a motion to extend time" value="${escape(state.manual.documentTitle)}" maxlength="160" required></div><div class="review-actions"><button type="button" class="secondary" data-action="back-choose">Back to documents</button><button class="primary" type="submit" style="margin-top:0">Continue in prototype <span aria-hidden="true">→</span></button></div></form><p class="small-muted">Try fictional details. These fields stay only in this browser page and reset when you reload.</p></section>`;
  }

  function completeScreen() {
    const source = state.completedManually ? 'You entered the case details yourself.' : `You used ${documentById(state.result.docId).name}, ${rangeLabel(state.result)}, and reviewed the suggestions.`;
    return `${progress()}<section class="panel empty-panel"><div class="completion-mark" aria-hidden="true">✓</div><span class="eyebrow">End of this prototype</span><h1 style="margin-top:10px">Ready for the next filing step</h1><p>${escape(source)}</p><p>In LITEfile, you would continue reviewing the case and filing details. This prototype has not saved a filing or sent anything to a court.</p><div class="retained-files"><strong>${current().documents.length} complete files retained.</strong> Choosing a document or page range for suggestions did not remove any exhibits.</div><div class="completion-actions"><button class="primary" data-action="concept" data-value="${state.concept==='assisted'?'manual':'assisted'}">Try concept ${state.concept==='assisted'?'B':'A'} <span aria-hidden="true">→</span></button><button class="secondary" data-action="reset">Restart this example</button></div></section>`;
  }

  function render() {
    document.getElementById('concept-assisted').classList.toggle('active',state.concept==='assisted');
    document.getElementById('concept-manual').classList.toggle('active',state.concept==='manual');
    for (const concept of ['assisted','manual']) {
      const link = document.getElementById(`concept-${concept}`);
      if (state.concept===concept) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
      link.href = `?concept=${concept}&scenario=${state.scenario}`;
    }
    document.getElementById('scenario').value = state.scenario;
    document.title = `${state.concept==='assisted'?'Suggest, then confirm':'Choose and organize'} · LITEfile prototype`;
    main.innerHTML = state.phase==='choose'?chooseScreen():state.phase==='results'?resultsScreen():['conflict','unreadable','missing'].includes(state.phase)?issueScreen():state.phase==='manual'?manualScreen():state.phase==='complete'?completeScreen():`${progress()}<section class="panel scanning" aria-busy="true"><div class="spinner" aria-hidden="true"></div><h1>Reading your selected pages</h1><p>${escape(selectedDocument().name)} · ${rangeLabel(scope())}</p><p class="small-muted">Simulating the next step. No document is being sent to a model.</p></section>`;
  }

  function selectSource(id) {
    if (!documentById(id)) return;
    state.selected = id; state.previewId = id; state.previewPage = 1;
    state.rangeMode = selectedDocument().kind==='packet'?'range':'all';
    state.from = '1'; state.to = ''; state.result = null;
    render(); focus(`source-${id}`); announce(`${selectedDocument().name} selected for case suggestions.`);
  }

  function syncScopeUI() {
    const selectedScope = scope();
    const button = document.getElementById('scan-button');
    if (button) button.disabled = !selectedScope.valid;
    const summary = document.getElementById('scan-summary');
    if (summary) summary.innerHTML = scanSummary();
    const error = document.getElementById('range-error');
    if (error) error.textContent = selectedScope.valid?'':selectedScope.error;
    for (const id of ['range-from','range-to']) {
      const input = document.getElementById(id);
      if (input) input.setAttribute('aria-invalid',String(!selectedScope.valid));
    }
  }

  function simulateScan() {
    const selectedScope = scope(), doc = selectedDocument();
    if (!selectedScope.valid || state.phase!=='choose') return;
    const generation = ++state.generation;
    const request = {docId:doc.id,...selectedScope};
    state.result = null; state.phase = 'scanning'; render(); pageFocus();
    announce('Simulating a scan of the confirmed document and pages.');
    window.setTimeout(() => {
      if (generation !== state.generation) return;
      state.result = request;
      if (doc.kind==='unreadable') state.phase='unreadable';
      else if (doc.kind==='packet' && request.from<=3 && request.to>=4) state.phase='conflict';
      else {
        const evidencePage = doc.kind==='packet' && request.from>=4?4:1;
        if (request.from>evidencePage || request.to<evidencePage) state.phase='missing';
        else {
          const data = pageData(doc,evidencePage);
          state.result.values = {caseName:data.caseName,caseNumber:data.caseNumber,documentTitle:data.exhibit?'Complaint from a prior case':data.title};
          state.result.evidencePage = evidencePage;
          state.phase='results';
        }
      }
      render(); pageFocus(); announce(state.phase==='results'?'Sample suggestions ready. Review the details and their source.':'The sample scan needs your help. No details were applied.');
    },650);
  }

  function changeSource() {
    state.generation += 1;
    state.phase='choose'; state.result=null;
    state.status=Object.keys(state.edits).length?'Previous sample suggestions were cleared. Your edits will be kept when you scan the new source.':'Previous sample suggestions were cleared. Check the document and pages, then scan again.';
    render(); pageFocus(); announce(state.status);
  }

  document.addEventListener('click', event => {
    const target = event.target.closest('[data-action]');
    if (!target || target.disabled) return;
    const action = target.dataset.action;
    event.preventDefault();
    if (action==='concept') {
      state.concept=target.dataset.value==='manual'?'manual':'assisted';
      reset(); focus(`concept-${state.concept}`); announce(`Concept ${state.concept==='assisted'?'A: suggest, then confirm':'B: choose and organize'} loaded.`);
    } else if (action==='reset') { const fromMain=main.contains(target);reset();if(fromMain)pageFocus();announce('This example has been reset.'); }
    else if (action==='preview') { state.previewId=target.dataset.id;state.previewPage=1;render();focus('preview-heading'); if(window.innerWidth<701)document.querySelector('.preview-panel').scrollIntoView({block:'start',behavior:'auto'});announce(`Previewing ${documentById(state.previewId).name}. This does not change the selected source.`); }
    else if (action==='use-suggestion') selectSource(current().suggestion);
    else if (action==='choose-other') { state.selected=null;state.result=null;render();focus(`source-${state.order[0]}`);announce('Choose a different document.'); }
    else if (action==='move') {
      const index=state.order.indexOf(target.dataset.id), next=index+Number(target.dataset.direction);
      if(next<0 || next>=state.order.length) return;
      [state.order[index],state.order[next]]=[state.order[next],state.order[index]];
      render();
      const preferred=document.getElementById(target.id);
      if(preferred && !preferred.disabled)focus(target.id);else focus(`source-${target.dataset.id}`);
      announce(`${documentById(target.dataset.id).name} moved to position ${next+1}. ${selectedDocument()?`${selectedDocument().name} remains selected.`:'No source is selected.'}`);
    } else if (action==='page') {
      const doc=documentById(state.previewId);state.previewPage=Math.max(1,Math.min(doc.pages,state.previewPage+Number(target.dataset.direction)));render();
      const preferred=document.getElementById(target.id);if(preferred&&!preferred.disabled)focus(target.id);else focus('preview-page');announce(`Preview page ${state.previewPage} of ${doc.pages}.`);
    } else if (action==='scan') simulateScan();
    else if (action==='change-source') changeSource();
    else if (action==='manual-entry') { state.generation+=1;state.manual={...state.manual,...state.edits};state.phase='manual';state.result=null;render();pageFocus(); }
    else if (action==='back-choose') { state.phase='choose';render();pageFocus(); }
    else if (action==='finish') {state.completedManually=false;state.phase='complete';render();pageFocus();announce('Prototype complete. Nothing was filed.');}
  });

  document.addEventListener('change', event => {
    const target=event.target;
    if(target.id==='scenario') {state.scenario=target.value;reset();focus('scenario');announce('New example loaded. Choices and sample results were reset.');}
    else if(target.name==='source')selectSource(target.value);
    else if(target.name==='scope') {state.rangeMode=target.value;render();focus(`scope-${target.value}`);syncScopeUI();}
    else if(target.id==='preview-page') {state.previewPage=Number(target.value);render();focus('preview-page');announce(`Preview page ${state.previewPage}.`);}
    else if(target.id==='ai-off') {
      state.aiOff=target.checked;state.generation+=1;state.result=null;
      if(state.concept==='assisted') {state.selected=null;state.rangeMode='all';state.from='1';state.to='';}
      state.status=state.aiOff?'AI is off. Select a source for local rules, or enter the case details yourself.':state.concept==='assisted'&&current().suggestion?'AI is on. Choose a source or use the suggested document before scanning.':'AI is on. Choose a source before scanning.';
      render();focus('ai-off');announce(state.status);
    }
  });

  document.addEventListener('input', event => {
    const target=event.target;
    if(target.id==='range-from'||target.id==='range-to') {state[target.id==='range-from'?'from':'to']=target.value;syncScopeUI();}
    if(target.dataset.resultField) {state.edits[target.dataset.resultField]=target.value;const provenance=target.nextElementSibling;if(provenance)provenance.innerHTML='<span class="edit-kept">Your edit is kept.</span>';}
    if(target.id.startsWith('manual-'))state.manual[target.name]=target.value;
  });

  document.addEventListener('submit', event => {
    if(event.target.id!=='manual-form')return;
    event.preventDefault();
    if(!event.target.reportValidity())return;
    state.completedManually=true;state.phase='complete';render();pageFocus();announce('Prototype complete using manually entered details. Nothing was filed.');
  });
  reset();
})();
