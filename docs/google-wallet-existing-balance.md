# Fase 8E.2B.2 — Reconciliación de una tarjeta existente

Componente interno de proveedor: `GoogleWalletService.reconcile_existing_loyalty_balance(customer, business_id=...)`.

Este componente no está conectado a endpoints, comandos, tareas o workers. No debe ejecutarse sobre datos reales hasta completar el orquestador de reconciliación y su revisión.

- Requiere modo real y coincidencia del cliente con un negocio activo explícito.
- Consulta exclusivamente el identificador estable del objeto, verifica su clase, identidad y estado activo.
- Si el saldo y su etiqueta coinciden, no escribe; en caso contrario envía exclusivamente `loyaltyPoints` por PATCH.
- Comprueba identidad, estado y saldo en la respuesta. El resultado no contiene objetos remotos, QR ni saldos.
- Un 404 en GET o PATCH se registra como objeto no encontrado, sin POST ni reconstrucción.
- No toca modelos, cola, ledger, diseño, código QR ni estado del objeto. La ausencia de objeto remoto no demuestra que el usuario nunca haya guardado un pase.
- El futuro orquestador debe verificar el ledger, autorización, vigencia del snapshot, bloqueo por cliente compartido con el worker, límites, idempotencia y registro persistente del resultado. No se debe utilizar esta función fuera de ese contrato en producción.

Referencias de contrato HTTP: [LoyaltyObject](https://developers.google.com/wallet/reference/rest/v1/loyaltyobject), [PATCH](https://developers.google.com/wallet/reference/rest/v1/loyaltyobject/patch). Las pruebas simulan HTTP; no confirman propagación a un Android, credenciales o permisos reales.

No se incluye activación ni migración.
