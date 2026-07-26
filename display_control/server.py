"""Small, dependency-free HTTP server for configuring the E-Ink frame by phone."""

from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import socket
import tempfile
import threading
import tomllib
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from .settings import (
    Catalog,
    SettingsStore,
    SettingsValidationError,
    default_photo_path,
    default_settings_path,
    discover_catalog,
)


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#173b35">
  <title>E-Ink Frame Control</title>
  <link rel="stylesheet" href="/app.css">
  <script src="/app.js" defer></script>
</head>
<body>
  <header class="hero">
    <div class="brand-mark" aria-hidden="true">◒</div>
    <div class="hero-copy">
      <p class="eyebrow">Utah County · E-Ink frame</p>
      <h1>Frame control</h1>
    </div>
    <span id="connection-pill" class="pill waiting"><i></i>Connecting</span>
  </header>

  <main>
    <nav class="tabs" aria-label="Control panel sections">
      <button class="tab active" data-tab="overview" aria-selected="true"><span>⌒</span>Overview</button>
      <button class="tab" data-tab="locations" aria-selected="false"><span>△</span>Locations</button>
      <button class="tab" data-tab="activities" aria-selected="false"><span>☼</span>Activities</button>
      <button class="tab" data-tab="photo" aria-selected="false"><span>▣</span>Photo</button>
    </nav>

    <div id="loading" class="loading-card"><span class="spinner"></span><p>Loading your frame…</p></div>
    <div id="fatal" class="notice error hidden" role="alert"></div>

    <section id="panel-overview" class="panel active" aria-labelledby="overview-title">
      <div class="section-heading">
        <div><p class="eyebrow">At a glance</p><h2 id="overview-title">Good morning</h2></div>
        <button id="refresh" class="icon-button" title="Reload settings" aria-label="Reload settings">↻</button>
      </div>
      <div class="stat-grid">
        <article class="stat"><span class="stat-icon mountain">△</span><strong id="location-count">—</strong><small>locations in rotation</small></article>
        <article class="stat"><span class="stat-icon sun">☀</span><strong id="activity-count">—</strong><small>activities considered</small></article>
        <article class="stat"><span class="stat-icon photo">▣</span><strong id="photo-state">—</strong><small>personal photo</small></article>
      </div>

      <article class="card">
        <div class="card-title"><div><p class="eyebrow">Forecast basics</p><h3>What the frame displays</h3></div></div>
        <div class="field-grid">
          <label class="field span-2"><span>Forecast location</span><input id="location-name" maxlength="120" autocomplete="off" placeholder="Provo, Utah"></label>
          <label class="field"><span>Units</span><select id="units"><option value="imperial">Imperial · °F</option><option value="metric">Metric · °C</option></select></label>
          <label class="switch-row field-switch"><span><b>Weather caption</b><small>Show condition text on artwork</small></span><input id="display-caption" type="checkbox" role="switch"></label>
        </div>
      </article>

      <article class="card">
        <div class="card-title"><div><p class="eyebrow">Activity ideas</p><h3>Recommendation tuning</h3></div></div>
        <div class="field-grid">
          <label class="field"><span>Ideas per forecast</span><input id="recommendation-count" type="number" min="1" max="10" step="1"></label>
          <label class="field"><span>Minimum match <output id="suitability-output">50%</output></span><input id="minimum-suitability" type="range" min="0" max="1" step="0.05"></label>
        </div>
        <p class="helper">Only activities above this weather match are suggested. Annual great days measure rarity, so a special day can rank higher; they are an estimate, not a quota.</p>
      </article>

      <article class="card compact">
        <div class="card-title"><div><p class="eyebrow">Control panel</p><h3>Keep this address handy</h3></div><span class="success-dot"></span></div>
        <p class="muted">Add this page to your phone’s Home Screen while your phone and frame are on the same Wi-Fi.</p>
      </article>
    </section>

    <section id="panel-locations" class="panel" aria-labelledby="locations-title">
      <div class="section-heading"><div><p class="eyebrow">Scene rotation</p><h2 id="locations-title">Utah landscapes</h2><p>Pick at least one place. Auto mode rotates only through these scenes.</p></div></div>
      <div id="location-list" class="location-grid"></div>
    </section>

    <section id="panel-activities" class="panel" aria-labelledby="activities-title">
      <div class="section-heading"><div><p class="eyebrow">Weather-aware ideas</p><h2 id="activities-title">Outdoor activities</h2><p>Choose what you actually enjoy, then fine-tune any activity’s ideal conditions.</p></div></div>
      <div class="activity-toolbar card compact">
        <label class="search"><span aria-hidden="true">⌕</span><input id="activity-search" type="search" placeholder="Search activities" autocomplete="off"></label>
        <div><button id="enable-all" class="text-button">All</button><button id="disable-all" class="text-button">None</button></div>
      </div>
      <p id="activity-summary" class="list-summary"></p>
      <div id="activity-list" class="activity-list"></div>
      <div id="activity-empty" class="empty hidden">No activities match that search.</div>
    </section>

    <section id="panel-photo" class="panel" aria-labelledby="photo-title">
      <div class="section-heading"><div><p class="eyebrow">Your artwork</p><h2 id="photo-title">Personal photo</h2><p>Upload from your camera roll. The original stays on your phone; the frame stores an optimized PNG.</p></div></div>
      <article class="card photo-card">
        <div id="photo-preview" class="photo-preview empty-preview"><span>▣</span><p>No photo uploaded yet</p></div>
        <label class="upload-button"><input id="photo-file" type="file" accept="image/png,image/jpeg,image/webp" hidden><span>↑</span><b>Choose a photo</b></label>
        <p id="photo-filename" class="file-name">PNG, JPEG, or WebP · up to 20 MB</p>
      </article>
      <article class="card">
        <label class="switch-row"><span><b>Include personal photo</b><small>Make the uploaded image available to the frame</small></span><input id="photo-enabled" type="checkbox" role="switch"></label>
        <div class="field-grid photo-fields">
          <label class="field span-2"><span>Photo caption</span><input id="photo-caption" maxlength="200" placeholder="Optional caption"></label>
          <label class="field"><span>Rotation</span><select id="photo-rotation"><option value="0">No rotation</option><option value="90">90° clockwise</option><option value="180">180°</option><option value="270">90° counter-clockwise</option></select></label>
        </div>
      </article>
      <div id="upload-progress" class="notice hidden" role="status"></div>
    </section>
  </main>

  <div id="save-bar" class="save-bar hidden" role="status">
    <span><i></i><b>Unsaved changes</b></span>
    <button id="discard" class="secondary-button">Discard</button>
    <button id="save" class="primary-button">Save to frame</button>
  </div>
  <footer><button id="reset" class="danger-link">Restore all defaults</button><span>Control panel v1</span></footer>
  <div id="toast" class="toast hidden" role="status"></div>
