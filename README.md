# Respaldo de Wanderlog en Railway

Servicio: `wanderlog-backup`, proyecto `JapanTrip`, entorno `production`.
Bucket existente: `functional-bottle`. Ejecución cada dos horas entre las
**08:00 y las 22:00, hora de Madrid durante el horario de verano**: 08:00,
10:00, 12:00, 14:00, 16:00, 18:00, 20:00 y 22:00. Railway interpreta cron en
UTC, por lo que la expresión configurada es `0 6-20/2 * * *`.
El proceso termina al acabar y no expone ningún servidor público.

## Qué conserva

- `source.html`: respuesta original de Wanderlog.
- `trip.json`: datos estructurados completos del viaje (itinerario, reservas, notas, presupuesto y metadatos).
- `resources.json`: datos auxiliares que proporciona Wanderlog para ese viaje.
- `emails/`: respuestas JSON y texto de los correos adjuntos, sin duplicados.
- `attachments/`: archivos adjuntos descargables, incluidos PDF.
- `images/`: fotografías accesibles de los lugares y portada.
- `index.html`: lector local independiente, sin scripts ni recursos externos.
- `manifest.json`: inventario, referencias originales, errores, tamaños y SHA-256.

Los enlaces a webs externas se preservan como datos: no se copian sitios de hoteles,
mapas interactivos, portales de reservas ni páginas enlazadas. Los cuerpos HTML de
correo se preservan en JSON, sin cargar sus píxeles de seguimiento. Si una imagen
externa ya no permite descargarla, se conserva su referencia y se registra el error.
La copia no se presenta como completa si falla un correo o archivo adjunto.

## Organización del bucket

```text
wanderlog/19396301/
  latest.json
  AAAA-MM-DDTHH-MM-SSZ-identificador/
    backup.zip
    manifest.json
```

Cada ejecución crea una carpeta nueva. No elimina copias antiguas. `latest.json`
apunta a la última copia con datos y adjuntos descargados; su estado puede indicar
`complete_with_media_errors` cuando fallan fotografías externas. Consultar siempre
`errors` en el manifiesto. Una copia con errores en correos o archivos se conserva
como `partial` y no actualiza `latest.json`.

Antes de publicar `latest.json`, se vuelve a descargar el ZIP del bucket y se
verifica su SHA-256. La metadata del ZIP también contiene su hash.

## Recuperar una copia

Descargar el ZIP indicado por `archiveKey` en `latest.json`, extraerlo y abrir
`index.html`. Los JSON permiten reutilizar los datos; Wanderlog no ofrece aquí una
restauración automática comprobada. No ejecutar el HTML original: usar el lector
local generado.

## Ejecutar manualmente y consultar registros

Desde una carpeta vinculada al proyecto:

```sh
railway service redeploy --service wanderlog-backup
railway logs --service wanderlog-backup --latest --lines 50
```

El evento `backup_uploaded_verified` confirma la copia y contiene su clave,
tamaño, estado y número de errores. `backup_failed` marca fallos de ejecución.

## Configuración

Las credenciales se resuelven mediante referencias al bucket en las variables del
servicio, nunca en los archivos fuente. Variables necesarias:
`AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_S3_BUCKET_NAME`, `AWS_DEFAULT_REGION`, `AWS_S3_URL_STYLE`, `WANDERLOG_URL`.
`EXPECTED_PLAN_ID=19396301` evita archivar por error otro viaje.

El enlace de Wanderlog debe seguir permitiendo el acceso de lectura. Un cambio
de permisos o de formato de la aplicación puede requerir actualizar el extractor.
Se usa la página y los endpoints de lectura utilizados por su cliente web;
no son una API pública estable garantizada por Wanderlog.

Para cambiar el horario, editar `deploy.cronSchedule` en `railway.json` y desplegar:

```sh
railway up . --path-as-root --service wanderlog-backup
```

Para probar sin subir datos al bucket:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
WANDERLOG_URL='URL_DEL_PLAN' .venv/bin/python backup.py --local-dir snapshots/prueba
```

El directorio de prueba debe ser nuevo para evitar mezclar archivos de distintas
versiones. El código solo hace peticiones de lectura a Wanderlog.
