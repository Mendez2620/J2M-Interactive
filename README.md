# J2M Interactive — Plataforma SaaS de Tarjetas de Lealtad Digitales

Plataforma SaaS Multi-Tenant para la emisión, gestión y sincronización en tiempo real de **Tarjetas de Lealtad Digitales** compatibles con **Apple Wallet** (`.pkpass`) y **Google Wallet**.

---

## 🚀 Características Principales

- 🏢 **Multi-Tenant / Marca Blanca**: Cada negocio personaliza su nombre, logo, colores hexadecimales primarios/secundarios, coordenadas geográficas y reglas de lealtad.
- 🏷️ **Soporte Dual de Programas**:
  - **Sellos / Estampas (`stamps`)**: Acumulación por visita y canje automático de recompensas.
  - **Puntos (`points`)**: Multiplicador por monto de compra ($) y canje flexible de puntos.
- 📱 **Pases Nativos de Wallet**:
  - **Google Wallet**: Generación de JWTs firmados con **RS256** y llamadas a la REST API de Google Wallet.
  - **Apple Wallet**: Empaquetado binario `.pkpass` en memoria con firma digital PKCS7, manifest SHA-1 y activos gráficos.
- 🌐 **Web Service Oficial de Apple Wallet (`/api/apple/v1`)**:
  - Registro de dispositivos (`pushToken`).
  - Sincronización automática de saldos en tiempo real mediante notificaciones push APNs silenciosas.
  - Entrega del pase `.pkpass` actualizado en la pantalla de bloqueo de iOS.
- 📍 **Geofencing & Notificaciones de Proximidad**: Muestra la tarjeta automáticamente en la pantalla bloqueada del cliente cuando se aproxima a la sucursal física.
- 🎂 **Registro Público con Cumpleaños (`/join/<slug>`)**: Onboarding responsivo para clientes con captura de fecha de nacimiento y botones directos de guardado en Wallet.
- 📊 **Panel Super Admin (`/admin`)**: acceso por sesión y CSRF para administradores globales separados del staff de cada negocio; incluye KPIs, branding y gestión de usuarios.
- 📷 **Web App de Escaneo Adaptativa (`/scanner`)**: Terminal móvil con lector de cámara QR (`html5-qrcode`), entrada manual / NFC, y botones de transacción adaptados al programa del negocio.

---

## 📂 Arquitectura del Proyecto

```text
J2M Interactive/
├── app/
│   ├── __init__.py                 # Application Factory (create_app)
│   ├── config.py                   # Configuraciones por entorno (.env)
│   ├── extensions.py               # SQLAlchemy, Migrate, CORS
│   ├── models/                     # Modelos de Base de Datos (ORM)
│   │   ├── __init__.py
│   │   ├── apple_device.py         # Registro de dispositivos Apple para APNs
│   │   ├── business.py             # Tenant / Negocio y Geocercas
│   │   ├── customer.py             # Cuentas de clientes, saldos y cumpleaños
│   │   ├── staff.py                # Usuarios Staff (Admin, Manager, Cashier)
│   │   └── transaction.py          # Historial de acumulación y canjes
│   ├── routes/                     # Controladores y Endpoints
│   │   ├── __init__.py             # Registro de Blueprints y Health Check
│   │   ├── admin.py                # Dashboard y Gestión Super Admin
│   │   ├── apple_webservice.py     # Protocolo REST API de Apple Wallet
│   │   ├── auth.py                 # Módulo de Autenticación
│   │   ├── business.py             # Registro público /join/<slug>
│   │   ├── customer.py             # Descarga y enlaces de pases
│   │   └── scanner.py              # Web App y API de Escaneo
│   ├── services/                   # Lógica de Integración con Wallets
│   │   ├── __init__.py
│   │   ├── apple_wallet.py         # PKCS7, pass.json y .pkpass ZIP builder
│   │   └── google_wallet.py        # RS256 JWTs y Google Wallet REST API
│   ├── templates/                  # Vistas HTML (Tailwind CSS)
│   │   ├── base.html               # Layout base responsivo
│   │   ├── join.html               # Formulario de registro público
│   │   ├── scanner.html            # Web app móvil del escáner
│   │   └── admin/
│   │       └── dashboard.html      # Panel Super Admin y métricas
│   └── utils/
│       └── __init__.py
├── tests/                          # Suite completa de pruebas unitarias
│   ├── test_apple_wallet.py        # Tests de generación .pkpass y firmas
│   ├── test_google_wallet.py       # Tests de JWTs y esquemas Google Wallet
│   ├── test_phase3.py              # Tests de registro, admin y transacciones
│   └── test_phase4.py              # Tests de Web Service Apple y parches REST
├── .env.example                    # Plantilla de variables de entorno
├── .gitignore                      # Exclusiones de Git (venv, keys, .pkpass)
├── requirements.txt                # Dependencias Python
└── run.py                          # Punto de entrada de la aplicación
```

