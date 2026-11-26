# Preparación de Google Wallet Demo Mode

Esta guía prepara una validación posterior. Mantenga ambos Wallet y
`GOOGLE_WALLET_RECONCILIATION_ENABLED` deshabilitados hasta que el propietario
autorice una ventana específica. El ensayo sintético confirma contratos del
código mediante transportes simulados. Demo Mode sí escribe en Google y
requiere un emisor de pruebas y cuentas autorizadas. La activación operativa
y el acceso de publicación comercial son decisiones independientes.

## Entorno aislado

1. Use el proyecto independiente de `compose.wallet-demo.yml`. Construya la

   imagen revisada con la etiqueta `j2m-interactive:wallet-demo-synthetic` y

   ejecute `docker compose -f compose.wallet-demo.yml config --quiet` antes

   de `up -d --wait`. La plantilla sintética fija la base, los modos Wallet,

   la red interna y la publicación en loopback. No combine este archivo con

   Compose operativo ni suministre un archivo de secretos.

2. El migrador aislado debe terminar antes del arranque web. Verifique la

   revisión `c3d4e5f6a7b8` y la respuesta de `/api/ready` desde el contenedor.

   Use `docker compose -f compose.wallet-demo.yml down` al terminar.

3. Antes de una futura prueba real, verifique con el propietario el emisor
   dedicado, sus credenciales dedicadas y sus cuentas Google autorizadas.
   No use el emisor operativo: los IDs incorporan el emisor y los IDs
   numéricos de negocio y cliente pueden repetirse entre bases.
4. Documente cómo ofrecer al proveedor los recursos HTTPS públicos de prueba,
   como el logo, con certificados válidos y sin exponer la aplicación ni
   PostgreSQL. Mantenga la red interna en el ensayo de preparación. La salida
   necesaria para Google se diseñará y autorizará por separado.
5. Detenga la prueba si no puede verificar la separación del emisor, base,
   red y credenciales. No reutilice workers, volúmenes ni recursos Wallet
   operativos. No solicite publicación comercial, cambie DNS o abra un túnel.

El Compose incluye web y worker. Ambos esperan al migrador y la red interna
bloquea el acceso a los proveedores. El comando
`python -m scripts.check_wallet_demo_readiness` inspecciona el Compose en
ejecucion y su esquema. Para revisar una propuesta futura, recibe por el
entorno `J2M_DEMO_CREDENTIALS_FILE`, `J2M_DEMO_ISSUER`,
`J2M_OPERATIONAL_ISSUER` y `J2M_DEMO_HTTPS_ORIGIN`. El archivo privado se
lee localmente y no se monta. El codigo 1 exige corregir evidencia tecnica.
El codigo 2 indica que los requisitos externos siguen pendientes. Ninguno de
los resultados habilita Wallet.

## Afinidad y recuperación

Ejecute `python -m scripts.probe_wallet_session_affinity` con
`TEST_POSTGRES_URL` apuntando a un PostgreSQL aislado de loopback
llamado `j2m_test`. La comprobación abre dos sesiones, cruza un commit
y prueba exclusión y liberación del advisory lock. Un puerto o un nombre de
pooler no confirma afinidad. La prueba sintética no certifica el pooler
operativo, que requiere autorización y evidencia propia.

El worker usa advisory locks de sesión, reservas persistentes y tokens de
reclamación. No desbloquee una reserva incierta por antigüedad. Antes de
un cambio autorizado, compruebe pin, ID inmutable de imagen, propiedad de
contenedor y red, y respuesta de Nginx con `X-J2M-Revision` de la versión
aprobada. El despliegue exige esa evidencia antes de construir, cambiar el
pin o recrear servicios. La revisión legada queda bloqueada porque no emite
la cabecera. Su compatibilidad con el esquema actualizado sigue pendiente. No pruebe rollback restaurando una base antigua sobre producción.
Mantenga un solo worker activo, preserve reservas e historial y verifique
compatibilidad de esquema antes de recuperar una versión anterior.

El diagnóstico de esquema ejecutable es `python -m scripts.check_wallet_referral_schema`.
Usa TLS con verificación de host y una transacción de solo lectura.

## Tarjetas e invitaciones históricas

Ejecute `python -m scripts.inspect_wallet_referral_history --business-id ID`
con `DATABASE_URL` en el entorno privado. La herramienta usa TLS verificado
y una transaccion de solo lectura. Avance con `--after` y
`--capability-after` segun los cursores devueltos. Use `--limit` entre 1
y 100. La salida excluye credenciales, codigos y datos personales.

Use el diagnóstico local de reconciliación por negocio y sus páginas de
reservas y trabajos. Consulte el estado del participante y la vigencia de
capacidades por lotes, sin copiar códigos ni URLs. Una referencia local no
prueba que la tarjeta exista o esté instalada. La detección de enlaces de
orígenes anteriores requiere evidencia del recurso remoto y orígenes
explícitamente autorizados. La migración no programa barridos históricos ni
regenera capacidades. Cualquier revisión posterior debe identificar el
negocio, aprobar un lote acotado y conservar las reservas inciertas.

## Matriz para una futura prueba real

Precondición común: emisor dedicado en Demo Mode, dispositivo y cuenta
autorizados, base ficticia y revisión correcta, salida acordada y rollback
verificado. La evidencia debe omitir credenciales, QR completos y URLs
privadas. Detenga toda la sesión si aparece un recurso real, identidad
inesperada, saldo incoherente o escritura incierta.

| Prueba | Acción | Resultado local y del proveedor | Evidencia física y criterio de parada |
| --- | --- | --- | --- |
| Alta y emisión | Crear negocio y cliente ficticios y guardar tarjeta | Un objeto asociado a la clase ficticia | Confirmar identidad de tarjeta y emisor en dispositivo. Detener si el ID existe fuera del entorno. |
| Reemisión y QR | Guardar la misma tarjeta y escanearla | ID, clase y QR estables | Escaneo físico correcto. Detener si aparece una tarjeta duplicada. |
| Sellos | Acumular y canjear; repetir la solicitud | Ledger y outbox sin duplicados; saldo vigente | Comparar saldo visible y confirmar que no retrocede. |
| Puntos | Acumular decimales y canjear consecutivamente | Transacciones separadas, mismo objeto | Validar representación y saldo después de cada operación. |
| Diseño | Guardar cambios y publicar explícitamente | Solo la publicación autorizada modifica los campos correspondientes | Verificar diseño y que saldo, QR e identidad sigan intactos. |
| Referidos | Consentir, consumir y dejar vencer una capacidad de prueba | Enlace público solo con consentimiento; capacidad temporal según estado | Validar apertura de los enlaces y retiro de capacidades no válidas. |
| Revocación y programa | Revocar; deshabilitar y reactivar | Revocación local inmediata; retirada visual eventual; sin reactivar revocados | Verificar ambos puntos de acceso en Wallet. Detener si se repone un enlace revocado. |
| Interrupción | Simular interrupción antes del envío o pérdida de confirmación | Solo se recuperan trabajos autorizados; una reserva incierta no se libera | Confirmar que no aparecen escrituras posteriores incompatibles. |
