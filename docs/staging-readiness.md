# Preparación y autorización de staging

Esta guía prepara un despliegue futuro; no demuestra que Railway, Supabase,
SMTP o Wallet funcionen realmente. Toda operación sobre infraestructura requiere
autorización posterior del propietario.

## Estados de liberación

- **Listo para pruebas locales:** dependencias, migraciones y suite local
  aprobadas, sin credenciales reales.
- **Listo para staging:** preflight sin errores, respaldo restaurable preparado,
  imagen Docker y CI aprobados. Todavía no está desplegado.
- **Staging validado:** servicios aislados desplegados por el propietario,
  migración comprobada, pruebas ficticias y observabilidad revisada.
- **Pendiente de producción:** estado predeterminado hasta validar restauración,
  dispositivos Wallet autorizados, correo real, colas y criterios de aceptación.

Bloquean staging: secretos ausentes, reutilizados o placeholders; SQLite; TLS
opcional; CORS, cookie o URL pública inseguros; revisión Alembic incorrecta;
backup no restaurable; CI fallido o workers sin configuración.

## 1. Variables privadas

Parta de `.env.example`, pero configure los valores reales exclusivamente en el
almacén privado de la plataforma. Genere por separado `SECRET_KEY`,
`JWT_SECRET_KEY`, `REGISTRATION_IDENTITY_HMAC_KEY` y
`REFERRAL_EVIDENCE_HMAC_KEYS`. Las claves HMAC deben permanecer
estables: rotarlas sin transición rompería sus controles históricos. El alta
pública y la recuperación ya no requieren SMTP ni un worker de correo.

Para rotar evidencia, anteponga la clave nueva y conserve las anteriores,
separadas por comas, durante toda la retención del historial. Retirar una clave
antigua impediría reconocer comprobantes firmados con ella; el preflight exige
que todas sean robustas, distintas y no sean placeholders.

Use `FLASK_ENV=production`, `FLASK_DEBUG=false`, `SESSION_COOKIE_SECURE=true`,
`UPLOAD_STORAGE_BACKEND=disabled` y el número exacto de proxies controlados en
`TRUSTED_PROXY_COUNT`. `PUBLIC_BASE_URL` y cada origen de `CORS_ORIGINS` deben
ser orígenes HTTPS exactos.

## 2. PostgreSQL aislado y TLS

Cree una base de staging separada de producción. Para Supabase use el Session
pooler por puerto **5432** y una `DATABASE_URL` privada. Mantenga
`sslmode=require` como mínimo; `verify-ca` o `verify-full` exigen certificados
reales. `require` cifra el transporte, pero no promete verificar la identidad
del servidor.

Antes de migrar, cree un respaldo y demuestre que puede restaurarse en otra base
efímera. Registre hora, revisión Alembic, responsable y resultado, nunca la URL
ni credenciales.

## 3. Preflight de solo lectura

Con el entorno futuro cargado:

```bash
flask --app run.py staging-preflight
```

El comando informa `CORRECTO`, `ADVERTENCIA` o `ERROR`, oculta valores y no
contacta SMTP, Google ni Apple. Tampoco consulta la base por defecto. Para
autorizar una lectura mínima de conectividad y `alembic_version`:

```bash
flask --app run.py staging-preflight --check-database
```

Esta opción ejecuta únicamente `SELECT 1` y una lectura de `alembic_version`.
Nunca crea tablas ni aplica migraciones. El head esperado para Fase 8C.3B1 es `5f6a7b8c9d0e`.

## 4. Migración controlada

1. Detenga escrituras y confirme el respaldo restaurable.
2. Ejecute `flask --app run.py db current`.
3. Revise el plan y ejecute una sola vez `flask --app run.py db upgrade` desde
   una tarea administrativa controlada.
4. Confirme `db current` y el preflight con conexión.
5. No agregue migraciones al `CMD`, health checks ni peticiones web.

`init-db` y toda entrada a `seed.py` están bloqueados en staging/producción y
frente a cualquier PostgreSQL, aunque el entorno se etiquete por error como
desarrollo. El seed solo admite SQLite dentro del proyecto local y la opción
`--yes-i-understand-data-will-be-deleted`. Los downgrades destructivos no son
un rollback: restaure el respaldo y vuelva a la versión compatible.
El seed local requiere además `J2M_DEMO_SEED_PASSWORD` (mínimo 12 caracteres),
sin contraseña de demostración incorporada al código ni permitida en servicios
públicos.

## 5. Procesos Railway futuros

Configure servicios separados que compartan imagen y base, pero no comando:

- Web: el `CMD` de Gunicorn del `Dockerfile`.
- Google Wallet, solo en modo real autorizado:
  `flask --app run.py run-google-wallet-sync-worker`.

