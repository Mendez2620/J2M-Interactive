# Worker histórico de correos de registro

La Fase 8A retiró el correo del alta pública. Ninguna solicitud nueva crea `customer_registration_challenges` ni `registration_email_jobs`, y el servicio de staging ya no necesita un worker de correo de registro.

Las tablas y el código histórico se conservan temporalmente para evitar pérdida de auditoría y permitir revisar trabajos anteriores. No debe iniciarse el worker en un despliegue nuevo. Antes de retirar definitivamente comandos, secretos y tablas se debe:

1. verificar que no queden trabajos `pending` o `processing`;
2. definir si deben cancelarse o conservarse según la política de retención;
3. realizar y comprobar un respaldo;
4. ejecutar una migración incremental revisada, nunca una eliminación manual.

Este documento no autoriza cambios en Railway o Supabase. La limpieza física queda fuera de la Fase 8A.
