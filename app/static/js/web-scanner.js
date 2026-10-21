(() => {
  const root = document.querySelector('[data-web-scanner]');
  if (!root || !window.J2MScannerState) return;
  const state = new window.J2MScannerState.ScannerState();
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const status = root.querySelector('[data-scanner-status]');
  const startPanel = root.querySelector('[data-start-panel]');
  const cameraPanel = root.querySelector('[data-camera-panel]');
  const cameraSelect = root.querySelector('[data-camera-select]');
  const cameraSelectWrap = root.querySelector('[data-camera-select-wrap]');
  const customerPanel = root.querySelector('[data-customer-panel]');
  const confirmPanel = root.querySelector('[data-confirm-panel]');
  const successPanel = root.querySelector('[data-success-panel]');
  const actionButtons = [...root.querySelectorAll('[data-action]')];
  const mobileCamera = (() => {
    if (navigator.userAgentData?.mobile === true) return true;
    const userAgent = navigator.userAgent || '';
    if (/Android|iPhone|iPad|iPod/i.test(userAgent)) return true;
    return navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1;
  })();
  let scanner = null;
  let scanning = false;
  let cameraLookupLocked = false;
  let cameraGeneration = 0;
  let preferredCameraId = null;
  let pendingConfirmation = null;

  function publishPhase() {
    root.dataset.scannerPhase = state.phase;
    if (typeof window.CustomEvent === 'function') window.dispatchEvent(new window.CustomEvent('j2m:scanner-phase', {detail:{phase:state.phase}}));
  }
  function message(text, type = 'info') {
    status.textContent = text;
    status.className = `rounded-2xl p-4 text-sm ${type === 'error' ? 'border border-rose-200 bg-rose-50 text-rose-800' : type === 'success' ? 'border border-emerald-200 bg-emerald-50 text-emerald-800' : 'border border-indigo-200 bg-indigo-50 text-indigo-800'}`;
  }
  function hideMessage() { status.classList.add('hidden'); status.textContent = ''; }
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
    root.querySelector('[data-start-camera]').disabled = ['lookup', 'processing', 'uncertain'].includes(state.phase);
    root.querySelector('[data-reset-scanner]').disabled = ['processing', 'uncertain'].includes(state.phase);
    root.querySelector('[data-confirm-operation]').disabled = state.phase === 'processing';
    root.querySelectorAll('[data-purchase-amount],[data-redeem-amount]').forEach(input => { input.disabled = ['processing', 'uncertain'].includes(state.phase); });
    publishPhase();
  }
  function clearSelection() {
    customerPanel.classList.add('hidden');
    confirmPanel.classList.add('hidden');
    successPanel.classList.add('hidden');
    root.querySelector('[data-customer-name]').textContent = '';
    root.querySelector('[data-customer-balance]').textContent = '';
    root.querySelector('[data-customer-program]').textContent = '';
    pendingConfirmation = null;
    updateControls();
  }
  async function stopCamera({showStart = true} = {}) {
    cameraGeneration += 1;
    cameraLookupLocked = false;
    const activeScanner = scanner;
    scanner = null;
    scanning = false;
    cameraPanel.classList.add('hidden');
    cameraSelectWrap.classList.add('hidden');
    if (showStart && state.phase === 'idle') startPanel.classList.remove('hidden');
    if (activeScanner) { try { await activeScanner.stop(); } catch (_error) {} }
  }
  function cameraError(error) {
    const name = error?.name || '';
    const detail = String(error?.message || error || '').toLowerCase();
    if (name === 'NotAllowedError' || detail.includes('permission')) return 'No se concedió acceso a la cámara. Revisa el permiso del navegador y vuelve a intentarlo.';
    if (name === 'NotFoundError' || name === 'OverconstrainedError' || detail.includes('notfound') || detail.includes('overconstrained')) return mobileCamera ? 'No se encontró una cámara trasera compatible en este dispositivo.' : 'No se encontró una cámara disponible.';
    if (name === 'NotReadableError') return 'La cámara está siendo utilizada por otra aplicación.';
    return 'No fue posible iniciar la cámara. Comprueba el permiso y vuelve a intentarlo.';
  }
  async function startCamera(requestedCameraId = null) {
    if (['lookup', 'processing', 'uncertain'].includes(state.phase)) { message('Termina la validación o resuelve el movimiento pendiente antes de escanear.', 'error'); return; }
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.Html5Qrcode) { message('La cámara requiere una conexión HTTPS y un navegador compatible.', 'error'); return; }
    root.querySelector('[data-start-camera]').disabled = true;
    startPanel.classList.add('hidden');
    cameraPanel.classList.remove('hidden');
    message('Preparando la cámara…');
    const generation = ++cameraGeneration;
    let attemptScanner = null;
    try {
      let source;
      if (mobileCamera) {
        source = {facingMode:{exact:'environment'}};
      } else {
        const cameras = await Html5Qrcode.getCameras();
        if (generation !== cameraGeneration) return;
        if (!cameras.length) throw Object.assign(new Error('camera missing'), {name:'NotFoundError'});
        cameraSelect.replaceChildren();
        cameras.forEach((camera, index) => { const option = document.createElement('option'); option.value = camera.id; option.textContent = camera.label || `Cámara ${index + 1}`; cameraSelect.appendChild(option); });
        cameraSelectWrap.classList.toggle('hidden', cameras.length < 2);
        const availableIds = new Set(cameras.map(camera => camera.id));
        source = [requestedCameraId, preferredCameraId].find(id => id && availableIds.has(id)) || cameras[0].id;
        preferredCameraId = source;
        cameraSelect.value = source;
      }
      if (generation !== cameraGeneration) return;
      attemptScanner = new Html5Qrcode(root.querySelector('[data-qr-reader]').id);
      scanner = attemptScanner;
      scanning = true;
      await attemptScanner.start(source, {fps:10, qrbox:{width:240,height:240}, disableFlip:mobileCamera}, decoded => cameraDecoded(decoded, generation), () => {});
      if (generation !== cameraGeneration || scanner !== attemptScanner) { try { await attemptScanner.stop(); } catch (_error) {} return; }
      message('Cámara activa. Centra el código QR dentro del recuadro.', 'success');
    } catch (error) {
      if (generation !== cameraGeneration) { if (attemptScanner) { try { await attemptScanner.stop(); } catch (_error) {} } return; }
      await stopCamera();
      message(cameraError(error), 'error');
    } finally { updateControls(); }
  }
  async function restartCamera() {
    if (mobileCamera || ['lookup', 'processing', 'uncertain'].includes(state.phase)) return;
    preferredCameraId = cameraSelect.value || preferredCameraId;
    await stopCamera({showStart:false});
    await startCamera(preferredCameraId);
  }
  function cameraDecoded(decoded, generation) {
    if (generation !== cameraGeneration || cameraLookupLocked || ['lookup', 'processing', 'uncertain'].includes(state.phase)) return;
    cameraLookupLocked = true;
    if (scanner && scanning && typeof scanner.pause === 'function') { try { scanner.pause(true); } catch (_error) {} }
    lookup(decoded, generation);
  }
  function resumeCameraAfterLookup(generation) {
    if (generation !== cameraGeneration || !scanner || !scanning) return;
    cameraLookupLocked = false;
    if (typeof scanner.resume === 'function') { try { scanner.resume(); } catch (_error) {} }
  }
  function balanceText(customer, business = state.selection?.business) {
    return business?.loyalty_type === 'stamps' ? `${customer.current_stamps}/${business.stamps_reward_limit} sellos` : `${customer.current_points} puntos`;
  }
  function renderSelection() {
    const selection = state.selection;
    if (!selection) { clearSelection(); return; }
    const {customer, business} = selection;
    root.querySelector('[data-customer-name]').textContent = customer.full_name || '';
    root.querySelector('[data-customer-balance]').textContent = balanceText(customer, business);
    root.querySelector('[data-customer-program]').textContent = business.loyalty_type === 'stamps' ? `Programa de ${business.stamps_reward_limit} sellos` : 'Programa de puntos';
    root.querySelector('[data-stamps-actions]').classList.toggle('hidden', business.loyalty_type !== 'stamps');
    root.querySelector('[data-points-actions]').classList.toggle('hidden', business.loyalty_type !== 'points');
    customerPanel.classList.remove('hidden');
    successPanel.classList.add('hidden');
    updateControls();
  }
  async function lookup(rawToken, cameraSourceGeneration = null) {
    const token = String(rawToken || '').trim();
    if (!token) return;
    const version = state.beginLookup();
    if (version === null) { message('Resuelve primero el movimiento pendiente antes de cambiar de cliente.', 'error'); return; }
    clearSelection();
    message('Validando cliente…');
    try {
      const response = await fetch('/api/web-scanner/lookup', {method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRFToken':csrf}, body:JSON.stringify({token})});
      const data = await responseJson(response);
      if (requireReauthentication(response, data)) return;
      if (version !== state.lookupVersion) return;
      if (!response.ok || !data?.customer || !data?.business) {
        state.finishLookup(version, null); clearSelection();
        message(data?.message || 'No fue posible reconocer esa tarjeta. Intenta escanearla nuevamente.', 'error');
        if (cameraSourceGeneration !== null) resumeCameraAfterLookup(cameraSourceGeneration);
        return;
      }
      state.finishLookup(version, {token, customer:data.customer, business:data.business});
      await stopCamera({showStart:false});
      renderSelection();
      message('Cliente confirmado. Selecciona el movimiento que deseas realizar.', 'success');
    } catch (_error) {
      if (version !== state.lookupVersion) return;
      state.finishLookup(version, null); clearSelection();
      message('No fue posible validar la tarjeta. Comprueba la conexión e inténtalo nuevamente.', 'error');
      if (cameraSourceGeneration !== null) resumeCameraAfterLookup(cameraSourceGeneration);
    }
  }
  function operationFor(action) {
    if (!state.selection) return null;
    const payload = {token:state.selection.token, action};
    if (action === 'earn_points') payload.purchase_amount = root.querySelector('[data-purchase-amount]').value;
    if (action === 'redeem_points') payload.amount = root.querySelector('[data-redeem-amount]').value;
    return {action, signature:JSON.stringify(payload)};
  }
  function operationCopy(candidate) {
    const payload = JSON.parse(candidate.signature);
    if (candidate.action === 'earn_stamp') return {title:'Agregar 1 sello', effect:'+1 sello'};
    if (candidate.action === 'redeem_stamp') return {title:'Canjear recompensa', effect:`Canjear ${state.selection.business.stamps_reward_limit} sellos`};
    if (candidate.action === 'earn_points') return {title:'Acreditar puntos', effect:`Registrar compra por $${payload.purchase_amount || '0'}`};
    return {title:'Canjear puntos', effect:`-${payload.amount || '0'} puntos`};
  }
  function prepareOperation(action) {
    const candidate = operationFor(action);
    if (!candidate) { message('Escanea primero una tarjeta válida.', 'error'); return; }
    const copy = operationCopy(candidate);
    pendingConfirmation = candidate;
    root.querySelector('[data-confirm-title]').textContent = copy.title;
    root.querySelector('[data-confirm-customer]').textContent = state.selection.customer.full_name || '';
    root.querySelector('[data-confirm-before]').textContent = balanceText(state.selection.customer);
    root.querySelector('[data-confirm-effect]').textContent = copy.effect;
    confirmPanel.classList.remove('hidden');
    customerPanel.classList.add('hidden');
    root.querySelector('[data-confirm-operation]').focus();
  }
  function cancelConfirmation() {
    pendingConfirmation = null;
    confirmPanel.classList.add('hidden');
    renderSelection();
  }
  async function processConfirmed() {
    if (!pendingConfirmation) return;
    const candidate = pendingConfirmation;
    pendingConfirmation = null;
    const operation = state.beginOperation({...candidate, id:requestId(), before:balanceText(state.selection.customer)});
    if (!operation) { message('Existe un resultado pendiente. Solo puedes reintentar exactamente el mismo movimiento.', 'error'); confirmPanel.classList.add('hidden'); renderSelection(); return; }
    updateControls();
    message('Confirmando movimiento…');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/web-scanner/process', {method:'POST', signal:controller.signal, headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRFToken':csrf,'Idempotency-Key':operation.id}, body:operation.signature});
      const data = await responseJson(response);
      if (requireReauthentication(response, data)) return;
      if (!response.ok) {
        confirmPanel.classList.add('hidden');
        if (response.status >= 500) { state.finishOperation('ambiguous'); message('El resultado todavía no puede confirmarse. Reintenta el mismo movimiento para consultar su resultado.', 'error'); }
        else { state.finishOperation('definitive'); message(data?.message || 'El movimiento fue rechazado. Revisa los datos e inténtalo nuevamente.', 'error'); }
        renderSelection();
        return;
      }
      if (!data?.customer) { state.finishOperation('ambiguous'); confirmPanel.classList.add('hidden'); renderSelection(); message('No fue posible confirmar el saldo. Reintenta el mismo movimiento.', 'error'); return; }
      state.finishOperation('success', data.customer);
      confirmPanel.classList.add('hidden');
      customerPanel.classList.add('hidden');
      root.querySelector('[data-success-name]').textContent = state.selection.customer.full_name || 'El cliente';
      root.querySelector('[data-success-balance]').textContent = balanceText(state.selection.customer);
      successPanel.classList.remove('hidden');
      message(data.wallet_sync?.google_status === 'pending' ? 'Movimiento confirmado. La tarjeta digital se actualizará en segundo plano.' : 'Movimiento confirmado correctamente.', 'success');
      updateControls();
    } catch (_error) {
      state.finishOperation('ambiguous');
      confirmPanel.classList.add('hidden');
      renderSelection();
      message('Resultado incierto por pérdida de conexión. Reintenta el mismo movimiento para confirmarlo sin duplicarlo.', 'error');
    } finally { clearTimeout(timeout); updateControls(); }
  }
  function reset() {
    if (!state.reset()) { message('No puedes cambiar de cliente mientras un movimiento tenga resultado incierto.', 'error'); return; }
    clearSelection();
    root.querySelector('[data-purchase-amount]').value = '';
    root.querySelector('[data-redeem-amount]').value = '';
    startPanel.classList.remove('hidden');
    hideMessage();
  }

  root.querySelector('[data-start-camera]').addEventListener('click', () => startCamera());
  root.querySelector('[data-stop-camera]').addEventListener('click', () => stopCamera());
  cameraSelect.addEventListener('change', restartCamera);
  actionButtons.forEach(button => button.addEventListener('click', () => prepareOperation(button.dataset.action)));
  root.querySelector('[data-cancel-operation]').addEventListener('click', cancelConfirmation);
  root.querySelector('[data-confirm-operation]').addEventListener('click', processConfirmed);
  root.querySelector('[data-reset-scanner]').addEventListener('click', reset);
  root.querySelector('[data-scan-next]').addEventListener('click', reset);
  window.addEventListener('pagehide', () => stopCamera({showStart:false}));
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopCamera({showStart:false}); });
  updateControls();
})();
