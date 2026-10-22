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
`REGISTRATION_EMAIL_ENCRYPTION_KEY`. Las dos últimas deben permanecer estables:
rotarlas sin transición rompería identidades HMAC o trabajos pendientes.

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
Nunca crea tablas ni aplica migraciones. El head esperado es `f9a0b1c2d3e4`.

## 4. Migración controlada

1. Detenga escrituras y confirme el respaldo restaurable.
2. Ejecute `flask --app run.py db current`.
3. Revise el plan y ejecute una sola vez `flask --app run.py db upgrade` desde
   una tarea administrativa controlada.
4. Confirme `db current` y el preflight con conexión.
5. No agregue migraciones al `CMD`, health checks ni peticiones web.

`init-db` y `seed-db` están bloqueados en producción. `seed-db` exige además una
confirmación explícita en desarrollo aislado. Los downgrades destructivos no son
un rollback: restaure el respaldo y vuelva a la versión compatible.

## 5. Procesos Railway futuros

Configure servicios separados que compartan imagen y base, pero no comando:

- Web: el `CMD` de Gunicorn del `Dockerfile`.
- Registro: `flask --app run.py run-registration-email-worker`.
- Google Wallet, solo en modo real autorizado:
  `flask --app run.py run-google-wallet-sync-worker`.

Los workers usan outboxes duraderos, reintentos y recuperación de leases;
reciben SIGTERM/SIGINT y terminan sin mantener transacciones durante llamadas
externas. Vigile únicamente métricas agregadas y nunca cuerpos de correo,
códigos, tokens o credenciales. El outbox no garantiza entrega exactamente una
vez ante todos los fallos externos.

## 6. Health, readiness y observabilidad

- `/api/health` confirma que el proceso web responde.
- `/api/ready` ejecuta una consulta mínima a PostgreSQL.
- Ninguno valida migraciones, workers, SMTP ni Wallet.
- El preflight comprueba configuración; no verifica proveedores externos.

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
- [ ] Preflight sin errores y revisión `f9a0b1c2d3e4` confirmada.
- [ ] CI, `pip check`, Tailwind, suite y Docker Linux aprobados.
- [ ] Web y workers configurados como procesos separados.
- [ ] Health, readiness, alertas y colas observados.
- [ ] Registro, recuperación, staff, promotores y tenants comprobados.
- [ ] Integraciones Wallet deshabilitadas o verificadas realmente.
- [ ] Plan de rollback y responsables aprobados.
- [ ] Decisión explícita de autorizar o rechazar producción.
