let setupConfig = null;
let setupOpened = false;
let setupDismissed = false;
window.observatoryHasData = false;
const setupElement = id => document.getElementById(id);
const shellQuote = value => "'" + String(value).replace(/'/g, "'\\''") + "'";

function openAgentSetup() {
  setupOpened = true;
  setupDismissed = false;
  setupElement('agent-setup').hidden = false;
  setupElement('agent-setup').scrollIntoView({behavior:'smooth',block:'start'});
}

function updateSetupCommands() {
  const python = setupElement('setup-runtime').value === 'python';
  const endpoint = setupElement('setup-endpoint').value.trim() || location.origin;
  const service = setupElement('setup-service').value.trim() || 'my-agent';
  const target = setupElement('setup-target').value.trim() || 'agent.py';
  setupElement('setup-install-step').hidden = !python;
  setupElement('setup-launch-number').textContent = python ? '2' : '1';
  setupElement('setup-target-field').hidden = !python;
  setupElement('setup-capture-options').hidden = !python;
  setupElement('setup-launch-title').textContent = python ? 'Start your agent with automatic capture' : 'Configure your exporter';
  setupElement('setup-launch-help').textContent = python
    ? 'Use your agent’s Python environment. Replace its usual “python agent.py” command with this command; its arguments go at the end.'
    : 'Use these settings in an app that already exports OpenTelemetry. Choose OTLP over HTTP, then restart the app.';
  let command;
  if (python) {
    command = 'python -m observatory run --endpoint ' + shellQuote(endpoint) + ' --service ' + shellQuote(service);
    if (setupElement('setup-capture').checked) command += ' --capture-content';
    if (setupElement('setup-module').checked) command += ' --module';
    command += ' ' + shellQuote(target);
  } else {
    command = 'export OTEL_EXPORTER_OTLP_ENDPOINT=' + shellQuote(endpoint) + '\n'
      + 'export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf\n'
      + 'export OTEL_SERVICE_NAME=' + shellQuote(service);
  }
  setupElement('setup-launch-code').textContent = command;
  setupElement('setup-install-code').textContent = setupConfig?.install_command
    || 'Download the installer to get your install command.';
  setupElement('setup-runtime-note').textContent = python
    ? 'Captures OpenAI and Anthropic SDK calls, plus LangChain / LangGraph activity. OpenAI-compatible local servers work when called through the OpenAI Python SDK. Restart existing agents through this launcher.'
    : 'Your app needs an OpenTelemetry SDK and exporter. These settings send its existing traces, logs, and metrics to this receiver.';
}

async function downloadSetupPackage() {
  const button = setupElement('download-agent-package');
  button.disabled = true;
  try {
    if (!setupConfig) {
      setupConfig = await get('/api/setup');
      updateSetupCommands();
    }
    const response = await authorizedFetch('/api/package');
    if (!response.ok) throw new Error('HTTP '+response.status);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = setupConfig?.package_filename || 'agent-observatory.tar.gz';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    setupElement('setup-feedback').textContent = 'Downloaded. Run the install command from your download folder, in your agent’s Python environment.';
  } catch (error) {
    setupElement('setup-feedback').textContent = 'Download failed: '+error.message+'. Refresh the page and retry.';
  } finally {
    button.disabled = false;
  }
}

async function copySetupCommand(id, button) {
  const text = setupElement(id).textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = 'Copied';
    setupElement('setup-feedback').textContent = 'Command copied. Paste it into your agent’s terminal.';
    setTimeout(() => { button.textContent = 'Copy'; }, 1600);
  } catch {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(setupElement(id));
    selection.removeAllRanges();
    selection.addRange(range);
    setupElement('setup-feedback').textContent = 'Select and copy the highlighted command.';
  }
}

async function refreshAgentConnections(stats) {
  if (!setupConfig) {
    setupConfig = await get('/api/setup');
    setupElement('setup-auth-hint').hidden = !setupConfig.auth_required;
    updateSetupCommands();
  }
  const data = await get('/api/connections');
  const sources = data.connections;
  window.observatoryHasData = Boolean(stats.spans || stats.logs || stats.metric_points || stats.runs);
  setupElement('agent-setup').hidden = setupDismissed || (window.observatoryHasData && !setupOpened);
  setupElement('investigation').hidden = !window.observatoryHasData;
  setupElement('waiting-first-call').hidden = window.observatoryHasData;
  const active = sources.filter(source => source.state === 'running');
  setupElement('receiver-state').textContent = active.length
    ? 'Receiver listening · '+active.length+' agent'+(active.length === 1 ? '' : 's')+' connected'
    : 'Receiver listening · '+location.host;
  setupElement('waiting-title').textContent = active.length ? 'Agent connected. Waiting for a model call.' : 'Waiting for your first agent';
  setupElement('waiting-help').textContent = active.length
    ? 'Use your agent normally. Finished model calls appear here with their model, duration, token usage, and recorded errors.'
    : 'Install automatic capture in your agent’s environment, start it with the command above, and make a model call. This page updates automatically.';
  const host = setupElement('connected-agents');
  host.replaceChildren();
  for (const source of sources.slice(0,8)) {
    const card = document.createElement('div');
    card.className = 'connected-source' + (source.span_count ? ' receiving' : '') + (source.state === 'offline' ? ' offline' : '');
    const title = document.createElement('strong');
    title.textContent = source.service;
    const status = document.createElement('small');
    const local = source.integrations.includes('local-session-log');
    const state = local ? (source.state === 'running' ? 'Recent session activity' : 'Idle / last observed') : source.state === 'running' ? (source.span_count ? 'Receiving telemetry' : 'Connected · monitoring enabled')
      : source.state === 'stopped' ? 'Finished' : 'Disconnected';
    status.textContent = state+' · '+source.span_count+' spans received';
    const details = document.createElement('small');
    details.textContent = source.integrations.join(', ') + (source.capture_content ? ' · content included' : ' · metadata only');
    card.append(title,status,details);
    host.append(card);
  }
}

setupElement('connect-agent').onclick = openAgentSetup;
setupElement('download-agent-package').onclick = downloadSetupPackage;
setupElement('close-agent-setup').onclick = () => {
  setupDismissed = true;
  setupOpened = false;
  setupElement('agent-setup').hidden = true;
};
setupElement('setup-endpoint').value = location.origin;
for (const id of ['setup-runtime','setup-endpoint','setup-service','setup-target','setup-capture','setup-module']) {
  setupElement(id).addEventListener('input', updateSetupCommands);
  setupElement(id).addEventListener('change', updateSetupCommands);
}
for (const button of document.querySelectorAll('[data-copy-command]')) {
  button.onclick = () => copySetupCommand(button.dataset.copyCommand, button);
}
updateSetupCommands();