---

## 🛠️ Instalación y Ejecución Local

### 1. Prerrequisitos
- Python 3.10+
- Git

### 2. Clonar el repositorio y crear el entorno virtual
```bash
git clone https://github.com/Mendez2620/J2M-Interactive.git
cd "J2M Interactive"

# Crear entorno virtual
python -m venv venv

# Activar en Windows (PowerShell)
.\venv\Scripts\Activate.ps1
# O en Linux/macOS:
# source venv/bin/activate
```

### 3. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno
Copia la plantilla `.env.example` a `.env`:
```bash
cp .env.example .env
```

En desarrollo local, el sistema utiliza **SQLite** (`sqlite:///j2m_loyalty.db`) y genera automáticamente claves criptográficas transitorias si no se configuran certificados reales.

### 5. Aplicar migraciones y crear el primer administrador
```bash
python -m flask --app run.py db upgrade
python -m flask --app run.py create-platform-admin
```
El comando solicita nombre, correo, teléfono y contraseña sin incorporar secretos al código. La contraseña debe tener al menos 12 caracteres, mayúsculas, minúsculas y un número. Producción se niega a iniciar con la clave predeterminada o con una `SECRET_KEY` menor de 32 caracteres, fuerza `DEBUG=False` y debe usar `SESSION_COOKIE_SECURE=true` bajo HTTPS.

### 6. Iniciar la aplicación
```bash
python run.py
```
La aplicación estará disponible en:
- **Página de Escáner**: `http://127.0.0.1:5000/scanner`
- **Login Super Admin**: `http://127.0.0.1:5000/admin/login`
- **Health Check**: `http://127.0.0.1:5000/api/health`

---

## 🧪 Ejecución de Pruebas Unitarias

El proyecto cuenta con una suite completa de **47 pruebas unitarias e integración**. El mismo comando se ejecuta automáticamente en GitHub Actions para pushes y pull requests:

```bash
python -m unittest discover tests -v
```

---

## 🔗 Rutas y Endpoints Principales

| Método | Endpoint | Descripción |
| :--- | :--- | :--- |
| `GET` | `/api/health` | Health Check de la aplicación |
| `GET` | `/join/<slug>` | Página pública de registro de clientes |
| `POST` | `/join/<slug>` | Procesa registro, guarda cumpleaños y retorna enlaces a Wallets |
| `GET` | `/admin` | Panel de Control Super Admin con métricas demográficas |
| `GET/POST` | `/admin/login` | Inicio de sesión del administrador global |
| `POST` | `/admin/logout` | Cierre de sesión administrativa |
| `POST` | `/admin/users/create` | Alta de administrador global |
| `POST` | `/admin/users/edit/<id>` | Edición de administrador global (el nombre histórico del parámetro es `staff_id`) |
| `POST` | `/admin/business/<id>/staff` | Creación de Gerentes y Cajeros |
| `POST` | `/admin/staff/<id>/reset-password` | Restablecimiento no destructivo de contraseña de staff |
| `GET` | `/scanner` | Terminal web de escaneo QR adaptativa |
| `POST` | `/api/scanner/lookup` | Búsqueda de cliente por QR token, ID o teléfono |
| `POST` | `/api/scanner/process` | Procesa abono/canje y dispara sincronización en tiempo real |
| `GET` | `/api/customers/<id>/wallet/google` | Obtiene URL directa para Google Wallet |
| `GET` | `/api/customers/<id>/wallet/apple` | Descarga directa del pase nativo `.pkpass` |
| `POST` | `/api/apple/v1/devices/.../registrations/...` | Registro oficial de dispositivo Apple para push APNs |
| `GET` | `/api/apple/v1/passes/<pass_type>/<serial>` | Entrega del `.pkpass` actualizado tras transacción |

