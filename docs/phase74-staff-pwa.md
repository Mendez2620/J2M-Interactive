# Fase 7.4 — Categorías, escáner web y PWA del personal

## Accesos

- Cajeros: `/staff/login` y `/staff/portal/scanner`.
- Gerentes: `/manager/login` y `/manager/portal/scanner`.
- Super Admin: el botón **Gestionar categorías** abre el módulo independiente sin recrear ni eliminar registros.

El escáner web exige que el negocio y el empleado estén activos, que ambos tengan el canal web habilitado y que el empleado posea `scanner_operate_web`. El permiso móvil `scanner_operate` permanece independiente. Las sesiones web se registran en `staff_sessions`, expiran, se revocan al cerrar sesión y dejan de ser válidas tras suspensión, cambio de contraseña o incremento de `auth_version`.

## Operación del escáner

La cámara se solicita únicamente al pulsar **Iniciar escáner**. `html5-qrcode` 2.3.8 se sirve localmente y permite elegir cámara; el ingreso manual es una alternativa cuando el dispositivo no permite cámara. El código se conserva solo en memoria mientras se confirma una operación y no se escribe en logs ni almacenamiento web.

La lectura solamente consulta. El usuario debe confirmar después la acreditación o canje. El backend reutiliza `process_loyalty_transaction`, ledger, idempotencia, concurrencia y outbox de Google Wallet. Cuando el outbox queda pendiente, la interfaz confirma el movimiento en J2M sin afirmar que Wallet ya se actualizó. No existen movimientos offline.

## Instalación

En Chrome Android y navegadores compatibles aparece **Instalar J2M Staff** cuando se recibe `beforeinstallprompt`. En Safari para iPhone o iPad se muestran las instrucciones **Compartir → Añadir a pantalla de inicio**. La disponibilidad y algunas capacidades dependen de la versión del sistema y navegador.

El manifest usa `display: standalone`, iconos 192 y 512, y abre `/staff/login`. El service worker solo almacena CSS, JavaScript público, iconos y la biblioteca QR. Las navegaciones autenticadas, APIs, tokens, saldos y transacciones nunca se guardan en Cache Storage. Sin conexión se muestra un aviso y no se encola ninguna operación.

## Despliegue y límites

- No requiere variables de entorno nuevas ni migraciones.
- Requiere HTTPS para cámara, service worker e instalación.
- Debe verificarse físicamente en Safari iOS/iPadOS y Chrome Android antes de producción.
- No cambia Flutter, Apple Wallet, Google Wallet ni los identificadores QR existentes.