</body>
</html>"""

APP_CSS = r"""
:root{--ink:#16312c;--forest:#1d5145;--sage:#7d9a78;--mint:#dbe8dc;--paper:#f7f3e9;--white:#fffdf8;--sun:#e7ad52;--red:#a74e3c;--line:#d8d8c8;--shadow:0 10px 34px rgba(31,58,47,.10);font-family:Inter,ui-rounded,"SF Pro Rounded",system-ui,-apple-system,sans-serif;color:var(--ink);background:var(--paper);font-synthesis:none}
*{box-sizing:border-box}body{margin:0;min-width:320px;background:radial-gradient(circle at 95% 4%,#e3d6ae88 0,transparent 25rem),linear-gradient(180deg,#eef3e9 0,var(--paper) 24rem);min-height:100vh}button,input,select{font:inherit;color:inherit}.hero{max-width:1040px;margin:auto;padding:calc(22px + env(safe-area-inset-top)) 22px 18px;display:flex;align-items:center;gap:13px}.brand-mark{width:45px;height:45px;border-radius:15px;background:var(--forest);color:white;display:grid;place-items:center;font-size:26px;box-shadow:0 7px 18px #1d514533}.hero-copy{flex:1}.hero h1,.section-heading h2,.card h3{margin:0;line-height:1.1}.hero h1{font-size:clamp(24px,6vw,34px);letter-spacing:-.04em}.eyebrow{text-transform:uppercase;font-size:10px;letter-spacing:.15em;font-weight:800;color:#6b7e68;margin:0 0 5px}.pill{border:1px solid #b8c8b7;background:#ffffffb3;padding:8px 11px;border-radius:99px;font-size:12px;font-weight:750;white-space:nowrap}.pill i,.save-bar i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;background:#d7a12e}.pill.online i{background:#4b9a65}.pill.offline i{background:var(--red)}main{max-width:1040px;margin:auto;padding:0 18px 130px}.tabs{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;padding:5px;border:1px solid #dce1d5;background:#f9fbf5cc;backdrop-filter:blur(12px);border-radius:17px;position:sticky;top:8px;z-index:10;box-shadow:0 3px 18px #28482c0a}.tab{appearance:none;border:0;background:transparent;border-radius:12px;padding:10px 4px;color:#6c7a6e;font-size:11px;font-weight:700;cursor:pointer}.tab span{display:block;font-size:19px;height:23px}.tab.active{background:var(--forest);color:#fff;box-shadow:0 4px 12px #214c4133}.panel{display:none;animation:fade .2s ease}.panel.active{display:block}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:30px 3px 18px}.section-heading h2{font-family:Georgia,serif;font-weight:500;font-size:clamp(27px,7vw,38px);letter-spacing:-.03em}.section-heading p:not(.eyebrow){color:#69776d;line-height:1.45;margin:8px 0 0;max-width:640px}.icon-button{border:1px solid var(--line);background:var(--white);border-radius:50%;width:43px;height:43px;font-size:22px;cursor:pointer}.card,.stat,.location-card,.activity-item{background:rgba(255,253,248,.94);border:1px solid rgba(130,143,118,.23);border-radius:21px;box-shadow:var(--shadow)}.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}.stat{padding:17px 14px;min-width:0}.stat strong{display:block;font-size:25px;margin:10px 0 2px;letter-spacing:-.04em}.stat small{color:#728074;display:block;line-height:1.2}.stat-icon{width:32px;height:32px;display:grid;place-items:center;border-radius:10px;background:#e8eee4;color:var(--forest);font-size:18px}.stat-icon.sun{background:#f7e7c4;color:#a66b15}.stat-icon.photo{background:#e7e4ef;color:#645b7a}.card{padding:20px;margin:14px 0}.card.compact{padding:17px}.card-title{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:18px}.card h3{font-size:19px}.success-dot{width:10px;height:10px;border-radius:50%;background:#5b9d68;box-shadow:0 0 0 6px #5b9d6818}.field-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:15px}.field{display:flex;flex-direction:column;gap:7px;font-size:13px;font-weight:750}.field.span-2{grid-column:span 2}.field input:not([type=range]),.field select,.search{width:100%;border:1px solid #ccd4c8;background:#fff;border-radius:12px;min-height:47px;padding:10px 12px;outline:none}.field input:focus,.field select:focus,.search:focus-within{border-color:#5e887a;box-shadow:0 0 0 3px #5385741b}.field output{float:right;color:var(--forest)}input[type=range]{accent-color:var(--forest);width:100%;height:28px}.switch-row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:4px 0}.switch-row span{display:flex;flex-direction:column;gap:3px}.switch-row small,.helper,.muted,.file-name{color:#728074;line-height:1.45}.switch-row input[type=checkbox]{appearance:none;width:48px;height:28px;border-radius:99px;background:#cbd1c8;position:relative;transition:.2s;flex:none}.switch-row input[type=checkbox]::after{content:"";position:absolute;width:22px;height:22px;left:3px;top:3px;border-radius:50%;background:#fff;box-shadow:0 2px 5px #0003;transition:.2s}.switch-row input:checked{background:var(--forest)}.switch-row input:checked::after{transform:translateX(20px)}.field-switch{align-self:end;min-height:47px}.helper{font-size:12px;margin:15px 0 0;border-left:3px solid #d6bd7b;padding-left:11px}.location-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.location-card{position:relative;overflow:hidden;cursor:pointer;min-height:190px;display:flex;flex-direction:column;justify-content:flex-end;padding:18px;isolation:isolate;transition:.18s}.location-card::before{content:"";position:absolute;inset:0;z-index:-2;background:linear-gradient(160deg,var(--scene-a),var(--scene-b))}.location-card::after{content:"";position:absolute;z-index:-1;inset:42% -12% -23%;background:var(--mountain);clip-path:polygon(0 58%,15% 30%,31% 57%,47% 8%,65% 45%,80% 24%,100% 53%,100% 100%,0 100%);opacity:.68}.location-card input{position:absolute;right:14px;top:14px;width:25px;height:25px;accent-color:var(--forest)}.location-card h3{margin:0 38px 4px 0;font-size:19px}.location-card p{font-size:12px;line-height:1.35;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.location-card.selected{outline:3px solid var(--forest);outline-offset:-3px}.location-card:not(.selected){filter:saturate(.55);opacity:.68}.location-card .art-badge{position:absolute;left:14px;top:14px;font-size:10px;font-weight:800;padding:5px 7px;border-radius:99px;background:#ffffffb8}.activity-toolbar{display:flex;align-items:center;gap:12px;position:sticky;top:83px;z-index:8}.search{display:flex;align-items:center;gap:8px;flex:1;min-height:44px;padding:6px 11px}.search input{border:0;outline:0;background:transparent;width:100%}.text-button{border:0;background:transparent;color:var(--forest);font-weight:800;padding:9px 7px;cursor:pointer}.list-summary{font-size:12px;color:#728074;margin:15px 4px 9px}.activity-list{display:grid;gap:10px}.activity-item{overflow:hidden}.activity-head{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:12px;padding:15px}.activity-toggle{width:23px;height:23px;accent-color:var(--forest)}.activity-title b{display:block}.activity-title small{color:#748075}.art-chip{font-size:10px;background:#e8eee4;color:#346051;padding:5px 7px;border-radius:99px;font-weight:800}.activity-item details{border-top:1px solid #e5e7dd}.activity-item summary{list-style:none;padding:12px 15px;cursor:pointer;color:var(--forest);font-size:12px;font-weight:800}.activity-item summary::-webkit-details-marker{display:none}.activity-item summary::after{content:"+";float:right;font-size:18px;line-height:12px}.activity-item details[open] summary::after{content:"−"}.activity-editor{padding:4px 15px 18px}.days-row{display:grid;grid-template-columns:1fr 100px;align-items:end;gap:10px;margin-bottom:15px}.metric-table{overflow-x:auto;border:1px solid #e0e3d9;border-radius:13px}.metric-head,.metric-row{display:grid;grid-template-columns:minmax(122px,1.5fr) repeat(5,minmax(66px,.7fr)) 62px;align-items:center;min-width:580px}.metric-head{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:#718075;background:#f1f3ec;padding:8px}.metric-row{border-top:1px solid #e5e7df;padding:8px;background:#fff}.metric-row:first-of-type{border-top:0}.metric-name{font-size:11px;font-weight:750}.metric-row input[type=number]{width:60px;border:1px solid #d8ddd3;border-radius:8px;padding:7px 5px;font-size:12px}.metric-required{text-align:center}.metric-required input{width:18px;height:18px;accent-color:var(--forest)}.restore-activity{margin-top:12px}.photo-card{text-align:center}.photo-preview{aspect-ratio:4/3;border-radius:15px;overflow:hidden;background:#e9ece4;display:grid;place-items:center;margin-bottom:16px;border:1px dashed #b9c2b5}.photo-preview img{width:100%;height:100%;object-fit:cover}.empty-preview span{font-size:37px;color:#788b79}.empty-preview p{margin:-25% 0 0;color:#718074;font-size:13px}.upload-button{display:flex;justify-content:center;align-items:center;gap:9px;background:var(--forest);color:#fff;border-radius:13px;padding:13px;cursor:pointer}.upload-button span{font-size:20px}.file-name{font-size:12px;margin:10px 0 0}.photo-fields{margin-top:18px;padding-top:17px;border-top:1px solid #e4e6dc}.notice{border-radius:13px;padding:13px 15px;background:#e6eee2;color:#315742}.notice.error{background:#f6e2dc;color:#7f372a}.loading-card,.empty{text-align:center;padding:50px 20px;color:#68766b}.spinner{display:inline-block;width:24px;height:24px;border:3px solid #cfdbcd;border-top-color:var(--forest);border-radius:50%;animation:spin .8s linear infinite}.save-bar{position:fixed;z-index:20;bottom:calc(12px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);width:min(calc(100% - 24px),720px);background:#173b35;color:#fff;border-radius:18px;padding:10px 11px 10px 16px;display:flex;align-items:center;gap:9px;box-shadow:0 15px 35px #102e2770}.save-bar span{margin-right:auto;font-size:13px}.save-bar i{background:var(--sun)}.primary-button,.secondary-button{border:0;border-radius:11px;padding:10px 13px;font-weight:800;cursor:pointer}.primary-button{background:#fff;color:var(--forest)}.secondary-button{background:#ffffff18;color:#fff}.danger-link{border:0;background:transparent;color:#9b5546;font-weight:700;cursor:pointer}footer{max-width:1040px;margin:-92px auto 0;padding:20px 20px calc(30px + env(safe-area-inset-bottom));display:flex;justify-content:space-between;color:#899287;font-size:11px}.toast{position:fixed;left:50%;top:18px;transform:translateX(-50%);z-index:50;background:#173b35;color:#fff;border-radius:12px;padding:11px 16px;box-shadow:var(--shadow);font-size:13px}.hidden{display:none!important}@keyframes spin{to{transform:rotate(360deg)}}@keyframes fade{from{opacity:0;transform:translateY(4px)}}
.location-card.scene-0{--scene-a:#b9d7d4;--scene-b:#728b68;--mountain:#4b6952}.location-card.scene-1{--scene-a:#efc18c;--scene-b:#be674a;--mountain:#71423a}.location-card.scene-2{--scene-a:#e9b598;--scene-b:#a95842;--mountain:#6e473e}.location-card.scene-3{--scene-a:#bdd7dc;--scene-b:#728d87;--mountain:#496c63}.location-card.scene-4{--scene-a:#a8d9d6;--scene-b:#789a91;--mountain:#4b7068}.toast.error{background:#843c31}
@media(max-width:640px){.hero{padding-left:17px;padding-right:17px}.pill{font-size:0;padding:9px}.pill i{margin:0}.hero-copy .eyebrow{font-size:9px}.stat-grid{gap:7px}.stat{padding:13px 11px}.stat strong{font-size:21px}.stat small{font-size:10px}.field-grid{grid-template-columns:1fr}.field.span-2{grid-column:auto}.field-switch{margin-top:4px}.location-grid{grid-template-columns:1fr}.location-card{min-height:155px}.activity-toolbar{top:78px;margin-left:-3px;margin-right:-3px}.save-bar{padding-left:13px}.save-bar span b{display:none}.secondary-button{padding-left:8px;padding-right:8px}.metric-head,.metric-row{grid-template-columns:122px repeat(5,66px) 62px}}
@media(min-width:780px){.tabs{width:520px}.panel{padding-top:5px}.photo-card{display:grid;grid-template-columns:1.25fr .75fr;gap:18px;align-items:center}.photo-preview{grid-row:span 2;margin:0}.location-grid{grid-template-columns:repeat(3,1fr)}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

APP_JS = r"""
(() => {
  'use strict';
  const $ = (selector, root=document) => root.querySelector(selector);
  const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
  const metricNames = {temperature_f:'Temperature °F',precipitation_chance:'Precipitation %',snowpack_inches:'Snowpack in',uv_index:'UV index',wind_mph:'Wind mph',visibility_miles:'Visibility mi',air_quality_index:'Air quality'};
  let catalog=null, settings=null, baseline='', photoAvailable=false, token='';

  function acquireToken(){
    const url=new URL(location.href); const supplied=url.searchParams.get('token');
    let stored='';try{if(supplied)localStorage.setItem('eink-control-token',supplied);stored=localStorage.getItem('eink-control-token')||''}catch(_error){}
    if(supplied){url.searchParams.delete('token');history.replaceState(null,'',url.pathname+(url.searchParams.toString()?'?'+url.searchParams:''));}
    token=supplied||stored;
  }
  async function api(path, options={}){
    const headers=new Headers(options.headers||{}); if(token)headers.set('X-EInk-Control-Token',token);
    const response=await fetch(path,{...options,headers,cache:'no-store'});
    const type=response.headers.get('content-type')||''; const value=type.includes('application/json')?await response.json():await response.text();
    if(!response.ok)throw new Error(value.error||value||`Request failed (${response.status})`); return value;
  }
  function snapshot(){return JSON.stringify(settings)}
  function setDirty(){const dirty=snapshot()!==baseline;$('#save-bar').classList.toggle('hidden',!dirty)}
  function toast(message,isError=false){const el=$('#toast');el.textContent=message;el.classList.toggle('error',isError);el.classList.remove('hidden');setTimeout(()=>el.classList.add('hidden'),2600)}
  function switchTab(name){$$('.tab').forEach(b=>{const active=b.dataset.tab===name;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active))});$$('.panel').forEach(p=>p.classList.toggle('active',p.id===`panel-${name}`));scrollTo({top:0,behavior:'smooth'})}

  function bindBasics(){
    $('#location-name').value=settings.display.location_name; $('#units').value=settings.display.units; $('#display-caption').checked=settings.display.caption;
    $('#recommendation-count').value=settings.recommendation_count; $('#minimum-suitability').value=settings.minimum_suitability; updateSuitability();
    $('#photo-enabled').checked=settings.photo.enabled; $('#photo-caption').value=settings.photo.caption; $('#photo-rotation').value=settings.photo.rotation;
    $('#location-name').oninput=e=>{settings.display.location_name=e.target.value;setDirty()};
    $('#units').onchange=e=>{settings.display.units=e.target.value;setDirty()};
    $('#display-caption').onchange=e=>{settings.display.caption=e.target.checked;setDirty()};
    $('#recommendation-count').oninput=e=>{settings.recommendation_count=Number(e.target.value);setDirty()};
    $('#minimum-suitability').oninput=e=>{settings.minimum_suitability=Number(e.target.value);updateSuitability();setDirty()};
    $('#photo-enabled').onchange=e=>{settings.photo.enabled=e.target.checked;updateStats();setDirty()};
    $('#photo-caption').oninput=e=>{settings.photo.caption=e.target.value;setDirty()};
    $('#photo-rotation').onchange=e=>{settings.photo.rotation=Number(e.target.value);setDirty()};
  }
  function updateSuitability(){$('#suitability-output').textContent=`${Math.round(settings.minimum_suitability*100)}%`}
  function updateStats(){
    $('#location-count').textContent=settings.enabled_locations.length;
    $('#activity-count').textContent=settings.enabled_activities.length;
    $('#photo-state').textContent=settings.photo.enabled?'On':'Off';
    $('#activity-summary').textContent=`${settings.enabled_activities.length} of ${catalog.activities.length} activities enabled`;
  }
  function renderLocations(){
    const list=$('#location-list');list.replaceChildren();
    catalog.locations.forEach((location,index)=>{
      const selected=settings.enabled_locations.includes(location.id);
      const label=document.createElement('label');label.className=`location-card scene-${index%5} ${selected?'selected':''}`;
      label.innerHTML=`${location.has_artwork?'<span class="art-badge">ART READY</span>':''}<input type="checkbox" ${selected?'checked':''}><h3>${escapeHtml(location.name)}</h3><p>${escapeHtml(location.description)}</p>`;
      $('input',label).onchange=e=>{
        if(!e.target.checked&&settings.enabled_locations.length===1){e.target.checked=true;toast('Keep at least one location selected',true);return}
        settings.enabled_locations=e.target.checked?[...settings.enabled_locations,location.id]:settings.enabled_locations.filter(id=>id!==location.id);
        label.classList.toggle('selected',e.target.checked);updateStats();setDirty();
      };list.append(label);
    });
  }
  function baseActivity(id){return catalog.activities.find(item=>item.id===id)}
  function resolvedActivity(activity){
    const copy=JSON.parse(JSON.stringify(activity));const over=settings.activity_overrides[activity.id]||{};
    if(over.estimated_great_days!==undefined)copy.estimated_great_days=over.estimated_great_days;
    Object.entries(over.conditions||{}).forEach(([metric,fields])=>Object.assign(copy.conditions[metric],fields));return copy;
  }
  function setActivityValue(id,metric,field,value){
    const base=baseActivity(id);settings.activity_overrides[id]??={};
    if(metric===null){if(value===base.estimated_great_days)delete settings.activity_overrides[id].estimated_great_days;else settings.activity_overrides[id].estimated_great_days=value}
    else{settings.activity_overrides[id].conditions??={};settings.activity_overrides[id].conditions[metric]??={};if(value===base.conditions[metric][field])delete settings.activity_overrides[id].conditions[metric][field];else settings.activity_overrides[id].conditions[metric][field]=value;if(!Object.keys(settings.activity_overrides[id].conditions[metric]).length)delete settings.activity_overrides[id].conditions[metric];if(!Object.keys(settings.activity_overrides[id].conditions).length)delete settings.activity_overrides[id].conditions}
    if(!Object.keys(settings.activity_overrides[id]).length)delete settings.activity_overrides[id];setDirty();
  }
  function renderActivities(){
    const query=$('#activity-search').value.trim().toLowerCase();const list=$('#activity-list');list.replaceChildren();let shown=0;
    catalog.activities.forEach(activity=>{if(query&&!activity.name.toLowerCase().includes(query))return;shown++;const current=resolvedActivity(activity);const item=document.createElement('article');item.className='activity-item';
      const rows=Object.entries(current.conditions).map(([metric,range])=>`<div class="metric-row" data-metric="${metric}"><span class="metric-name">${escapeHtml(metricNames[metric]||metric)}</span>${['tolerable_min','ideal_min','ideal_max','tolerable_max','weight'].map(field=>`<input aria-label="${metric} ${field}" data-field="${field}" type="number" step="any" value="${range[field]}">`).join('')}<label class="metric-required"><input aria-label="${metric} required" data-field="required" type="checkbox" ${range.required?'checked':''}></label></div>`).join('');
      item.innerHTML=`<div class="activity-head"><input class="activity-toggle" type="checkbox" ${settings.enabled_activities.includes(activity.id)?'checked':''}><div class="activity-title"><b>${escapeHtml(activity.name)}</b><small>${current.estimated_great_days} great days / year${activity.toddler_friendly?' · family-friendly':''}</small></div>${activity.has_artwork?'<span class="art-chip">ART</span>':''}</div><details><summary>Tune ideal weather</summary><div class="activity-editor"><div class="days-row"><div><b>Estimated great days / year</b><p class="helper">A rarity estimate used for ranking, not a yearly quota.</p></div><label class="field"><input class="days-input" type="number" min="0" max="365" step="1" value="${current.estimated_great_days}"></label></div><div class="metric-table"><div class="metric-head"><span>Condition</span><span>Tol. min</span><span>Ideal min</span><span>Ideal max</span><span>Tol. max</span><span>Weight</span><span>Must</span></div>${rows}</div><button class="text-button restore-activity">Restore this activity</button></div></details>`;
      $('.activity-toggle',item).onchange=e=>{settings.enabled_activities=e.target.checked?[...settings.enabled_activities,activity.id]:settings.enabled_activities.filter(id=>id!==activity.id);updateStats();setDirty()};
      $('.days-input',item).onchange=e=>{const value=Math.max(0,Math.min(365,Number(e.target.value)));e.target.value=value;setActivityValue(activity.id,null,'estimated_great_days',value);$('.activity-title small',item).textContent=`${value} great days / year${activity.toddler_friendly?' · family-friendly':''}`};
      $$('.metric-row input',item).forEach(input=>input.onchange=e=>{const row=e.target.closest('.metric-row'),field=e.target.dataset.field,value=field==='required'?e.target.checked:Number(e.target.value);setActivityValue(activity.id,row.dataset.metric,field,value)});
      $('.restore-activity',item).onclick=()=>{delete settings.activity_overrides[activity.id];renderActivities();setDirty();toast(`${activity.name} restored`)};list.append(item);
    });$('#activity-empty').classList.toggle('hidden',shown!==0);updateStats();
  }
  function renderPhoto(){
    const preview=$('#photo-preview');if(photoAvailable){preview.className='photo-preview';preview.innerHTML=`<img alt="Uploaded frame photo" src="/api/photo?v=${Date.now()}">`}else{preview.className='photo-preview empty-preview';preview.innerHTML='<span>▣</span><p>No photo uploaded yet</p>'}
  }
  function escapeHtml(text){const node=document.createElement('span');node.textContent=text;return node.innerHTML}
  async function save(showToast=true){
    try{$('#save').disabled=true;settings=await api('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings)});baseline=snapshot();$('#save-bar').classList.add('hidden');if(showToast)toast('Saved to your frame')}
    catch(error){toast(error.message,true);throw error}finally{$('#save').disabled=false}
  }
  async function resetAll(){if(!confirm('Restore every location, activity, and display setting to its default? Your uploaded photo will not be deleted.'))return;try{settings=await api('/api/settings/reset',{method:'POST'});baseline=snapshot();renderAll();toast('Defaults restored')}catch(error){toast(error.message,true)}}
  async function uploadPhoto(file){
    if(!file)return;$('#photo-filename').textContent=file.name;const notice=$('#upload-progress');notice.textContent='Preparing and uploading photo…';notice.classList.remove('hidden','error');
    try{settings.photo.enabled=true;await save(false);const body=new FormData();body.append('photo',file);await api('/api/photo',{method:'POST',body});photoAvailable=true;renderPhoto();updateStats();notice.textContent='Photo uploaded. It is ready for the frame.';toast('Photo uploaded')}
    catch(error){notice.textContent=error.message;notice.classList.add('error');toast(error.message,true)}
  }
  function renderAll(){bindBasics();renderLocations();renderActivities();renderPhoto();updateStats();setDirty()}
  async function load(){
    $('#loading').classList.remove('hidden');$('#fatal').classList.add('hidden');
    try{const [cat,stored,health]=await Promise.all([api('/api/catalog'),api('/api/settings'),api('/healthz')]);catalog=cat;settings=stored;photoAvailable=Boolean(health.photo_available);baseline=snapshot();renderAll();$('#connection-pill').className='pill online';$('#connection-pill').innerHTML='<i></i>Online'}
    catch(error){$('#connection-pill').className='pill offline';$('#connection-pill').innerHTML='<i></i>Offline';$('#fatal').textContent=error.message;$('#fatal').classList.remove('hidden')}
    finally{$('#loading').classList.add('hidden')}
  }
  acquireToken();$$('.tab').forEach(button=>button.onclick=()=>switchTab(button.dataset.tab));$('#activity-search').oninput=renderActivities;
  $('#enable-all').onclick=()=>{settings.enabled_activities=catalog.activities.map(a=>a.id);renderActivities();setDirty()};$('#disable-all').onclick=()=>{settings.enabled_activities=[];renderActivities();setDirty()};
  $('#save').onclick=()=>save();$('#discard').onclick=()=>{settings=JSON.parse(baseline);renderAll()};$('#reset').onclick=resetAll;$('#refresh').onclick=load;$('#photo-file').onchange=e=>uploadPhoto(e.target.files[0]);
  addEventListener('beforeunload',event=>{if(settings&&snapshot()!==baseline){event.preventDefault();event.returnValue=''}});load();
})();
"""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _normalize_photo(payload: bytes, destination: Path) -> None:
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions are too large")
            opened.verify()
        with Image.open(io.BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                image.save(temporary, format="PNG", optimize=True)
                temporary.replace(destination)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("The uploaded file is not a valid supported image") from exc


def _discover_lan_host() -> str:
    """Find a phone-reachable address without transmitting any network traffic."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect only asks the kernel which interface it would use; it does
        # not contact this documentation-only address.
        probe.connect(("192.0.2.1", 80))
        address = probe.getsockname()[0]
        if address and not address.startswith("127.") and address != "0.0.0.0":
            return address
    except OSError:
        pass
    finally:
        probe.close()
    hostname = socket.gethostname().split(".", 1)[0].strip()
    if hostname:
        try:
            for _family, _type, _proto, _canonical, address in socket.getaddrinfo(
                hostname, None, socket.AF_INET
            ):
                candidate = address[0]
                if candidate and not candidate.startswith("127.") and candidate != "0.0.0.0":
                    return candidate
        except OSError:
            pass
    return f"{hostname}.local" if hostname else "localhost"


class _ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        store: SettingsStore,
        catalog: Catalog,
        photo_path: Path,
        max_bytes: int,
        callback: Callable[[Path], None] | None,
        access_token: str | None,
        max_connections: int,
        request_timeout: float,
    ):
        super().__init__(address, ControlHandler)
        self.store = store
        self.catalog = catalog
        self.photo_path = photo_path
        self.max_bytes = max_bytes
        self.callback = callback
        self.access_token = access_token
        self.request_timeout = request_timeout
        self._connection_slots = threading.BoundedSemaphore(max_connections)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, client_address

    def process_request(self, request, client_address) -> None:
        self._connection_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class ControlHandler(BaseHTTPRequestHandler):
    server: _ControlHTTPServer
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _reply(
        self,
        status: int,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True

    def _error(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        self._reply(status, _json_bytes({"error": message}), extra_headers=headers)

    def _authorized(self) -> bool:
        expected = self.server.access_token
        if expected is None:
            return True
        supplied = self.headers.get("X-EInk-Control-Token", "")
        authorization = self.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _require_mutation_auth(self) -> bool:
        if self._authorized():
            return True
        self._error(401, "A valid control-panel access token is required", {"WWW-Authenticate": "Bearer"})
        return False

    def _content_length(self, maximum: int) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            self._error(411, "Content-Length is required")
            return None
        try:
            length = int(raw)
        except ValueError:
            self._error(400, "Invalid Content-Length")
            return None
        if length <= 0:
            self._error(400, "Request body is empty")
            return None
        if length > maximum:
            self._error(413, "Request body is too large")
            return None
        return length

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._reply(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/app.css":
            self._reply(200, APP_CSS.encode("utf-8"), "text/css; charset=utf-8")
        elif path == "/app.js":
            self._reply(200, APP_JS.encode("utf-8"), "text/javascript; charset=utf-8")
        elif path == "/healthz":
            self._reply(
                200,
                _json_bytes(
                    {
                        "status": "ok",
                        "schema_version": 1,
                        "photo_available": self.server.photo_path.is_file(),
                    }
                ),
            )
        elif path == "/api/catalog":
            self._reply(200, _json_bytes(self.server.catalog.as_dict()))
        elif path == "/api/settings":
            self._reply(200, _json_bytes(self.server.store.load()))
        elif path == "/api/photo":
            try:
                payload = self.server.photo_path.read_bytes()
            except OSError:
                self._error(404, "No photo has been uploaded")
                return
            self._reply(200, payload, "image/png")
        else:
            self._error(404, "Not found")

    def do_PUT(self) -> None:
        # Mutation bodies are bounded but may be rejected before they are read. Closing
        # prevents unread bytes from being mistaken for the next keep-alive request.
        self.close_connection = True
        if urlsplit(self.path).path != "/api/settings":
            self._error(404, "Not found")
            return
        if not self._require_mutation_auth():
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "Expected application/json")
            return
        length = self._content_length(MAX_JSON_BYTES)
        if length is None:
            return
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            normalized = self.server.store.save(value)
        except (json.JSONDecodeError, UnicodeError):
            self._error(400, "Request body is not valid JSON")
            return
        except SettingsValidationError as exc:
            self._error(422, str(exc))
            return
        self._reply(200, _json_bytes(normalized))

    def do_POST(self) -> None:
        self.close_connection = True
        path = urlsplit(self.path).path
        if path not in ("/api/photo", "/api/settings/reset"):
            self._error(404, "Not found")
            return
        if not self._require_mutation_auth():
            return
        if path == "/api/settings/reset":
            length_header = self.headers.get("Content-Length")
            if length_header not in (None, "0"):
                self._error(400, "Reset does not accept a request body")
                return
            self._reply(200, _json_bytes(self.server.store.reset()))
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._error(415, "Expected multipart/form-data with a photo field")
            return
        length = self._content_length(self.server.max_bytes)
        if length is None:
            return
        raw = self.rfile.read(length)
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: "
                + content_type.encode("latin-1")
                + b"\r\nMIME-Version: 1.0\r\n\r\n"
                + raw
            )
        except (UnicodeError, ValueError):
            self._error(400, "Malformed multipart upload")
            return
        part = next(
            (
                item
                for item in message.walk()
                if item.get_content_disposition() == "form-data"
                and item.get_param("name", header="content-disposition") == "photo"
            ),
            None,
        )
        if part is None:
            self._error(400, "No photo field was received")
            return
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            self._error(400, "The photo field is empty")
            return
        try:
            _normalize_photo(payload, self.server.photo_path)
        except ValueError as exc:
            self._error(400, str(exc))
            return
        if self.server.callback is not None:
            try:
                self.server.callback(self.server.photo_path)
            except Exception:
                pass
        self._reply(
            201,
            _json_bytes(
                {
                    "status": "uploaded",
                    "path": self.server.photo_path.name,
                    "bytes": self.server.photo_path.stat().st_size,
                }
            ),
        )


class ControlServer:
    """Start/stop facade suitable for the simulator, Pi service, and tests."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        settings_path: Path | str | None = None,
        photo_path: Path | str | None = None,
        weather_repo: Path | str | None = None,
        max_bytes: int = 20 * 1024 * 1024,
        callback: Callable[[Path], None] | None = None,
        access_token: str | None = None,
        max_connections: int = 8,
        request_timeout: float = 15.0,
    ):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if access_token is not None and not access_token:
            raise ValueError("access_token cannot be empty")
        if max_connections <= 0:
            raise ValueError("max_connections must be positive")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        catalog = discover_catalog(weather_repo)
        resolved_settings = Path(settings_path).expanduser() if settings_path else default_settings_path()
        resolved_photo = (
            Path(photo_path).expanduser() if photo_path else default_photo_path(resolved_settings)
        )
        self.host = host
        self.settings_path = resolved_settings
        self.photo_path = resolved_photo
        self.catalog = catalog
        self.httpd = _ControlHTTPServer(
            (host, port),
            SettingsStore(resolved_settings, catalog),
            catalog,
            resolved_photo,
            max_bytes,
            callback,
            access_token,
            max_connections,
            request_timeout,
        )
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "::"):
            host = _discover_lan_host()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="eink-control-server", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


def _runtime_config_values(path: Path) -> tuple[Path | None, Path | None]:
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    repositories = value.get("repositories", {})
    sources = value.get("sources", {})
    weather_value = repositories.get("avian_weather", "") if isinstance(repositories, dict) else ""
    photo_value = sources.get("photo", "") if isinstance(sources, dict) else ""

    def resolve(raw: Any) -> Path | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else (path.parent / candidate).resolve()

    return resolve(weather_value), resolve(photo_value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Host the E-Ink frame's phone-friendly LAN configuration page"
    )
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    parser.add_argument("--settings", type=Path, help="control-panel JSON file")
    parser.add_argument("--photo", type=Path, help="fixed destination for uploaded photos")
    parser.add_argument("--weather-repo", type=Path, help="path to the AvianVisitors repository")
    parser.add_argument(
        "--runtime-config",
        type=Path,
        help="read repository and photo paths from an eink-display TOML config",
    )
    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument("--access-token", help="require this token for changes")
    token_group.add_argument(
        "--access-token-file", type=Path, help="read the mutation token from this file"
    )
    parser.add_argument(
        "--max-upload-mb", type=int, default=20, help="maximum multipart request size"
    )
    parser.add_argument("--max-connections", type=int, default=8)
    parser.add_argument("--request-timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    weather_repo = args.weather_repo
    photo_path = args.photo
    if args.runtime_config is not None:
        try:
            runtime_weather, runtime_photo = _runtime_config_values(args.runtime_config.expanduser())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            parser.error(f"cannot read --runtime-config: {exc}")
        weather_repo = weather_repo or runtime_weather
        photo_path = photo_path or runtime_photo
    access_token = args.access_token
    if args.access_token_file is not None:
        try:
            access_token = args.access_token_file.expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"cannot read --access-token-file: {exc}")
        if not access_token:
            parser.error("--access-token-file is empty")
    if args.max_upload_mb <= 0:
        parser.error("--max-upload-mb must be positive")
    if args.max_connections <= 0:
        parser.error("--max-connections must be positive")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be positive")
    try:
        server = ControlServer(
            args.host,
            args.port,
            settings_path=args.settings,
            photo_path=photo_path,
            weather_repo=weather_repo,
            max_bytes=args.max_upload_mb * 1024 * 1024,
            access_token=access_token,
            max_connections=args.max_connections,
            request_timeout=args.request_timeout,
        )
    except (OSError, ValueError, ImportError) as exc:
        parser.error(str(exc))
    server.start()
    print(f"Control panel: {server.url}")
    print(f"Settings: {server.settings_path}")
    print(f"Uploads: {server.photo_path}")
    if access_token:
        print("Access token required for changes (token not shown).")
    print("Press Ctrl-C to stop.")
    try:
        if server.thread is not None:
            server.thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
