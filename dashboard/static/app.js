'use strict';
const $ = id => document.getElementById(id);
// Gauge colours come from style.css (.gauge.<dimension>, stability zones via data-zone).
const dims = ['trust','curiosity','vigilance','attachment','stability','satisfaction'];
const label = (prefix, raw) => window.i18n ? window.i18n.label(prefix + raw, raw) : raw;
for (const name of dims) {
  const row = document.createElement('div');
  row.className = 'gauge '+name;
  row.innerHTML = `<div class="gauge-label"><span class="gauge-name"><i></i>${t('dim.'+name)}</span><output id="value-${name}">—</output></div><div class="track" role="meter" aria-label="${t('dim.'+name)}" aria-valuemin="0" aria-valuemax="1" id="meter-${name}"><div class="fill" id="fill-${name}"></div>${name==='stability'?'<i class="marker critical"></i><i class="marker warning"></i>':''}</div>`;
  $('gauges').append(row);
}
let busy = false, applying = false;
const clock = value => new Date(value).toLocaleTimeString([], {hour12:false});
async function api(path,options={}) {
  const response = await stiraFetch(path,{cache:'no-store',signal:AbortSignal.timeout(6000),...options});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
function renderState(s) {
  document.body.classList.remove('stale');
  $('error').hidden=true; $('connection-dot').className='status-dot live';
  $('connection').textContent=t('observer.live');
  $('last-sync').textContent=t('observer.last_sync',{time:clock(s.observed_at)});
  $('source-label').textContent=s.source_exists?t('observer.source_file',{version:s.version}):t('observer.source_new');
  for(const name of dims) {
    const value=s[name];
    if(typeof value!=='number' || !Number.isFinite(value)) throw new Error(t('observer.invalid_telemetry',{name}));
    $('value-'+name).textContent=value.toFixed(3);
    $('fill-'+name).style.width=(value*100)+'%';
    $('meter-'+name).setAttribute('aria-valuenow',value);
    if(name==='stability') $('fill-'+name).parentElement.parentElement.dataset.zone=value<s.thresholds.stability_critical?'crit':value<s.thresholds.stability_warning?'warn':'ok';
  }
  const copy={normal:[t('observer.normal_title'),t('observer.normal_detail')],warning:[t('observer.warning_title'),t('observer.warning_detail')],coherence:[t('observer.coherence_title'),t('observer.coherence_detail')],critical:[t('observer.critical_title'),t('observer.critical_detail')]};
  if(!copy[s.breaker]) throw new Error(t('observer.unknown_breaker'));
  $('breaker-panel').className='panel breaker '+s.signal.toLowerCase();
  $('signal').textContent=s.signal;
  $('break-level').textContent=s.breaker.toUpperCase();
  $('signal-title').textContent=copy[s.breaker][0]; $('signal-detail').textContent=copy[s.breaker][1];
  // #337: 1.0 is both "fewer than the minimum samples" and "measured, no movement".
  const unmeasured=s.bc_measured===false;
  $('bc-score').textContent=unmeasured?'—':s.bc_score.toFixed(2);
  $('bc-arc').style.strokeDashoffset=452.389*(unmeasured?1:1-s.bc_score);
  $('bc-dial').dataset.state=!unmeasured&&s.bc_score<s.thresholds.bc_threshold?'warn':'ok';
  $('bc-threshold').textContent=s.thresholds.bc_threshold.toFixed(2);
  $('bc-status').textContent=unmeasured?t('observer.bc_unmeasured'):s.bc_score<s.thresholds.bc_threshold?t('observer.below_threshold'):t('observer.coherent');
  $('bc-window').textContent=unmeasured?t('observer.bc_unmeasured_window',{count:s.bc_samples,min:s.bc_min_samples}):t('observer.window_samples',{count:s.bc_window.length});
  $('emotion').textContent=s.emotion_transcription || t('observer.no_transcription');
  $('emotion').className=s.emotion_transcription?'':'empty';
  $('apply').disabled=applying;
}
function renderHistory(entries) {
  $('history-count').textContent=entries.length+' / 10';
  const log=$('history'); log.replaceChildren();
  if(!entries.length) {
    const p=document.createElement('p');p.className='empty';
    p.textContent=t('observer.no_turns');
    log.append(p);return;
  }
  for(const entry of [...entries].reverse()) {
    const row=document.createElement('div');row.className='log-entry';
    const event=document.createElement('div');event.className='event';
    const time=document.createElement('time');time.textContent=clock(entry.timestamp);time.dateTime=entry.timestamp;
    const name=document.createElement('span');name.textContent=label('stimulus.',entry.stimulus);
    const details=document.createElement('small');details.textContent=t('observer.turn_details',{turn:String(entry.turn).padStart(2,'0'),source:entry.source || 'operator',intensity:Number(entry.intensity).toFixed(2),breaker:entry.breaker});
    event.append(time,name,details);
    const deltas=document.createElement('div');deltas.className='deltas';
    for(const [key,value] of Object.entries(entry.deltas || {})) {
      const chip=document.createElement('span');chip.className='delta'+(value<0?' negative':'');chip.textContent=`${label('dim.',key)} ${value>0?'+':''}${Number(value).toFixed(3)}`;deltas.append(chip);
    }
    if(!deltas.childElementCount){const chip=document.createElement('span');chip.className='micro';chip.textContent=t('observer.no_change');deltas.append(chip);}
    row.append(event,deltas);log.append(row);
  }
}
function offline(error) {
  document.body.classList.add('stale');$('connection-dot').className='status-dot offline';$('connection').textContent=t('observer.offline');
  $('error').hidden=false;$('error').textContent=t('observer.stale_error',{error:error.message});
  $('breaker-panel').className='panel breaker unknown';$('signal').textContent='—';$('break-level').textContent=t('base.unknown');
  $('signal-title').textContent=t('observer.no_signal');$('signal-detail').textContent=t('observer.reconnect_first');$('apply').disabled=true;
}
// Resolves to whether the state differs from the last read, null when it could not be read.
let lastKey=null;
async function refresh() {
  if(busy || applying) return undefined;
  busy=true;
  try {
    const [state,history]=await Promise.all([api('/api/state'),api('/api/history')]);renderState(state);renderHistory(history);
    const key=JSON.stringify([state,history],(name,value)=>name==='observed_at'?undefined:value);const changed=key!==lastKey;lastKey=key;return changed;
  }
  catch(error){offline(error);return null;}finally{busy=false;}
}
$('intensity').addEventListener('input',()=>{$('intensity-value').textContent=Number($('intensity').value).toFixed(2);});
$('stimulus-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(applying || busy) return;
  applying=true; $('apply').disabled=true;$('stimulus-result').textContent=t('observer.applying');
  try {
    const result=await api('/api/stimulus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stimulus:$('stimulus').value,intensity:Number($('intensity').value)})});
    renderState(result.state);renderHistory(await api('/api/history'));
    $('stimulus-result').textContent=t('observer.recorded',{turn:result.result.turn});
  } catch(error) { $('stimulus-result').textContent=error.message;offline(error); }
  finally { applying=false; await refresh(); }
});
// #369: not while the tab is hidden, slower while nothing changes, backed off on failure (poll.js).
if(typeof StiraPoll!=='undefined'){StiraPoll.keep(refresh,2000,15000,undefined,document);}
else{refresh();setInterval(refresh,2000);}
