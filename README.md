# CampusRadar

**Plataforma colaborativa de información universitaria** — EL-4203 Programación Avanzada · FCFM · Universidad de Chile.

CampusRadar es una plataforma web (estilo SOSAFE/Waze, para el campus) que permite a la comunidad **reportar, validar y visualizar** información en tiempo real: fallas de infraestructura, alertas de emergencia, actividades, eventos e información logística.

Los reportes tienen un **ciclo de vida dinámico** (nuevo → verificado / controvertido / crítico / expirado / archivado) que se calcula con **lazy evaluation** —sin timers ni `time.sleep`— a partir de la antigüedad, las confirmaciones/desmentidos y la reputación de los usuarios.

---

## Funcionalidades

- **Reportes** de 5 tipos (infraestructura, actividad extraprogramática, alerta de emergencia, evento universitario, información logística) con autor, ubicación, tags, descripción y estado.
- **Ciclo de vida perezoso** del estado del reporte (sin mecanismos bloqueantes).
- **Usuarios y roles** (estudiante, moderador, administrador) con permisos diferenciados y **autenticación JWT**.
- **Reputación** de usuarios y validación comunitaria (confirmar / desmentir ponderado por reputación).
- **Interacciones**: comentar, confirmar, desmentir, denunciar.
- **Tags, búsqueda y feed** ordenado por prioridad (ranking polimórfico).
- **Geolocalización** con mapa Leaflet + filtros por ubicación.
- **Moderación** (archivar, denunciar, voto único, no votar el propio reporte).
- **Notificaciones y suscripciones**: seguir un **tag** o un **edificio** y recibir avisos de nuevos reportes que coincidan.
- **Interfaz web** funcional (dashboard, mapa, formularios) + **API REST** documentada en `/docs`.

---

## Arquitectura

Separación estricta de responsabilidades en capas (un objetivo explícito del proyecto):

| Capa | Módulo | Responsabilidad |
|------|--------|-----------------|
| Dominio (POO puro) | `app/domain.py` | Entidades y reglas de negocio sin frameworks. Herencia, polimorfismo, abstracción, encapsulamiento, composición. |
| Persistencia | `app/persistence.py` | Modelos ORM SQLAlchemy (Single Table Inheritance) + factory de engine. |
| Repositorios | `app/repositories.py` | Patrón Repository + Unit of Work. Mappers ORM ↔ dominio. |
| Servicios | `app/services.py` | Lógica de negocio de orquestación (fan-out de notificaciones). |
| API REST | `app/api.py` | Endpoints HTTP, schemas Pydantic, auth JWT. |
| Web | `app/api_web.py` | Vistas Jinja2 (cookie-auth). |
| Config | `app/config.py` | Configuración tipada desde entorno/`.env`. |

### Estructura del repositorio

```
.
├── app/
│   ├── __init__.py
│   ├── config.py                 # Settings (variables de entorno + .env)
│   ├── domain.py                 # Dominio POO puro
│   ├── persistence.py            # ORM SQLAlchemy + crear_engine (SQLite/PostgreSQL)
│   ├── repositories.py           # Repository + Unit of Work
│   ├── services.py               # NotificacionService
│   ├── api.py                    # API REST
│   ├── api_web.py                # Capa web (Jinja2)
│   ├── main.py                   # Entry point -> app.main:app
│   └── templates/                # index, login, registro, notificaciones
├── tests/
│   ├── conftest.py               # Harness compartido (BD en memoria, fixtures)
│   ├── test_main.py
│   └── test_notificaciones.py
├── docs/                         # Enunciado del proyecto (PDF)
├── .env.example                  # Plantilla de variables de entorno
├── .dockerignore / .gitignore
├── Dockerfile
├── docker-compose.yml            # app + SQLite (por defecto)
├── docker-compose.postgres.yml   # override para PostgreSQL
├── pyproject.toml                # metadata + configuración de pytest
├── requirements.txt
└── README.md
```

---

## Stack tecnológico

Python 3.10+ (probado en 3.12) · FastAPI · Uvicorn · SQLAlchemy 2.0 · Pydantic 2 · pydantic-settings · Jinja2 + Tailwind (CDN) · Leaflet · passlib[bcrypt] + python-jose (JWT) · pytest · Docker. PostgreSQL opcional vía psycopg2.

