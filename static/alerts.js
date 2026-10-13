'use strict';

let alertSettingsContext = null;

function resetAlertSettings() { alertSettingsContext = null; }

function alertSettingsPanel() {
  return `<section class="panel settings-panel alert-settings"><form id="alert-form">
    <div class="settings-section"><h3>后台告警通知</h3><p>资源超过阈值时，由服务器发送通知。关闭网页后仍会检测；配置对整台服务器生效。</p>
      <div class="form-message info" id="alert-feedback" role="status">正在读取告警配置…</div><button class="button" id="alert-retry" type="button" hidden>重新加载配置</button>
      <fieldset id="alert-fields" disabled hidden><label class="alert-check alert-master"><input type="checkbox" id="alert-enabled"><span><strong>开启告警推送</strong><small>保存后生效，需要先配置通知渠道。</small></span></label>
      <h4 class="alert-section-title">通知渠道</h4><div class="alert-channel-grid"><div><label class="form-label" for="alert-channel">接收方式</label><select class="form-input" id="alert-channel"><option value="wecom">企业微信群机器人</option><option value="serverchan">个人微信 · Server酱 Turbo</option><option value="webhook">通用 Webhook</option></select></div><div><label class="form-label" for="alert-credential" id="alert-credential-label">机器人 Webhook</label><input class="form-input" id="alert-credential" type="password" autocomplete="new-password" maxlength="2048" spellcheck="false" aria-describedby="alert-credential-status alert-channel-help"><span id="alert-credential-status" class="alert-help">未配置</span></div></div>
      <p class="alert-help" id="alert-channel-help"></p><label class="alert-check"><input type="checkbox" id="alert-clear-credential"><span>删除已保存的渠道凭据（需同时关闭推送）</span></label>
      <h4 class="alert-section-title">资源阈值</h4><p class="alert-help">严格超过阈值才触发。磁盘按每个挂载点独立检测；关闭单项开关可停止该项推送。</p>
      <div class="alert-rule-grid">${[['cpu','CPU 使用率',80],['memory','内存使用率',90],['disk','磁盘使用率',90]].map(([key,label,threshold])=>`<div class="alert-rule"><label class="alert-check"><input type="checkbox" id="alert-${key}-enabled" checked><strong>${label}</strong></label><label class="form-label" for="alert-${key}-threshold">超限阈值（%）</label><input class="form-input" id="alert-${key}-threshold" type="number" min="1" max="100" step="0.1" value="${threshold}" required></div>`).join('')}</div>
      <div class="alert-timing-grid"><div><label class="form-label" for="alert-duration">持续超限时间（秒）</label><input class="form-input" id="alert-duration" type="number" min="0" max="3600" step="1" value="60" required><p class="alert-help">默认 60 秒，设为 0 则立即告警。恢复正常后重新计时。</p></div><div><label class="form-label" for="alert-repeat">重复通知间隔（秒）</label><input class="form-input" id="alert-repeat" type="number" min="60" max="86400" step="1" value="900" required><p class="alert-help">默认 900 秒（15 分钟）。发送失败也按此间隔重试。</p></div></div></fieldset>
      <div class="alert-actions" id="alert-actions" hidden><button class="button button-primary" id="alert-save" type="submit" disabled>保存配置</button><button class="button" id="alert-test" type="button" disabled>发送测试通知</button><span class="alert-help" id="alert-save-note">测试通知使用已保存的渠道。</span></div>
    </div></form>
    <div class="settings-section"><div class="alert-history-heading"><div><h3>告警状态与发送记录</h3><p>保留最近 100 条发送结果，重启后仍可查看。</p></div><button class="button" id="alert-history-refresh" type="button" disabled>刷新记录</button></div><div id="alert-active" class="alert-help"></div><div class="table-scroll"><table class="alert-history-table"><thead><tr><th>发送时间</th><th>通知内容</th><th>渠道</th><th>结果</th></tr></thead><tbody id="alert-history"><tr><td colspan="4"><div class="empty-state">正在读取发送记录…</div></td></tr></tbody></table></div></div>
  </section>`;
}

