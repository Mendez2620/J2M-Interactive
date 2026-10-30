(() => {
  const root = document.querySelector('[data-staff-linking]');
  if (!root) return;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const status = root.querySelector('[data-staff-linking-status]');
  const request = async (url, payload) => {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify(payload)});
    const data = await response.json().catch(() => ({message: 'Respuesta inválida del servidor.'}));
    if (!response.ok) throw new Error(data.message || 'No fue posible completar la operación.');
    return data;
  };
  root.querySelector('[data-create-linking]').addEventListener('submit', async (event) => {
    event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('button'); button.disabled = true;
    try {
      const values = new FormData(form); const data = await request(root.dataset.createEndpoint, {customer_id: values.get('customer_id'), human_check_note: values.get('human_check_note'), recovery_mode: values.get('recovery_mode') === 'true'});
      const result = root.querySelector('[data-linking-result]'); result.classList.remove('hidden');
      result.querySelector('[data-linking-secret]').textContent = data.linking_secret;
      result.querySelector('[data-linking-path]').textContent = data.enrollment_path;
      result.querySelector('[data-linking-id]').textContent = data.enrollment_id;
      root.querySelector('[data-approve-linking] input[name="enrollment_id"]').value = data.enrollment_id;
      status.textContent = 'Autorización creada. Espera el código mostrado en el dispositivo del cliente.';
    } catch (error) { status.textContent = error.message; } finally { button.disabled = false; }
  });
  root.querySelector('[data-approve-linking]').addEventListener('submit', async (event) => {
    event.preventDefault(); const values = new FormData(event.currentTarget); const id = values.get('enrollment_id');
    try { await request(root.dataset.approveTemplate.replace('{id}', encodeURIComponent(id)), {pairing_code: values.get('pairing_code')}); status.textContent = 'Emparejamiento confirmado. El cliente ya puede crear su passkey.'; }
    catch (error) { status.textContent = error.message; }
  });
})();
