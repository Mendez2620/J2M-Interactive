# Apple Wallet 8D.1A — Modelo de emisor y preparación para activación

**Estado:** consulta enviada a Apple Developer Support el 2026-11-03, **caso `30000004100447`**; pendiente de respuesta escrita y decisión formal del titular de J2M. Esta fase NO autoriza emisión comercial, migración de pases ni habilitar APNs. Revisado el 2026-11-03.

## 1. Motivo de la revisión

El producto J2M Interactive presta un servicio de lealtad **multiempresa**. El código actual (`app/services/apple_wallet.py`) obtiene `APPLE_PASS_TYPE_ID`, `APPLE_TEAM_ID` y certificados de variables **globales** de proceso, pero establece `pass.json.organizationName = business.name` para cada negocio.

El **Apple Developer Program License Agreement, Attachment 5 §1** restringe el uso del Pass Type ID a los pases del propio emisor y prohíbe usarlo para firmar el pase de un tercero. La mención de marcas de terceros exige contar con derechos. Attachment 5 §2.3 obliga a facilitar en el pase **nombre, dirección y datos de contacto (teléfono y correo)** para consultas y reclamaciones.

Fuente oficial: https://developer.apple.com/support/terms/apple-developer-program-license-agreement/

**No interpretar una sola membresía de 99 USD como licencia universal para emitir los pases independientes de todos los clientes de J2M.** Obtener respuesta escrita de Apple sobre esta estructura antes de habilitar emisión para terceros. Este documento no es asesoría jurídica ni respuesta de Apple.

## 2. Decisiones pendientes y modelos posibles

| Modelo | Emisor contractual y técnico | Requisito previo | Estado |
| --- | --- | --- | --- |
| Demo propia J2M | Titular autorizado del Apple Developer Program emite un pase bajo identidad propia autorizada | Membresía, Pass Type ID propio, certificado, datos de contacto y pruebas | Aún no activo |
| Emisor individual por negocio | Cada restaurante es emisor bajo su propia identidad; J2M presta servicios técnicos autorizados | Apple confirma requisitos y cada emisor aporta identificador/certificado y autorizaciones | Pendiente de confirmar |
| J2M como emisor de un programa compartido | J2M figura realmente como emisor y presenta comercios participantes | Apple confirma por escrito el modelo, derechos sobre marcas, términos y responsabilidad | Pendiente de confirmar |

Si el diseño autorizado exige identidad por negocio, **NO basta** con cambiar el `organizationName` del JSON: hay que implementar resolución de emisor por tenant, credenciales/certificados aislados, validación explícita y plan de migración. No habilitarlo silenciosamente.

La membresía individual usa el nombre legal del titular para relaciones con Apple y para vendedor de apps. Una conversión posterior a organización requiere solicitud, D-U-N-S y revisión de Apple; no asumir continuidad automática de certificados, identificadores o pases. Fuentes:
- https://developer.apple.com/help/account/membership/program-enrollment/
- https://developer.apple.com/help/account/membership/updating-your-account-information/

## 3. Consulta que debe enviarse por canal autenticado de Apple

Canal oficial: https://developer.apple.com/contact/ (iniciar sesión con Apple ID; ofrece correo o llamada). No se publica una dirección de correo universal verificada para enviar esta consulta directa sin abrir un caso.

**Asunto:** Apple Wallet Pass Type IDs for a multi-merchant loyalty SaaS — issuer model clarification

> Hello Apple Developer Support,
>
> We are developing J2M Interactive, a multi-tenant loyalty SaaS used by independent restaurants and merchants. Our backend generates and signs Wallet loyalty passes, and serves updates through PassKit Web Service and APNs.
>
> Before joining Apple Developer Program and issuing passes, we would like clarification on Attachment 5 of the Apple Developer Program License Agreement.
>
> 1. Can J2M legally act as the sole issuer for a program involving several independent participating merchants, while displaying their authorized names and branding, under one developer team and Pass Type ID? What issuer/support-contact disclosures would be required?
> 2. If each merchant is considered the issuer of its own program, must each merchant have its own developer membership, Pass Type ID and signing certificate, with J2M operating only as an authorized technical service provider? What delegation or agreements are required?
> 3. Can we begin with an individual membership for an internal J2M-branded demo and later convert it to an organization account when J2M is formally incorporated? What happens to existing Pass Type IDs, signing certificates and installed passes?
>
> Please indicate the permitted model or the appropriate specialist team. Thank you.

**Envío confirmado** por el portal oficial Apple Developer Support (desarrollo técnico → otras preguntas). Identificador del caso: `30000004100447`. Apple indicó que responderá al correo del Apple ID. La **respuesta y autorización contractual siguen pendientes**. Conservar el número de caso y el texto de respuesta escrita en un repositorio privado, sin datos personales ni credenciales en Git. No incluir datos de clientes, claves o certificados.

## 4. Revisión técnica del estado actual

