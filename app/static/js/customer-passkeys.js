(() => {
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const decode = (value) => {
    const normalized = value.replace(/-/g, '+').replace(/_/g, '/');
    const bytes = Uint8Array.from(atob(normalized + '='.repeat((4 - normalized.length % 4) % 4)), c => c.charCodeAt(0));
    return bytes.buffer;
  };
  const encode = (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const normalizeOptions = (options) => {
    options.challenge = decode(options.challenge);
    if (options.user) options.user.id = decode(options.user.id);
    if (options.excludeCredentials) options.excludeCredentials.forEach(item => { item.id = decode(item.id); });
    if (options.allowCredentials) options.allowCredentials.forEach(item => { item.id = decode(item.id); });
    return options;
  };
  const serialize = (credential) => ({
    id: credential.id, rawId: encode(credential.rawId), type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: credential.response.attestationObject ? {
      attestationObject: encode(credential.response.attestationObject),
      clientDataJSON: encode(credential.response.clientDataJSON),
      transports: credential.response.getTransports ? credential.response.getTransports() : [],
    } : {
      authenticatorData: encode(credential.response.authenticatorData),
      clientDataJSON: encode(credential.response.clientDataJSON),
      signature: encode(credential.response.signature),
      userHandle: credential.response.userHandle ? encode(credential.response.userHandle) : null,
    },
  });
  const post = async (url, body) => {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': csrf()}, body: JSON.stringify(body || {})});
    const data = await response.json().catch(() => ({message: 'Respuesta inválida del servidor.'}));
    if (!response.ok) throw new Error(data.message || 'La operación no pudo completarse.');
    return data;
  };
  const status = (root, message) => { const node = root.querySelector('[data-auth-status]'); if (node) node.textContent = message; };
  document.querySelector('[data-enroll-passkey]')?.addEventListener('click', async (event) => {
    const root = event.currentTarget.closest('[data-customer-enrollment]'); event.currentTarget.disabled = true;
    try { status(root, 'Esperando confirmación…'); const options = normalizeOptions(await post('/customer-auth/enroll/options')); const credential = await navigator.credentials.create({publicKey: options}); const result = await post('/customer-auth/enroll/complete', serialize(credential)); window.location.assign(result.redirect); }
    catch (error) { status(root, error.message); event.currentTarget.disabled = false; }
  });
  document.querySelector('[data-login-passkey]')?.addEventListener('click', async (event) => {
    const root = event.currentTarget.closest('[data-customer-login]'); const slug = root.dataset.businessSlug; event.currentTarget.disabled = true;
    try { status(root, 'Verificando passkey…'); const options = normalizeOptions(await post(`/customer-auth/login/${encodeURIComponent(slug)}/options`)); const credential = await navigator.credentials.get({publicKey: options}); const result = await post(`/customer-auth/login/${encodeURIComponent(slug)}/complete`, serialize(credential)); window.location.assign(result.redirect); }
    catch (error) { status(root, error.message); event.currentTarget.disabled = false; }
  });
})();
