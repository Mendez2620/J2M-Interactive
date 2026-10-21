(() => {
  const root = document.querySelector('[data-web-scanner]');
  if (!root || !window.J2MScannerState) return;
  const state = new window.J2MScannerState.ScannerState();
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const status = root.querySelector('[data-scanner-status]');
  const cameraPanel = root.querySelector('[data-camera-panel]');
  const cameraSelect = root.querySelector('[data-camera-select]');
  const actionButtons = [...root.querySelectorAll('[data-action]')];
  let scanner = null;
  let scanning = false;
  let cameraLookupLocked = false;
  let cameraGeneration = 0;

  function message(text, type = 'info') {
    status.textContent = text;
    status.className = `rounded-xl p-3 text-sm ${type === 'error' ? 'bg-rose-50 text-rose-800' : type === 'success' ? 'bg-emerald-50 text-emerald-800' : 'bg-indigo-50 text-indigo-800'}`;
  }
  async function responseJson(response) { try { return await response.json(); } catch (_error) { return null; } }
  function requireReauthentication(response, data) {
    if (response.status !== 401 && !(response.status === 400 && data?.message?.includes('CSRF'))) return false;
    const login = window.location.pathname.startsWith('/manager/') ? '/manager/login' : '/staff/login';
    window.location.assign(`${login}?csrf_error=1`);
    return true;
  }
  function requestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
  }
  function updateControls() {
    const selected = state.phase === 'selected';
    const uncertain = state.phase === 'uncertain';
    actionButtons.forEach(button => { button.disabled = !(selected || (uncertain && button.dataset.action === state.pendingOperation?.action)); });
    root.querySelector('[data-lookup-manual]').disabled = ['lookup', 'processing', 'uncertain'].includes(state.phase);
    root.querySelector('[data-start-camera]').disabled = ['lookup', 'processing', 'uncertain'].includes(state.phase);
    root.querySelector('[data-reset-scanner]').disabled = ['processing', 'uncertain'].includes(state.phase);
    root.querySelectorAll('[data-purchase-amount],[data-redeem-amount]').forEach(input => { input.disabled = ['processing', 'uncertain'].includes(state.phase); });
  }
  function clearSelection() {
    root.querySelector('[data-customer-panel]').classList.add('hidden');
    root.querySelector('[data-customer-name]').textContent = '';
    root.querySelector('[data-customer-balance]').textContent = '';
    updateControls();
  }
  async function stopCamera() {
    cameraGeneration += 1;
    cameraLookupLocked = false;
    if (scanner && scanning) { try { await scanner.stop(); } catch (_error) {} }
    scanning = false;
    cameraPanel.classList.add('hidden');
  }
  function cameraError(error) {
    const name = error?.name || '';
    if (name === 'NotAllowedError') return 'Permiso de cámara denegado. Puedes usar el ingreso alternativo.';
    if (name === 'NotFoundError') return 'No se encontró una cámara disponible.';
    if (name === 'NotReadableError') return 'La cámara está ocupada por otra aplicación.';
    return 'No fue posible iniciar la cámara en este navegador.';
  }
  async function startCamera() {
    if (['lookup', 'processing', 'uncertain'].includes(state.phase)) { message('Espera a que termine la validación o resuelve el movimiento pendiente.', 'error'); return; }
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.Html5Qrcode) { message('La cámara requiere HTTPS y un navegador compatible. Usa el ingreso alternativo.', 'error'); return; }
    root.querySelector('[data-start-camera]').disabled = true;
    message('Solicitando acceso a la cámara…');
    const generation = ++cameraGeneration;
    try {
      const cameras = await Html5Qrcode.getCameras();
      cameraSelect.replaceChildren();
      cameras.forEach((camera, index) => { const option = document.createElement('option'); option.value = camera.id; option.textContent = camera.label || `Cámara ${index + 1}`; cameraSelect.appendChild(option); });
      if (!cameras.length) throw Object.assign(new Error('camera missing'), {name:'NotFoundError'});
      cameraPanel.classList.remove('hidden');
      scanner ||= new Html5Qrcode(root.querySelector('[data-qr-reader]').id);
      scanning = true;
      await scanner.start(cameraSelect.value || {facingMode:'environment'}, {fps:10, qrbox:{width:240,height:240}}, decoded => cameraDecoded(decoded, generation), () => {});
      if (generation !== cameraGeneration) { try { await scanner.stop(); } catch (_error) {} return; }
      message('Cámara activa. Coloca el QR dentro del recuadro.', 'success');
    } catch (error) { await stopCamera(); message(cameraError(error), 'error'); }
    finally { updateControls(); }
  }
  async function restartCamera() { await stopCamera(); await startCamera(); }
  function cameraDecoded(decoded, generation) {
    if (generation !== cameraGeneration || cameraLookupLocked || state.phase === 'lookup' || state.phase === 'processing' || state.phase === 'uncertain') return;
    cameraLookupLocked = true;
    if (scanner && scanning && typeof scanner.pause === 'function') { try { scanner.pause(true); } catch (_error) {} }
    lookup(decoded, generation);
  }
  function resumeCameraAfterLookup(generation) {
    if (generation !== cameraGeneration || !scanner || !scanning) return;
    cameraLookupLocked = false;
    if (typeof scanner.resume === 'function') { try { scanner.resume(); } catch (_error) {} }
  }
  function renderSelection() {
    const selection = state.selection;
    if (!selection) { clearSelection(); return; }
    const {customer, business} = selection;
    root.querySelector('[data-customer-name]').textContent = customer.full_name || '';
    root.querySelector('[data-customer-balance]').textContent = business.loyalty_type === 'stamps' ? `${customer.current_stamps}/${business.stamps_reward_limit} sellos` : `${customer.current_points} puntos`;
    root.querySelector('[data-stamps-actions]').classList.toggle('hidden', business.loyalty_type !== 'stamps');
    root.querySelector('[data-points-actions]').classList.toggle('hidden', business.loyalty_type !== 'points');
    root.querySelector('[data-customer-panel]').classList.remove('hidden');
    updateControls();
  }
  async function lookup(rawToken, cameraSourceGeneration = null) {
    const token = String(rawToken || '').trim();
    if (!token) return;
    const version = state.beginLookup();
    if (version === null) { message('Resuelve primero el movimiento pendiente antes de cambiar de cliente.', 'error'); return; }
    clearSelection();
    message('Validando el código…');
    try {
      const response = await fetch('/api/web-scanner/lookup', {method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRFToken':csrf}, body:JSON.stringify({token})});
      const data = await responseJson(response);
      if (requireReauthentication(response, data)) return;
      if (version !== state.lookupVersion) return;
      if (!response.ok || !data?.customer || !data?.business) {
        state.finishLookup(version, null); clearSelection();
        message(data?.message || 'No fue posible validar el código.', 'error');
        if (cameraSourceGeneration !== null) resumeCameraAfterLookup(cameraSourceGeneration);
        return;
      }
      state.finishLookup(version, {token, customer:data.customer, business:data.business});
      root.querySelector('[data-manual-token]').value = '';
      await stopCamera(); renderSelection();
      message('Cliente encontrado. Selecciona y confirma una operación.', 'success');
    } catch (_error) {
      if (version !== state.lookupVersion) return;
      state.finishLookup(version, null); clearSelection();
      message('No fue posible validar el código. No hay ningún cliente seleccionado.', 'error');
      if (cameraSourceGeneration !== null) resumeCameraAfterLookup(cameraSourceGeneration);
    }
  }
  function balanceText(customer, business = state.selection?.business) { return business?.loyalty_type === 'stamps' ? `${customer.current_stamps} sellos` : `${customer.current_points} puntos`; }
  function operationFor(action) {
    if (!state.selection) return null;
    const payload = {token:state.selection.token, action};
    if (action === 'earn_points') payload.purchase_amount = root.querySelector('[data-purchase-amount]').value;
    if (action === 'redeem_points') payload.amount = root.querySelector('[data-redeem-amount]').value;
    return {action, signature:JSON.stringify(payload)};
  }
  async function process(action) {
    const candidate = operationFor(action);
    if (!candidate) { message('Consulta y confirma primero un cliente válido.', 'error'); return; }
    const operation = state.beginOperation({...candidate, id:requestId(), before:balanceText(state.selection.customer)});
    if (!operation) { message('Existe un resultado pendiente. Solo puedes reintentar exactamente el mismo movimiento.', 'error'); return; }
    updateControls(); message('Confirmando movimiento en J2M…');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/web-scanner/process', {method:'POST', signal:controller.signal, headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRFToken':csrf,'Idempotency-Key':operation.id}, body:operation.signature});
      const data = await responseJson(response);
      if (requireReauthentication(response, data)) return;
      if (!response.ok) {
        if (response.status >= 500) {
          state.finishOperation('ambiguous');
          message('El resultado aún no puede confirmarse. Reintenta el mismo movimiento; se conservará su identificador.', 'error');
        } else {
          state.finishOperation('definitive');
          message(data?.message || 'El movimiento fue rechazado.', 'error');
        }
        return;
      }
      if (!data?.customer) {
        state.finishOperation('ambiguous');
        message('La respuesta no permitió confirmar el saldo. Reintenta el mismo movimiento.', 'error');
        return;
      }
      state.finishOperation('success', data.customer);
      renderSelection();
      const wallet = data.wallet_sync?.google_status === 'pending' ? ' El movimiento está confirmado en J2M; Google Wallet tiene una actualización pendiente.' : '';
      message(`Movimiento confirmado. Saldo anterior: ${operation.before}. Saldo resultante: ${balanceText(state.selection.customer)}.${wallet}`, 'success');
    } catch (_error) {
      state.finishOperation('ambiguous');
      message('Resultado incierto por pérdida de conexión. Reintenta el mismo movimiento para confirmar, sin duplicarlo.', 'error');
    } finally { clearTimeout(timeout); updateControls(); }
  }
  function reset() {
    if (!state.reset()) { message('No puedes cambiar de cliente mientras un movimiento tenga resultado incierto.', 'error'); return; }
    clearSelection();
    root.querySelector('[data-purchase-amount]').value = '';
    root.querySelector('[data-redeem-amount]').value = '';
    root.querySelector('[data-manual-token]').value = '';
    message('Listo para escanear otro cliente.');
  }

  root.querySelector('[data-start-camera]').addEventListener('click', startCamera);
  root.querySelector('[data-stop-camera]').addEventListener('click', stopCamera);
  cameraSelect.addEventListener('change', restartCamera);
  root.querySelector('[data-lookup-manual]').addEventListener('click', () => lookup(root.querySelector('[data-manual-token]').value));
  actionButtons.forEach(button => button.addEventListener('click', () => process(button.dataset.action)));
  root.querySelector('[data-reset-scanner]').addEventListener('click', reset);
  window.addEventListener('pagehide', stopCamera);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopCamera(); });
  updateControls();
})();
