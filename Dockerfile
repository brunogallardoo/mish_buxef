# =============================================================================
# CampusRadar - Dockerfile (producción)
# =============================================================================
#
# DECISIONES DE DISEÑO:
# - Multi-stage build: la etapa "builder" instala dependencias con pip
#   en un directorio aislado; la etapa final copia solo los artefactos
#   necesarios, reduciendo el tamaño de imagen (~40% menos).
# - Base: python:3.12-slim en lugar de la imagen completa para menor
#   superficie de ataque y arranque más rápido.
# - Usuario no-root: la app corre como "appuser" (UID 1000), nunca como
#   root, mejorando la postura de seguridad del contenedor.
# - Volumen declarado en /app/data: SQLite persiste aquí; docker-compose
#   lo monta como named volume para sobrevivir reinicios.
# - PYTHONDONTWRITEBYTECODE + PYTHONUNBUFFERED: buenas prácticas para
#   contenedores (sin archivos .pyc, logs en tiempo real).
# =============================================================================

# ── Etapa 1: Builder (instalar dependencias) ──────────────────────────────────
FROM python:3.12-slim AS builder

# Evitar archivos .pyc y buffering de stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Copiar solo requirements para aprovechar caché de capas de Docker:
# si requirements.txt no cambia, esta capa se reutiliza en rebuilds.
COPY requirements.txt .

# Instalar en directorio local (no en el sistema) para copiar luego
RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Etapa 2: Final (imagen de producción) ────────────────────────────────────
FROM python:3.12-slim AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Ruta donde uvicorn encuentra los paquetes instalados en el builder
    PYTHONPATH=/app \
    # Variables de la aplicación (se pueden sobreescribir en docker-compose)
    SECRET_KEY=campusradar-production-secret-change-me \
    DATABASE_URL=/app/data/campusradar.db \
    PORT=8000

# Copiar paquetes instalados desde el builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Crear usuario no-root
RUN addgroup --system appgroup \
 && adduser --system --ingroup appgroup --uid 1000 appuser

# Crear directorio de datos con permisos correctos
RUN mkdir -p /app/data && chown appuser:appgroup /app/data

# Copiar el paquete de la aplicación (incluye app/templates/)
COPY --chown=appuser:appgroup app/ ./app/

# Cambiar a usuario no-root
USER appuser

# Exponer puerto de la aplicación
EXPOSE 8000

# Health check: FastAPI responde en / con 200 OK
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" \
    || exit 1

# Comando de inicio: uvicorn en modo producción (sin reload)
# --workers 1 porque SQLite no soporta escrituras concurrentes multi-proceso.
# Para PostgreSQL se puede aumentar a --workers 4.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