function bindAlertSettings() {
  const root = $('#alert-form');
  if(!root) return;
  const context = {root, userId:state.user.id, config:null, busy:false, dirty:false};
  alertSettingsContext = context;
  const current = ()=>alertSettingsContext===context && state.user?.id===context.userId && state.user?.role==='admin' && root.isConnected;
  const feedback = (message, error=false)=>{
    if(!current())return;
    const target=$('#alert-feedback');target.textContent=message;target.className=`form-message ${error?'error':'info'}`;
  };
  const buttons = ()=>{
    if(!current())return;
    $('#alert-fields').disabled=context.busy || !context.config;
    $('#alert-fields').hidden=!context.config;
    $('#alert-actions').hidden=!context.config;
    $('#alert-save').disabled=context.busy || !context.config;
    $('#alert-test').disabled=context.busy || !context.config?.credential_configured || context.dirty;
    $('#alert-history-refresh').disabled=context.busy || !context.config;
    $('#alert-save-note').textContent=context.dirty?'有未保存的修改，请先保存再测试通知。':'测试通知使用已保存的渠道，关闭推送时也可测试。';
  };
  const channelHelp = ()=>{
    const channel=$('#alert-channel').value;
    const configured=context.config?.channel===channel && context.config.credential_configured;
    const guidance={
      wecom:['机器人 Webhook','在企业微信群中添加群机器人，复制 Webhook 地址。消息会发送到该企业微信群。'],
      serverchan:['SendKey','在 Server酱 Turbo 获取 SCT 开头的 SendKey，并按该服务指引绑定微信接收渠道。接收方式与额度由第三方服务提供。'],
      webhook:['HTTPS Webhook 地址','填写接收通知的 HTTPS 地址，服务器将以 POST 发送包含指标、阈值与时间的 JSON。']
    }[channel];
    $('#alert-credential-label').textContent=guidance[0];
    $('#alert-channel-help').textContent=guidance[1];
    $('#alert-credential-status').textContent=configured?'已配置 · 留空保留，填写新值替换。':'未配置 · 请输入渠道凭据并保存。';
    $('#alert-credential').placeholder=configured?'留空保留现有凭据':channel==='serverchan'?'SCT 开头的 SendKey':'https://…';
  };
  const fill = result=>{
    context.config=result.config;context.dirty=false;
    $('#alert-enabled').checked=result.config.enabled;
    $('#alert-channel').value=result.config.channel;
    $('#alert-credential').value='';$('#alert-clear-credential').checked=false;
    $('#alert-duration').value=result.config.duration_seconds;
    $('#alert-repeat').value=result.config.repeat_seconds;
    for(const metric of ['cpu','memory','disk']) {
      $(`#alert-${metric}-enabled`).checked=result.config.rules[metric].enabled;
      $(`#alert-${metric}-threshold`).value=result.config.rules[metric].threshold;
    }
    channelHelp();renderAlertRecords(result);
  };
  const load = async()=>{
    context.busy=true;buttons();$('#alert-retry').hidden=true;
    try {
      const result=await apiRequest('/api/admin/alerts');
      if(!current())return;
      fill(result);feedback('配置保存在服务器中，仅管理员可修改。');
    } catch(error) {
      if(current()){
        feedback(error.status===404?'告警接口尚未启用，请更新并重启 MoniLite 服务，然后重新加载配置。':`告警配置加载失败：${error.message}`,true);
        $('#alert-retry').hidden=false;
        if(!context.config)$('#alert-history').innerHTML='<tr><td colspan="4"><div class="empty-state">配置加载失败，请重新加载后查看发送记录。</div></td></tr>';
      }
    }
    finally {context.busy=false;buttons();}
  };
  const refreshRecords = async()=>{
    const result=await apiRequest('/api/admin/alerts');
    if(current())renderAlertRecords(result);
  };
  root.oninput=root.onchange=()=>{
    context.dirty=true;channelHelp();buttons();
  };
  $('#alert-channel').onchange=()=>{
    $('#alert-credential').value='';$('#alert-clear-credential').checked=false;
    context.dirty=true;channelHelp();buttons();
  };
  root.onsubmit=async event=>{
    event.preventDefault();if(context.busy || !context.config)return;
    const rules={};
    for(const metric of ['cpu','memory','disk'])rules[metric]={enabled:$(`#alert-${metric}-enabled`).checked,threshold:Number($(`#alert-${metric}-threshold`).value)};
    const body={enabled:$('#alert-enabled').checked,channel:$('#alert-channel').value,credential:$('#alert-credential').value,
      clear_credential:$('#alert-clear-credential').checked,duration_seconds:Number($('#alert-duration').value),repeat_seconds:Number($('#alert-repeat').value),rules};
    context.busy=true;buttons();feedback('正在保存…');
    try {
      const result=await apiRequest('/api/admin/alerts',{method:'PATCH',body});
      if(current()){fill(result);feedback('告警配置已保存。');toast('告警配置已保存');}
    } catch(error) {feedback(error.message,true);}
    finally {context.busy=false;buttons();}
  };
  $('#alert-test').onclick=async()=>{
    if(context.busy || context.dirty)return;
    context.busy=true;buttons();feedback('正在发送测试通知…');
    try {
      await apiRequest('/api/admin/alerts/test',{method:'POST',body:{}});
      feedback('测试通知发送成功，请到接收渠道查看。');
    } catch(error) {feedback(error.message,true);}
    finally {
      if(current()){
        try {await refreshRecords();} catch(error) {feedback(`发送完成，但记录刷新失败：${error.message}`,true);}
      }
      context.busy=false;buttons();
    }
  };
  $('#alert-history-refresh').onclick=async()=>{
    context.busy=true;buttons();
    try {await refreshRecords();} catch(error) {feedback(error.message,true);}
    finally {context.busy=false;buttons();}
  };
  $('#alert-retry').onclick=load;
  load();
}

function renderAlertRecords(result) {
  const channels={wecom:'企业微信',serverchan:'Server酱',webhook:'Webhook'};
  $('#alert-active').textContent=result.active.length?`当前 ${result.active.length} 项指标超过阈值：${result.active.map(event=>`${({cpu:'CPU',memory:'内存',disk:'磁盘'})[event.metric]}${event.mount?' '+event.mount:''} ${number(event.value)}%（持续 ${event.duration_seconds} 秒）`).join('；')}`:result.config.enabled?'当前没有超限指标。':'后台告警推送已关闭。';
  $('#alert-history').innerHTML=result.history.length?result.history.map(row=>`<tr><td>${esc(dateLabel(row.timestamp))}</td><td><strong>${esc(row.event.title)}</strong><span class="alert-record-detail">${row.event.kind==='test'?'测试渠道连接':`${esc(row.event.hostname)} · ${number(row.event.value)}% / 阈值 ${number(row.event.threshold)}% · 持续 ${row.event.duration_seconds} 秒`}</span></td><td>${esc(channels[row.channel]||row.channel)}</td><td><span class="status-badge ${row.success?'':'warning'}">${row.success?'发送成功':'发送失败'}</span>${row.error?`<span class="alert-record-detail">${esc(row.error)}</span>`:''}</td></tr>`).join(''):'<tr><td colspan="4"><div class="empty-state">尚无发送记录。保存渠道后，可发送一条测试通知。</div></td></tr>';
}
