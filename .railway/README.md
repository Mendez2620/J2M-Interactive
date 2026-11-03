# J2M Interactive — Railway Infrastructure as Code (Phase #33)

**Estado: candidato en revisión; no aplicado.** El 2026-11-03 se consultó la configuración real de `j2m-interactive-staging` mediante la integración Railway, y se preparó `.railway/railway.ts` con los tres servicios existentes.

**No se pudo ejecutar `railway config pull` ni `railway config plan` desde ChatGPT** porque la sesión CLI local de Railway no está instalada/autenticada aquí; las lecturas de la integración son de solo consulta y no sustituyen el plan oficial. El archivo se redactó como **candidato**, no como exportación comprobada por la CLI. **No marcar como migración completada, no hacer merge de este PR antes de probar el plan en una sesión CLI autenticada.**

## Fuente de verdad y alcance

- Proyecto: `j2m-interactive-staging`. ID de referencia (solo verificación, no secreto): `2efc1b38-c626-4e08-8463-b2506de42f76`.
- Entorno Railway llamado `production`, pero corresponde al **proyecto de staging de J2M**. ID de verificación: `bdd0c3bc-bbff-4172-b7f3-1d58ac081562`.
- Este repositorio controla tres servicios; por prudencia se usa el `partial = "j2m-core-services"`, de modo que servicios futuros no estén sujetos a eliminación por ausencia del archivo. Verificar que no exista ownership/partial previo antes de reclamar recursos.
- Los secretos y los valores de configuración permanecen guardados en Railway con `preserve()`. NO usar `--include-variables`, `--show-values`, dumps de variables ni archivos de estado con valores secretos. Las `RAILWAY_*` son variables proporcionadas automáticamente por la plataforma y se excluyen.
- Los dominios `*.up.railway.app` son generados por Railway y no aparecen en el IaC. No remover ni reemplazar el dominio público actual.
- No agregar servicios Apple, buckets, volúmenes o bases de datos que **no existan** actualmente en este proyecto. PostgreSQL se aloja por separado en Supabase.

### Estado real leído el 2026-11-03

| Servicio | Rama | Builder | Start / health | Función |
| --- | --- | --- | --- | --- |
| `j2m-web` | `staging` | RAILPACK | start automático, `/api/ready` | web Flask |
| `j2m-google-wallet-worker` | `staging` | DOCKERFILE, `Dockerfile` | `flask --app run.py run-google-wallet-sync-worker` | sincronización Google Wallet |
| `j2m-migrator-77a` | `main` | RAILPACK | `python -m flask --app run.py db upgrade 9d0e1f2a3b4c`, restart NEVER | migrador de una sola ejecución |

`APPLE_WALLET_MODE=disabled` ya está definido en web y Google worker. No cambiarlo a `mock` o `real` durante la migración. La nueva base en Supabase ya tiene Alembic `9d0e1f2a3b4c`; **no volver a aplicar migraciones como prueba del IaC**.

## Comprobación segura (solo lectura) antes de un merge

Usar una sesión autenticada del Railway CLI, desde la **rama del PR**, en un equipo de confianza con la CLI actualizada y acceso al proyecto de staging. No pegar credenciales ni tokens en ChatGPT.

1. Confirmar `railway whoami` y vincular el CLI de forma explícita **al proyecto staging y su entorno**, nunca a un proyecto de producción comercial. Comprobar IDs antes de continuar. Si el CLI exige seleccionar un servicio, elegir el entorno primero.
2. Ejecutar `railway config partials list`. Si algún recurso ya es propiedad de otra partial, STOP: no reclamarlo silenciosamente.
3. Guardar una copia local del candidato fuera del directorio del repositorio; en un **worktree temporal aislado** usar `railway config pull` para obtener el estado real y comparar el resultado con `.railway/railway.ts`. No utilizar `--include-variables` y no subir la exportación sin revisarla. `pull` puede reescribir el archivo de trabajo: hacerlo únicamente en esa copia, **nunca sobre la rama del PR**.
4. En el checkout de la rama del PR, confirmar que no existe `railway.toml` (se elimina SOLO en esta rama). Comprobar en Railway Settings de cada servicio si hay una ruta de config-as-code todavía especificada; quitarla requerirá aprobación de un cambio de configuración posterior.
5. Ejecutar **solo** `railway config plan --json` y `railway config plan --detailed-exit-code`, **sin** `--show-values`. Revisar todas las acciones y diferencias: **0 crear, 0 eliminar, 0 cambiar** es lo esperado para una importación fiel. Una primera adquisición de ownership partial podría aparecer separada; NO aplicar sin entender el resultado.
6. Si el plan presenta alteraciones de source, branch, builder, start command, restart policy, healthcheck, dominio, puerto, replicas, variables o permisos, **NO aplicar**. Corregir el candidato en el mismo PR hasta obtener un plan seguro.
7. Si el comando `railway config plan` informa que un servicio sigue administrado por `railway.toml`, revisar su configuración en dashboard y planificar el retiro de la ruta antigua con una ventana controlada. No convertir un error del plan en permiso de aplicar.
8. Comprobar `git diff` y `git status`: no incluir secretos, archivos binarios, exportaciones crudas de variables ni planes guardados. Solo cuando el plan esté documentado, los revisores y el propietario autoricen **merge** del PR y, por separado, **apply** de IaC.

### Revisión posterior a una autorización de apply (NO ejecutar ahora)

- Guardar evidencia del plan aprobado y el SHA exacto de IaC.
- Aplicar mediante `railway config apply` interactivo revisando otra vez el plan. No usar `--yes` ni `--confirm-destructive` en una primera migración.
- Comprobar que servicios y dominios mantienen sus IDs y ramas, que los tres están HEALTHY/SUCCESS y `GET /api/ready` devuelve 200.
- Validar worker Google Wallet, outbox y saldos sin nuevas publicaciones en masa.
- Validar migrador one-shot, migración en `9d0e1f2a3b4c`, RLS y recuentos de tablas sin alteraciones.
- Comprobar que no se introdujeron duplicados de servicios ni variables, que `railway config plan` posterior queda limpio y que la partial se registró.
- No abrir un PR a staging hasta que se valide la migración y el propietario autorice el flujo de ramas.

## Razón de mantener IaC review-only

El `railway.toml` heredado imponía `DOCKERFILE` y política de reinicios ON_FAILURE de forma compartida. El estado activo observado es diferente para web y migrador, por lo que copiar la configuración antigua literalmente modificaría servicios. La referencia aquí es **el estado activo de Railway**, no los defaults del TOML.

El heredado `railway.toml` se elimina de esta rama candidata conforme a la guía de Railway; **no se ha borrado de `main` ni de `staging`**.

Fuentes:
- https://docs.railway.com/infrastructure-as-code
- https://docs.railway.com/infrastructure-as-code/reference
- https://docs.railway.com/config-as-code

El corte definitivo de Config as Code para servicios heredados se documenta para **2026-12-01**. Priorizar la comprobación y adopción antes de esa fecha, pero nunca saltarse el `plan` para cumplir el calendario.