---

## Puesta en marcha

### Opción A — Local (entorno virtual)

```bash
# Windows (PowerShell)
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Abre <http://localhost:8000/>. La primera vez se crean usuarios demo automáticamente.

### Opción B — Docker (SQLite, sin instalar dependencias)

```bash
docker compose up --build -d          # levantar
docker compose logs -f campusradar    # logs
docker compose down                   # bajar  (down -v para borrar la BD)
```

### Opción C — Docker con PostgreSQL

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build -d
```

Levanta un servicio `db` (postgres:16) y apunta la app a él vía `DATABASE_URL`. La app espera a que la BD esté *healthy* antes de arrancar.

---

## Configuración (variables de entorno)

La configuración se lee del entorno y, opcionalmente, de un archivo `.env` (ver `.env.example`). **Nunca subas el `.env` real** (está en `.gitignore`).

| Variable | Por defecto | Descripción |
|----------|-------------|-------------|
| `SECRET_KEY` | *(clave de desarrollo)* | Clave para firmar los JWT. **Cámbiala en producción.** |
| `ALGORITHM` | `HS256` | Algoritmo de firma del JWT. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `480` | Validez del token (minutos). |
| `DATABASE_URL` | `campusradar.db` | Ruta SQLite simple **o** URL completa (`postgresql+psycopg2://user:pass@host:5432/db`). |

---

## Usuarios demo (se crean si la BD está vacía)

| Rol | Email | Contraseña |
|-----|-------|-----------|
| Administrador | `admin@campus.cl` | `admin123` |
| Estudiante | `estudiante@campus.cl` | `est123` |

También puedes registrarte en <http://localhost:8000/web/registro>.

---

## API REST

Documentación interactiva en **`/docs`**. Principales endpoints (la mayoría requieren `Authorization: Bearer <token>`):

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/auth/registro` · `/auth/login` | Registro / login (devuelven JWT) |
| GET | `/auth/me` | Perfil del usuario autenticado |
| POST | `/reportes/` | Crear reporte |
| GET | `/reportes/feed` | Feed ordenado por prioridad |
| GET | `/reportes/buscar` | Búsqueda por tags/edificio/tipo/autor |
| GET | `/reportes/mapa` | Reportes geolocalizados |
| POST | `/reportes/{id}/confirmar` · `/desmentir` · `/comentar` · `/denunciar` | Interacciones |
| DELETE | `/reportes/{id}/archivar` | Archivar (moderador/admin) |
| POST/GET/DELETE | `/suscripciones/` | Seguir tag/edificio, listar, dejar de seguir |
| GET | `/notificaciones/` · `/notificaciones/conteo` | Listar / contar no leídas |
| POST | `/notificaciones/{id}/leer` · `/notificaciones/leer-todas` | Marcar leídas |

Vistas web: `/` (dashboard), `/web/login`, `/web/registro`, `/web/notificaciones`, `/web/logout`.

---

## Tests

```bash
pip install -r requirements.txt
pytest
```

Cubren autenticación, reputación dinámica, ciclo de vida perezoso, voto único, moderación, feed y la funcionalidad de suscripciones/notificaciones (incluido un test de dominio puro). El harness (BD SQLite en memoria) está en `tests/conftest.py`.

---

## Despliegue

La aplicación se despliega fácilmente con Docker (ver arriba). Para la nube (Render, Railway, etc.): usar el `Dockerfile`, definir `SECRET_KEY` y `DATABASE_URL` como variables del servicio, y montar un disco/volumen para la persistencia (o usar PostgreSQL gestionado).

---

## Decisiones de diseño destacadas

- **Lazy evaluation del estado**: el estado del reporte es una *property* que se recalcula al consultarse (UTC timezone-aware en todo el dominio), evitando timers/hilos.
- **Dominio desacoplado del ORM**: el dominio es POO puro y testeable sin BD; los repositorios traducen ORM ↔ dominio.
- **Single Table Inheritance** para reportes, usuarios e interacciones (comparten columnas, difieren en comportamiento que vive en el dominio).
- **Independencia del motor de BD**: programando contra SQLAlchemy, cambiar de SQLite a PostgreSQL es solo cambiar `DATABASE_URL`.
#   m i s h _ b u x e f  
 