- 8C.3C2 fue integrado en `main` y `staging`. Migración vigente: `9d0e1f2a3b4c`.
- En staging se configuró **`APPLE_WALLET_MODE=disabled`** explícitamente en web y Google worker. Falta el servicio Apple dedicado.
- `AppleWalletService` usa `APPLE_PASS_TYPE_ID`, `APPLE_TEAM_ID`, rutas de certificado/clave/WWDR, `APPLE_PASS_AUTH_KEYS` y `APPLE_WEB_SERVICE_URL` globales.
- PassKit conserva `serialNumber` por credencial persistida; cambiar el Pass Type ID o el Team ID para un pase ya instalado **no es una actualización normal**. Requiere estrategia de reemisión y consentimiento del titular, preservando el ledger.
- Los pases históricos emitidos usando QR de caja como autenticación PassKit no pueden actualizarse automáticamente con el mecanismo seguro nuevo.
- El `staging-preflight` actual valida configuración/HTTPS/rutas pero su resultado no sustituye la verificación contractual ni una instalación física real.
- Revisar `pass.json` para exponer, de forma segura y sin PII indebida, **nombre, dirección, teléfono y correo de soporte del emisor** tal como exige Attachment 5 §2.3; `wallet_contact` por sí solo no demuestra que estén todos los datos requeridos. Crear implementación y tests solo tras resolver emisor.
- Conservar compatibilidad Google Wallet, IDs de cliente, QR de caja, puntos, sellos y ledger.

## 5. Puertas de seguridad antes de habilitar `real`

1. **Titularidad:** respuesta de Apple, identificación del emisor y autorización de marca; determinar si demo personal o programa comercial por emisor.
2. **Membresía:** titular inscrito, Apple Team ID y Pass Type ID expedido. No usar placeholders como `pass.com.j2minteractive.loyalty` o `J2M1234567`.
3. **Firma:** generar CSR y clave privada en dispositivo/entorno controlado; mantener la clave fuera de Git, tickets, correo y chat. Descargar certificado Pass Type ID y WWDR, comprobar cadena, fechas, UID/OU y clave correspondiente. Secretos de firma con custodia rotación y permisos mínimos.
4. **Datos del pase:** identidad del emisor, derechos de marca, nombre/dirección/correo/teléfono para soporte, términos y privacidad aprobados. Contenido congruente con el certificado.
5. **Servicio:** HTTPS público `APPLE_WEB_SERVICE_URL`, `APPLE_PASS_AUTH_KEYS` de alta entropía y conservación segura del anillo, web y worker con secretos referenciados (no copiados al repositorio), certificado disponible en ambos procesos, health/preflight seguros. Evitar exponer paths o material sensible en logs.
6. **Apple worker:** desplegar `j2m-apple-wallet-worker` dedicado con `J2M_PROCESS_TYPE=apple-wallet-worker`, sin HTTP healthcheck, dominio público o migraciones automáticas. Limitar replicas/concurrencia de acuerdo con el worker existente, revisión y monitoreo de jobs.
7. **Pruebas:** primero validación offline de firma y config; luego demo bajo identidad autorizada con un solo cliente/pase de prueba; registrar dispositivo en iPhone real, publicar cambio reversible, verificar APNs aceptado y descarga del mismo serial actualizado; errores/transitorios/backoff y controles de revocación. APNs 200 no acredita instalación.
8. **Liberación:** aprobar explícitamente una activación controlada, respaldar base y variables, comprobar aislamiento tenant, rollback operativo a `disabled`; nunca enviar lotes reales antes de las pruebas.

## 6. Si Apple exige emisores separados por negocio (propuesta, NO implementada)

Preparar una fase posterior con una entidad `WalletIssuer`/equivalente que vincule `business_id` a un emisor autorizado y a referencias de secretos no exportables. Una credencial de pase debe quedar ligada al `pass_type_identifier` original; aislar materiales de firma, HMAC, APNs topic, Team ID y cadena del emisor. Definir control de acceso Super Admin, auditoría append-only, CAS y retención de versiones/llaves. Migrar sin reescribir identificadores instalados, sin tocar saldos ni activar todos los negocios. Probar con dos emisores/certificados diferentes, accesos cruzados, revocación y rollback seguro.

## 7. Estado/entregables para cerrar 8D.1A

- [x] Número de caso y confirmación de envío a Apple Developer Support: `30000004100447`.
- [ ] Respuesta escrita de Apple guardada de manera privada.
- [ ] Modelo de emisor escogido y aprobado por el titular de J2M.
- [ ] Decisión sobre membresía personal para demo o entidad legal para emisión comercial.
- [ ] Revisión de datos de soporte obligatorios en `pass.json`.
- [ ] Aprobación de diseño para implementación técnica siguiente.

**Este documento por sí solo NO habilita Apple Wallet.** No contiene secretos y no autoriza cambios en Railway, Supabase, GitHub `main` o clientes reales.
