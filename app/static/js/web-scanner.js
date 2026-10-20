(() => {
  const root = document.querySelector('[data-web-scanner]');
  if (!root) return;
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const status = root.querySelector('[data-scanner-status]');
  const cameraPanel = root.querySelector('[data-camera-panel]');
  const cameraSelect = root.querySelector('[data-camera-select]');
  let scanner = null;
  let scanning = false;
  let lookupPending = false;
  let transactionPending = false;
  let currentToken = null;
  let currentCustomer = null;
  let currentBusiness = null;
  let retryOperation = null;

  function message(text, type = 'info') {
    status.textContent = text;
    status.className = `rounded-xl p-3 text-sm ${type === 'error' ? 'bg-rose-50 text-rose-800' : type === 'success' ? 'bg-emerald-50 text-emerald-800' : 'bg-indigo-50 text-indigo-800'}`;
  }
  async function json(response) { try { return await response.json(); } catch (_error) { return {}; } }
  function requestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
  }
  async function stopCamera() {
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
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.Html5Qrcode) { message('La cámara requiere HTTPS y un navegador compatible. Usa el ingreso alternativo.', 'error'); return; }
    root.querySelector('[data-start-camera]').disabled = true;
    message('Solicitando acceso a la cámara…');
    try {
      const cameras = await Html5Qrcode.getCameras();
      cameraSelect.replaceChildren();
      cameras.forEach((camera, index) => { const option = document.createElement('option'); option.value = camera.id; option.textContent = camera.label || `Cámara ${index + 1}`; cameraSelect.appendChild(option); });
      if (!cameras.length) throw Object.assign(new Error('camera missing'), {name:'NotFoundError'});
      cameraPanel.classList.remove('hidden');
      scanner ||= new Html5Qrcode(root.querySelector('[data-qr-reader]').id);
      await scanner.start(cameraSelect.value || {facingMode:'environment'}, {fps:10, qrbox:{width:240,height:240}}, decoded => { if (!lookupPending) lookup(decoded); }, () => {});
      scanning = true; message('Cámara activa. Coloca el QR dentro del recuadro.', 'success');
    } catch (error) { await stopCamera(); message(cameraError(error), 'error'); }
    finally { root.querySelector('[data-start-camera]').disabled = false; }
  }
  async function restartCamera() { await stopCamera(); await startCamera(); }
  function renderCustomer(data) {
    currentCustomer = data.customer; currentBusiness = data.business;
    root.querySelector('[data-customer-name]').textContent = data.customer.full_name;
    root.querySelector('[data-customer-balance]').textContent = data.business.loyalty_type === 'stamps' ? `${data.customer.current_stamps}/${data.business.stamps_reward_limit} sellos` : `${data.customer.current_points} puntos`;
    root.querySelector('[data-stamps-actions]').classList.toggle('hidden', data.business.loyalty_type !== 'stamps');
    root.querySelector('[data-points-actions]').classList.toggle('hidden', data.business.loyalty_type !== 'points');
    root.querySelector('[data-customer-panel]').classList.remove('hidden');
  }
  async function lookup(token) {
    token = String(token || '').trim(); if (!token || lookupPending) return;
    lookupPending = true; message('Validando el código…');
    try {
      const response = await fetch('/api/web-scanner/lookup', {method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRFToken':csrf}, body:JSON.stringify({token})});
      const data = await json(response); if (!response.ok) throw new Error(data.message || 'No fue posible validar el código.');
      currentToken = token; root.querySelector('[data-manual-token]').value = ''; await stopCamera(); renderCustomer(data); message('Cliente encontrado. Selecciona y confirma una operación.', 'success');
    } catch (error) { message(error.message, 'error'); }
    finally { lookupPending = false; }
  }
  function balanceText(customer) { return currentBusiness.loyalty_type === 'stamps' ? `${customer.current_stamps} sellos` : `${customer.current_points} puntos`; }
  async function process(action) {
    if (!currentToken || !currentCustomer || transactionPending) return;
    const payload = {token:currentToken, action};
    if (action === 'earn_points') payload.purchase_amount = root.querySelector('[data-purchase-amount]').value;
    if (action === 'redeem_points') payload.amount = root.querySelector('[data-redeem-amount]').value;
    const signature = JSON.stringify(payload);
    if (!retryOperation || retryOperation.signature !== signature) retryOperation = {signature, id:requestId()};
    const before = balanceText(currentCustomer); transactionPending = true;
    root.querySelectorAll('[data-action]').forEach(button => { button.disabled = true; }); message('Confirmando movimiento en J2M…');
    try {
      const response = await fetch('/api/web-scanner/process', {method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json','X-CSRFToken':csrf,'Idempotency-Key':retryOperation.id}, body:signature});
      const data = await json(response); retryOperation = null; if (!response.ok) throw new Error(data.message || 'El movimiento fue rechazado.');
      currentCustomer = data.customer; renderCustomer({customer:data.customer,business:currentBusiness});
      const wallet = data.wallet_sync?.google_status === 'pending' ? ' El movimiento está confirmado en J2M; Google Wallet tiene una actualización pendiente.' : '';
      message(`Movimiento confirmado. Saldo anterior: ${before}. Saldo resultante: ${balanceText(data.customer)}.${wallet}`, 'success');
    } catch (error) { message(error.message || 'Sin conexión. Puedes reintentar sin duplicar el movimiento.', 'error'); }
    finally { transactionPending = false; root.querySelectorAll('[data-action]').forEach(button => { button.disabled = false; }); }
  }
  function reset() { currentToken = null; currentCustomer = null; currentBusiness = null; retryOperation = null; root.querySelector('[data-customer-panel]').classList.add('hidden'); root.querySelector('[data-purchase-amount]').value = ''; root.querySelector('[data-redeem-amount]').value = ''; message('Listo para escanear otro cliente.'); }

  root.querySelector('[data-start-camera]').addEventListener('click', startCamera);
  root.querySelector('[data-stop-camera]').addEventListener('click', stopCamera);
  cameraSelect.addEventListener('change', restartCamera);
  root.querySelector('[data-lookup-manual]').addEventListener('click', () => lookup(root.querySelector('[data-manual-token]').value));
  root.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => process(button.dataset.action)));
  root.querySelector('[data-reset-scanner]').addEventListener('click', reset);
  window.addEventListener('pagehide', stopCamera);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopCamera(); });
})();