Use `J2M_PROCESS_TYPE=web` o `google-wallet-worker` según
el servicio. El `HEALTHCHECK` de la imagen consulta `/api/health` únicamente en
modo web; en un worker comprueba que PID 1 corresponda al comando esperado.
Como defensa para servicios legacy que todavía no declaren `J2M_PROCESS_TYPE`,
el healthcheck detecta el comando de PID 1 (Gunicorn web o worker conocido);
si no reconoce un único proceso válido, falla cerrado. **Defina siempre la
variable explícitamente en Railway antes de declarar completado staging**.
No sustituye los controles de progreso/backlog de los trabajadores.

`railway.toml` no declara un healthcheck HTTP compartido. Configure desde el
servicio web de Railway la ruta `/api/ready` y un timeout acorde al arranque.
Los servicios worker no deben tener `healthcheckPath`, dominio ni puerto; deben
conservar reinicio `ON_FAILURE`. Así un worker nunca hereda una comprobación
HTTP que no puede responder.

En servicios legacy, Railway continúa leyendo Config as Code y cualquier valor
presente en `railway.toml` prevalece sobre el panel, aunque no actualice lo que
el panel muestra. El mecanismo está deprecado: servicios nuevos no pueden
adoptarlo y los archivos dejan de aplicarse el 1 de diciembre de 2026. Antes de
esa fecha, migre de forma controlada a Infrastructure as Code a nivel de
proyecto (`.railway/railway.ts`), comenzando con `railway config pull` y
revisando `railway config plan`; esta fase no crea ni aplica esa infraestructura.

El worker Google Wallet usa un outbox duradero, reintentos y recuperación de leases;
reciben SIGTERM/SIGINT y terminan sin mantener transacciones durante llamadas
externas. Vigile únicamente métricas agregadas y nunca cuerpos de correo,
códigos, tokens o credenciales. El outbox no garantiza entrega exactamente una
vez ante todos los fallos externos.

## 6. Health, readiness y observabilidad

- `/api/health` confirma que el proceso web responde.
- `/api/ready` ejecuta una consulta mínima a PostgreSQL.
- Ninguno valida migraciones, workers, SMTP ni Wallet.
- El preflight comprueba configuración; no verifica proveedores externos.

La comprobación de worker es liveness, no una garantía contra bloqueo interno.
Alértese además por ausencia de ciclos, trabajos pendientes/fallidos y antigüedad
del backlog; así no se considera sano un proceso vivo que dejó de avanzar.

Tras cada cambio de configuración, revise en los detalles del deployment el
origen efectivo de cada valor: el icono de archivo identifica ajustes heredados
de Config as Code. Verifique explícitamente `/api/ready` en web y ausencia de
healthcheck HTTP en ambos workers.

Configure alertas externas para disponibilidad, errores 5xx, latencia,
conexiones, trabajos pendientes/reintentados/fallidos y antigüedad del backlog.
Los endpoints públicos no deben exponer tablas, SQL, dependencias ni excepciones.

## 7. Pruebas funcionales de staging

Después de un despliegue autorizado, use exclusivamente negocios, empleados y
clientes ficticios. Verifique registro y recuperación, Super Admin, gerente,
cajero, aislamiento entre negocios, promotores/referidos, CSRF, revocación y
cierre de sesión. Compruebe que balances, ledger y QR históricos no cambian por
health checks o preflight.

Apple/Google Wallet deben probarse solo con cuentas y dispositivos autorizados.
Una imagen Docker construida o un JWT mock correcto no demuestra aceptación por
los proveedores, APNs ni instalación física.

## 8. Recuperación y decisión

Ante una migración fallida: bloquee tráfico, conserve logs sanitizados, no
reintente indefinidamente, restaure en una base aislada y valide integridad antes
de decidir el rollback. Ante backlog: detenga el worker afectado, conserve la
base, diagnostique estados agregados y reanude con la misma configuración.

Autorice producción únicamente cuando staging tenga migraciones, restauración,
flujos funcionales, correo, workers, observabilidad y Wallet requeridos
verificados. Si una integración opcional queda deshabilitada, documente la
limitación y confirme que la interfaz no la ofrece como operativa.

## Checklist del propietario

- [ ] Entorno de staging aislado y sin datos reales.
- [ ] Secretos independientes, estables y guardados fuera de Git.
- [ ] PostgreSQL con TLS y respaldo restaurado de prueba.
- [ ] Preflight sin errores y revisión `5f6a7b8c9d0e` confirmada.
- [ ] CI, `pip check`, Tailwind, suite y Docker Linux aprobados.
- [ ] Web y workers configurados como procesos separados.
- [ ] Health, readiness, alertas y colas observados.
- [ ] Registro, recuperación, staff, promotores y tenants comprobados.
- [ ] Integraciones Wallet deshabilitadas o verificadas realmente.
- [ ] Plan de rollback y responsables aprobados.
- [ ] Decisión explícita de autorizar o rechazar producción.