---

## 🔐 Configuración para Producción

### Imágenes de negocios
Los formularios administrativos aceptan `logo_file` y `cover_file` en PNG, JPG o WEBP, con máximo de 5 MB por imagen y 12 MB por solicitud. Pillow decodifica y verifica el contenido real antes de escribir el lote completo; si una imagen o el commit falla, se eliminan solamente los archivos recién creados. Se guardan con nombres únicos bajo `app/static/uploads/`, directorio excluido de Git. La vista pública usa el logo y la portada subidos; `logo_url` continúa disponible como alternativa remota.

Google Wallet usa el archivo local únicamente cuando `PUBLIC_BASE_URL` contiene la URL pública HTTPS del despliegue. Apple Wallet convierte el logo y portada validados a recursos PNG del paquete. La aceptación final por Google, las dimensiones/revisión visual de Apple y la distribución real siguen dependiendo de credenciales, consolas y dispositivos externos.

### Limitación de downgrade inicial

La revisión inicial `9a85c3791398` funciona tanto como bootstrap de una base vacía como actualización de una base heredada. Por ello `flask db downgrade` está bloqueado deliberadamente en esa revisión: no existe información histórica suficiente para distinguir qué tablas pertenecían previamente a una instalación heredada. El bloqueo ocurre antes de eliminar columnas o tablas. Para revertir, restaura un backup verificado en lugar de ejecutar una eliminación destructiva.

### Seguridad pendiente de las APIs operativas
El login web administrativo sí usa sesiones seguras y comprobación de cuenta activa. En cambio, `/api/scanner/login` aún devuelve un token simulado y las rutas de manager confían en un `staff_id` enviado por el cliente; no deben considerarse mecanismos de producción. La siguiente prioridad es emitir JWT firmados con expiración/rotación, validar el token en cada operación y derivar `staff_id` y `business_id` exclusivamente de sus claims, incluyendo pruebas de aislamiento entre tenants.

### Google Wallet
1. En **Google Cloud Console**, habilita la **Google Wallet API**.
2. Crea una **Service Account** y descarga su clave en formato JSON.
3. En **Google Pay & Wallet Console**, crea tu **Issuer Account** y vincula el email de la Service Account.
4. Configura en tu `.env` de producción:
   ```env
   GOOGLE_ISSUER_ID=3388000000000000000
   GOOGLE_SA_EMAIL=tu-sa@tu-proyecto.iam.gserviceaccount.com
   GOOGLE_SA_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
   ```

### Apple Wallet
1. En el **Apple Developer Portal**, genera un **Pass Type ID** (ej. `pass.com.tuempresa.loyalty`).
2. Genera el **Certificado de Pase** (`Pass Type ID Certificate`), exporta la llave y cert en formato PEM.
3. Descarga el certificado **Apple WWDR G4**.
4. Coloca los archivos en la carpeta `certs/` y configura en tu `.env`:
   ```env
   APPLE_PASS_TYPE_ID=pass.com.tuempresa.loyalty
   APPLE_TEAM_ID=TU_TEAM_ID_APPLE
   APPLE_CERT_PATH=certs/passcert.pem
   APPLE_KEY_PATH=certs/passkey.pem
   APPLE_WWDR_PATH=certs/wwdr.pem
   APPLE_WEB_SERVICE_URL=https://api.tudominio.com/api/apple/v1
   ```

Sin credenciales reales, las pruebas usan el modo mock de Wallet: validan estructura de pases, JWT y llamadas simuladas, pero no prueban emisión efectiva en cuentas de Google/Apple, APNs ni instalación en dispositivos físicos.

---

## 📄 Licencia
Propiedad de **J2M Interactive**. Todos los derechos reservados.
