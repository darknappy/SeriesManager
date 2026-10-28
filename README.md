# SeriesAdmin

Gestor local de biblioteca de series para Jellyfin, con CLI y panel web en español.
Ordena carpetas y archivos con nombres compatibles con Jellyfin, detecta episodios
faltantes, duplicados y emparejamientos incorrectos, y mantiene un catálogo separado
para el anime que está en emisión.

> **Regla de seguridad:** nada se renombra, mueve ni borra hasta que revises el plan
> y escribas `APLICAR`. Los duplicados y descartes van a cuarentena, nunca se eliminan.
> Los cambios de catálogo, identificadores y alias no tocan los archivos.

Repositorio público: <https://github.com/darknappy/SeriesManager>

---

## Estado de esta instalación

Estado verificado el 28 de octubre de 2026:

- **46 fichas** de serie y **46 grupos de carpeta** de origen.
- **2.369 filas de archivo** en el catálogo, con **1.091 vídeos** y **1.275 imágenes**
  presentes (incluidas las miniaturas generadas por Jellyfin).
- **1.091 vídeos interpretados**, **0 sin interpretar**, **0 desaparecidos** y
  **0 grupos duplicados**. Solo quedan **3 archivos en cuarentena**.
- **InuYasha** sigue siendo una sola ficha: 7 temporadas, 193 episodios y TMDB
  `35610`.
- Quedan **6 series con 11 huecos aislados**: Rooster Fighter S01E1-E3, You and I
  Are Polar Opposites S02E1/E2/E13, VisionQuest S01 (2), Accelerator S01E7,
  Trapped in a Dating Sim S02E11 y Uma Musume S01E13.
- Quedan **4 fichas sin TVmaze**: los tres OVA de Macross con TMDB manual y
  `Ranma`, identificada manualmente con TMDB `259140`. Ranma se detecta por archivos
  (3 temporadas y 25 vídeos) y **no genera huecos falsos**.
- **0 carpetas pendientes de identificar** y **0 series por revisar**.
- Las carpetas nuevas **The Ramparts of Ice**, **A Certain Scientific Accelerator**,
  **To Aru Kagaku no Railgun S/T** y **Toaru Majutsu no Index III** ya están
  identificadas; las secuelas de Railgun e Index se asignaron a sus fichas y
  temporadas existentes.
- **A Certain Magical Index** quedó con **76 episodios** (S00-S03) y
  **A Certain Scientific Railgun** con **78** (S00-S03). El OVA nuevo de Railgun
  ocupa `S00E01`; los especiales anteriores pasaron a `S00E02-E05`. Los **88
  movimientos** de ambas series se aplicaron sin conflictos y **0 nombres**
  conservan el prefijo duplicado.
- Se corrigió la acumulación de prefijos del título de la serie en los nombres
  Jellyfin. La limpieza es **idempotente**: volver a planificar un nombre canónico no
  añade otra copia del prefijo.
- El **plan 5** y el **plan 7** se aplicaron. El **plan 9** quedó `partial`; los
  planes **8 y 10** se marcaron como `stale` porque se generaron antes de corregir
  los prefijos. El **plan 11** se aplicó el 28 de octubre: **220 operaciones**
  (219 movimientos y 1 cuarentena), **0 errores** y **22,3 GB**. El informe está en
  `data/reports/plan_11.html`. El **plan 12** se aplicó después como reparación
  puntual: **50 renombrados** (25 vídeos y 25 miniaturas de Ranma), **0 errores** y
  **4,2 GB**, retirando el título genérico `Ranma 1 2 2024`; su informe está en
  `data/reports/plan_12.html`. **No hay ningún plan listo pendiente.** El plan 9
  queda como registro histórico; no conviene deshacerlo después del plan 11.
- La verificación de idempotencia sobre una copia temporal del catálogo no propone
  ningún movimiento de episodios ni miniaturas en Index/Railgun. Se retiraron 110
  registros históricos de archivos que ya no existían; **no se borró ningún archivo
  de la biblioteca**.

La carpeta `D:\Series\En emision` es una zona de descargas en curso: **no forma parte
del catálogo de series** y se usa para detectar qué títulos de la temporada ya están
descargados.

---

## Qué incluye

- Escaneo de `D:\Series` y de `D:\SeriesEnEmision`.
- Detección de temporada/episodio en nombres de archivo muy variados, incluidos
  títulos que contienen números como `Ranma_1_2_2024_01`.
- Metadatos y episodios desde **TVmaze**; catálogo de temporada y próximos estrenos
  desde **AniList**. Sin claves de TMDB.
- Emparejamiento con candidatos de TVmaze/AniList y edición manual de IDs.
- **Identificación automática de carpetas nuevas**: el escaneo busca candidatos para
  las fichas sin proveedor y sin búsquedas previas; `sync` reintenta las que aún no
  tengan candidatos. Usa títulos en inglés, romaji, nativos y sinónimos de AniList.
- **Nombres Jellyfin idempotentes**: se eliminan los prefijos repetidos de la serie y
  las marcas `SxxExx` embebidas antes de construir el nombre final.
- **Identificadores manuales** para carpetas cuyo nombre fue modificado.
- **Alias de carpeta** para vincular una carpeta renombrada a una ficha existente.
- Mapeo automático de carpetas con `[tmdbid-<id>]` y semillas para secuelas conocidas
  (`Index III`, `Railgun S/T`, `Accelerator`, `The Ramparts of Ice`).
- Detección de **carpetas sin identificar** con acceso directo al formulario de alta.
- **Descargas en curso**: carpetas de staging que no se escanean como series y se usan
  para marcar qué animes de la temporada ya tienes.
- Cartelera AniList de la temporada actual con **lista seleccionable**, seleccionar
  todo, seguimiento masivo y distintivos de biblioteca/descarga/seguimiento.
- Plan de renombrado/movimiento en seco, con informe HTML independiente.
- Detección de episodios faltantes, archivos idénticos y varios archivos por episodio.
- Cuarentena para duplicados y archivos retirados.
- Diario de operaciones y opción de deshacer un plan aplicado.
- Panel web local (FastAPI) y CLI (Click + Rich).
- Integración opcional con Jellyfin: probar conexión, crear la biblioteca de emisión,
  refrescar el catálogo y respaldar el estado visto.

---

## Requisitos

- Windows 10/11.
- Python 3.11 o superior.
- Paquetes de `requirements.txt`: `requests`, `click`, `rich`, `fastapi`, `uvicorn`,
  `jinja2`, `python-multipart`.
- Opcional: `ffmpeg`/`ffprobe` en el `PATH` (el proyecto no los necesita para el plan,
  pero ayudan a inspeccionar medios).
- Jellyfin en `http://127.0.0.1:8096` (opcional para previsualizar).

Instalación:

```bat
cd /d D:\SeriesAdmin
python -m pip install -r requirements.txt
copy config.example.json config.json
```

Edita `config.json` si tus bibliotecas no están en `D:\Series` y
`D:\SeriesEnEmision`.

---

## Arranque rápido

```bat
cd /d D:\SeriesAdmin

REM 1. Crear configuración, base de datos y semillas
python -m series_manager init

REM 2. Escanear la biblioteca (no modifica archivos)
python -m series_manager scan

REM 3. Sincronizar metadatos y episodios con TVmaze
python -m series_manager sync

REM 4. Revisar carpetas sin identificar y avisos
python -m series_manager unidentified
python -m series_manager status

REM 5. Generar el plan de previsualización y el informe HTML
python -m series_manager plan

REM 6. Revisar el informe en data\reports\plan_<id>.html
REM    y, solo si todo está bien:
python -m series_manager apply <id>
```

También puedes usar los lanzadores:

- `series.bat` → CLI (`series.bat status`, `series.bat plan`, …).
- `iniciar_panel.bat` → panel web en `http://127.0.0.1:5055`.

O todo en uno:

```bat
python -m series_manager rebuild
```

`rebuild` hace escaneo + identificación automática + sincronización + plan. Sigue sin
modificar archivos.

---

## Comandos principales

| Comando | Qué hace |
| --- | --- |
| `init` | Crea carpetas, base de datos y carga las semillas. |
| `scan` | Escanea la biblioteca, clasifica archivos e intenta identificar carpetas nuevas. Usa `--sin-huellas` para ir más rápido. |
| `sync` | Sincroniza series, temporadas y episodios con TVmaze; reintenta emparejar las fichas nuevas. `--forzar` ignora la caché. |
| `match` | Busca candidatos para series sin proveedor. |
| `unidentified` | Lista carpetas que aún no tienen una ficha identificada. |
| `identify <carpeta> --titulo ... [--tmdb ...] [--tvmaze ...] [--anilist ...]` | Crea una ficha manual para una carpeta renombrada. |
| `link-folder <serie> <carpeta>` | Vincula una carpeta renombrada a una ficha existente. |
| `downloads` | Lista los vídeos detectados en las carpetas de descargas en curso. |
| `airing --descubrir [--temporada fall] [--ano 2026] [--limite N]` | Muestra la cartelera AniList con estado de biblioteca, descarga y seguimiento. |
| `watch add-bulk <id> <id> …` | Añade varios animes de AniList al seguimiento. |
| `rebuild` | Escaneo + identificación automática + sincronización + plan. |
| `plan` | Genera el plan en seco y el informe HTML. `--abrir` abre el informe. |
| `apply <id>` | Aplica un plan. Pide confirmación; `--si` la omite. |
| `undo <id>` | Deshace un plan aplicado usando el diario. |
| `status` | Resumen del catálogo e incidencias. |
| `list` | Lista series; `--q texto` filtra. |
| `missing` | Huecos aislados; `--serie <id o título>` muestra el detalle y `--todos` incluye temporadas ausentes. |
| `duplicates` | Archivos idénticos y episodios con varios archivos. |
| `watch list/add/add-manual/edit/advance/remove/sync` | Gestiona la lista editable de seguimiento. |
| `web` | Inicia el panel local. |
| `doctor` | Diagnóstico de rutas, proveedores, espacio y Jellyfin. |
| `jellyfin test/refresh/create-library/backup` | Acciones de Jellyfin. |
| `report <id>` | Regenera el informe HTML de un plan. |
| `jobs` | Historial de trabajos. |

Todos los comandos aceptan `-h`/`--help`.

### Ejemplos de identificación manual

```bat
REM Listar carpetas pendientes
python -m series_manager unidentified

REM Crear una ficha para una carpeta renombrada
python -m series_manager identify "Mi carpeta rara" --titulo "Título real" --ano 2024 --tvmaze 12345 --tmdb 67890

REM Vincular una carpeta renombrada a una ficha que ya existe
python -m series_manager link-folder 13 "InuYasha temporada 7"
```

El ID local (`--clave`) es opcional. Si el TMDB/TVmaze/AniList indicado ya pertenece a
otra ficha, la carpeta se vincula a esa ficha canónica en lugar de crear un duplicado.

---

## Identificar carpetas con nombres modificados

El escaneo crea una ficha provisional cuando encuentra una carpeta que no coincide con
ninguna semilla o proveedor. Esas fichas aparecen en **Series → Carpetas sin
identificar** y también con `python -m series_manager unidentified`.

Hay tres formas de resolverlo, sin mover ni renombrar archivos:

1. **Formulario web:** **Series → Crear identificador**. Puedes abrirlo con
   `/series/new?carpeta=Nombre%20exacto` para que la carpeta y el título vengan
   precargados. Acepta título, año, TMDB, TVmaze, AniList, clave local y notas.
2. **Alias desde la ficha:** en **Series → ficha → Carpetas de origen → Vincular otra
   carpeta**, escribe el nombre exacto de la carpeta renombrada. La ficha conserva sus
   episodios y archivos; solo se añade el alias.
3. **CLI:** `identify` crea la ficha y `link-folder` la vincula a una existente.

Reglas de seguridad:

- El formulario no mueve, renombra ni borra nada.
- El siguiente `scan` asigna los archivos de esa carpeta a la ficha.
- Las carpetas con `[tmdbid-<id>]` se mapean automáticamente a la ficha que ya tiene
  ese TMDB; no necesitan identificador manual.
- Un identificador no inventa IDs: si no sabes el ID, deja el campo vacío y usa título,
  año y notas. La ficha seguirá existiendo y el catálogo sabrá que la carpeta es tuya.

---

## Descargas en curso y temporada AniList

### Carpetas de staging

Las descargas en curso no son series. Se configuran en **Ajustes → Carpetas de
descargas en curso** (una por línea) o con `staging_roots` en `config.json`:

```json
"staging_roots": [
  "D:\\Series\\En emision"
]
```

Además, el programa detecta automáticamente carpetas llamadas `En emision`, `Inbox` o
`Descargas` dentro de la biblioteca principal. Esas carpetas:

- no se escanean como series;
- no crean fichas falsas;
- no generan incidencias de series sin vídeo;
- se usan para saber qué títulos de la temporada ya tienes descargados.

`python -m series_manager downloads` muestra los vídeos detectados y su episodio.

### Cartelera interactiva

En **En emisión**, pulsa **Descubrir temporada** o abre directamente:

```text
http://127.0.0.1:5055/airing?descubrir=1&temporada=FALL&ano=2026
```

La tabla de AniList incluye:

- casillas de selección por título;
- **Seleccionar todos** y contador de seleccionados;
- envío masivo a la lista de seguimiento;
- distintivos **En biblioteca**, **Descargada** y **En seguimiento**;
- panel de **Descargas en curso** con los vídeos encontrados en staging.

El estado de descarga se calcula por título y temporada, tolerando nombres de release
con prefijos, guiones, años y calidades. Si una carpeta fue renombrada, identifícala
primero para que la detección sea más fiable.

Desde CLI:

```bat
python -m series_manager airing --descubrir --temporada fall --ano 2026 --limite 50
python -m series_manager watch add-bulk 209872 185731
```

---

## Panel web

```bat
python -m series_manager web
```

Abre `http://127.0.0.1:5055`. Secciones:

- **Panel:** estado general, último plan, incidencias, próximos estrenos y disco.
- **Series:** búsqueda, filtros, panel de carpetas sin identificar y ficha con
  temporadas, episodios, archivos, candidatos, alias de carpeta e identificadores.
- **Planes:** previsualización, filtros por acción, informe y aplicación/deshacer.
- **En emisión:** biblioteca en emisión, seguimiento editable, cartelera AniList
  seleccionable y descargas en curso.
- **Faltantes:** por defecto solo huecos aislados en temporadas ya empezadas; las
  temporadas ausentes se ocultan.
- **Duplicados:** grupos idénticos y episodios con varias versiones.
- **Incidencias:** avisos de escaneo, sincronización y emparejamiento.
- **Cuarentena:** archivos retirados de la biblioteca, con su operación de origen.
- **Informes:** planes HTML generados.
- **Trabajos:** historial y progreso en vivo.
- **Ajustes:** rutas, staging, reglas de nombres, proveedores y Jellyfin.

El panel escucha solo en `127.0.0.1` por defecto. No está pensado para exponerse a
Internet.

---

## Cómo se nombran las cosas

Estructura objetivo:

```text
D:\Series\InuYasha (2000) [tmdbid-35610]\Season 01\InuYasha (2000) - S01E01 - Título.mp4
D:\Series\InuYasha (2000) [tmdbid-35610]\Season 07\InuYasha (2000) - S07E01 - Título.mp4
D:\Series\Chobits (2002) [tmdbid-37527]\Season 00\Chobits (2002) - S00E01 - Especial.mp4
D:\SeriesEnEmision\Serie (2026) [tmdbid-12345]\Season 01\Serie (2026) - S01E05 - Título.mkv
```

Reglas:

- Carpeta de serie: `Título (Año) [tmdbid-<id>]`.
- Temporadas: `Season 01`, `Season 02`, …; especiales en `Season 00`.
- Archivo: `Título (Año) - SxxEyy - Título del episodio.ext`.
- Multi-episodio: `SxxEyy-Ezz`.
- El ID de TMDB solo se incluye si existe y es correcto. Si no, se omite para evitar
  que Jellyfin asocie la ficha equivocada.
- Los especiales decimales (8.5, 18.5) y las OVA se renumeran secuencialmente en
  `Season 00`.

Los nombres de archivo y carpeta se sanean para Windows (`:`, `/`, `\`, `*`, `?`, etc.)
sin perder acentos.

---

## Decisiones del catálogo actual

Estas reglas están codificadas en `data/series_seed.json` y `data/groups_seed.json`:

- **Dos bibliotecas Jellyfin:** `D:\Series` (principal) y `D:\SeriesEnEmision`
  («Anime en emisión»). Mover una serie a emisión es manual desde su ficha.
- **Series separadas:** `Macross`, `Macross II`, `Macross Plus`, `Macross Zero`,
  `Macross Delta`; `Ghost in the Shell: Stand Alone Complex`, `SAC_2045`, `Arise`
  y `The Ghost in the Shell` (2026).
- **Carpetas unidas:** las temporadas separadas de `Link Click`, `Vox Machina`,
  `Yashahime`, `100 Kanojo`, `Uma Musume`, `GitS SAC`, etc. se agrupan en una sola
  carpeta de serie con varias temporadas.
- **InuYasha:** una sola serie con 7 temporadas. Los archivos 001–167 usan numeración
  absoluta y se reparten entre las temporadas 1–6; los 26 episodios de Kanketsu-hen /
  The Final Act se colocan en `Season 07` bajo la misma ficha TMDB 35610. Así Jellyfin
  identifica la serie completa con un solo identificador.
- **MAO:** una sola temporada de 26 episodios (TVmaze la divide en dos de 13).
- **Smoking Behind the Supermarket with You:** los archivos dicen `S02`, pero solo
  existe una temporada; se corrigen a `Season 01`.
- **Specials de Chobits:** 8.5, 18.5, 24.5 y OVA se renumeran en `Season 00`.
- **Uma Musume:** OVA principal y especiales BNW se ordenan y renumeran en `Season 00`.
- **Ranma:** la carpeta `Ranma (2024) [tmdbid-259140]` contiene 25 vídeos en 3
  temporadas sin huecos falsos. Se retiró de los nombres el título genérico repetido
  `Ranma 1 2 2024`; los archivos quedan como `Ranma (2024) - SxxExx.mp4` y sus
  miniaturas. Tiene TMDB `259140` manual y no tiene TVmaze/AniList; la estructura de
  temporadas sale de los archivos.

---

## Plan, aplicación y deshacer

1. `scan` llena `files` con rutas, tamaños, huellas y episodios detectados.
2. `sync` crea temporadas/episodios canónicos y asigna cada archivo a su episodio.
   Para series sin proveedor usa la estructura real de los archivos, no una temporada
   sintética única.
3. `plan` construye `plan_actions` y un informe HTML. **No toca archivos.**
4. `apply` valida que origen y destino estén dentro de las carpetas permitidas, mueve
   con `shutil.move`, comprueba el tamaño y registra cada operación en `journal`.
5. `undo` recorre el diario en orden inverso y devuelve los archivos a su ruta original.

Duplicados y archivos descartados se mueven a:

```text
D:\SeriesAdmin\quarantine\plan-<id>\...
```

Nunca se borran archivos. Las carpetas vacías sí se limpian después de aplicar.

Si un plan tiene conflictos, no se aplican automáticamente. Revisa la pestaña
**Conflictos**, decide qué versión conservar y vuelve a generar el plan.

---

## Jellyfin

### Clave API

1. Jellyfin → Panel → **API Keys** → crear una clave.
2. Pegarla en el panel: **Ajustes → Jellyfin → Clave API**, o en `config.json`.
3. Probar con `python -m series_manager jellyfin test`.

La clave es opcional para escanear, sincronizar y planificar.

### Biblioteca de emisión

Con la clave configurada:

```bat
python -m series_manager jellyfin create-library
python -m series_manager jellyfin refresh
```

O desde **Ajustes → Acciones de Jellyfin**.

La biblioteca principal sigue apuntando a `D:\Series`. La nueva apunta a
`D:\SeriesEnEmision` y se llama «Anime en emisión».

### Estado visto

`jellyfin backup` copia el estado visto/posición desde la base local de Jellyfin a
`jellyfin_state`. Útil antes de reorganizar. La restauración automática no está
incluida: Jellyfin identifica episodios por metadatos, no por ruta, así que un
renombrado correcto debería conservar el estado.

---

## Proveedores

### TVmaze

- Búsqueda: `https://api.tvmaze.com/search/shows`
- Ficha con temporadas y episodios: `/shows/{id}?embed[]=seasons&embed[]=episodes`
- Es la fuente principal de temporadas, episodios, fechas y sinopsis.
- Límite de peticiones respetado con una pausa configurable.

### AniList

- GraphQL: `https://graphql.anilist.co`
- Se usa para la temporada actual, cartelera, estado y próximos episodios.
- Requiere un `User-Agent` de navegador; el cliente ya lo envía.
- Caché en disco de 12 h por defecto.

### TMDB

No se usa la API de TMDB. Los IDs se toman de la base local de Jellyfin, de las
semillas o se editan manualmente en la ficha de cada serie. Nunca se inventan IDs.

---

## Configuración

Copia `config.example.json` a `config.json`. Valores principales:

```json
{
  "library_root": "D:\\Series",
  "airing_root": "D:\\SeriesEnEmision",
  "staging_roots": ["D:\\Series\\En emision"],
  "include_tmdb_id": true,
  "include_episode_title": true,
  "include_quality": false,
  "keep_episode_thumbs": true,
  "auto_match_threshold": 0.8,
  "airing_horizon_days": 45,
  "jellyfin": {
    "url": "http://127.0.0.1:8096",
    "api_key": "",
    "db_path": "C:\\ProgramData\\Jellyfin\\Server\\data\\jellyfin.db",
    "airing_library_name": "Anime en emisión",
    "airing_library_path": "D:\\SeriesEnEmision"
  },
  "web": {"host": "127.0.0.1", "port": 5055, "open_browser": true}
}
```

`config.json` contiene rutas y claves locales y está excluido de Git. Ajustes guarda
los cambios y conserva la clave API si dejas el campo vacío.

Con `keep_episode_thumbs: true` se conservan y renombran las miniaturas locales de cada
episodio. Si lo pones en `false`, se envían a cuarentena (nunca se borran) y Jellyfin
descargará las suyas.

---

## Estructura del proyecto

```text
D:\SeriesAdmin\
  series_manager\
    cli.py            CLI en español
    web.py            panel FastAPI
    jobs.py           trabajos en segundo plano
    scan.py           escaneo y clasificación
    sync.py           metadatos y asignación de episodios
    catalog.py        reconciliación, identificadores y alias
    plan.py           plan en seco
    apply.py          aplicación y deshacer
    naming.py         parser y nombres Jellyfin
    matching.py       emparejamiento
    airing.py         emisión, staging y seguimiento
    jellyfin.py       API/base local de Jellyfin
    reports.py        informes HTML
    providers\        TVmaze y AniList
    db.py             esquema SQLite
    config.py         configuración
  templates\          vistas Jinja y plan_report.html
  static\             CSS y JS
  data\
    series_seed.json  fichas canónicas
    groups_seed.json  carpetas de origen
    reports\          informes HTML (no versionado)
    cache\            caché HTTP (no versionada)
  quarantine\         duplicados y descartes (no versionado)
  tests\              pruebas unitarias
  config.example.json configuración de ejemplo
  requirements.txt    dependencias
```

---

## Pruebas

```bat
cd /d D:\SeriesAdmin
python -m unittest discover -s tests -v
```

La suite actual tiene **35 pruebas** y cubre el parser de nombres (incluidos títulos
con números), la generación de rutas, el esquema SQLite, las huellas, la validación de
rutas, la reconciliación del catálogo, la detección de descargas en staging y la
sincronización de series sin proveedor.

---

## Problemas frecuentes

**«Me dice que falta una serie que sí está»**

Primero comprueba que la carpeta no esté en `D:\Series\En emision` o en otra carpeta de
staging: se excluyen a propósito. Después ejecuta `scan` y `sync`. Si la carpeta cambió
de nombre, aparecerá en **Carpetas sin identificar**; usa **Crear identificador** o
`link-folder`.

**«Una serie aparece con huecos falsos»**

Los huecos de `missing` solo se calculan sobre temporadas ya empezadas. Si todos los
archivos de una temporada faltan, la temporada se oculta por defecto; usa
`python -m series_manager missing --todos` para verla. La estructura de series sin
proveedor se toma de los archivos, no de una temporada sintética.

**«Las descargas no aparecen en la temporada»**

Añade la carpeta en **Ajustes → Carpetas de descargas en curso** o revisa
`staging_roots`. Después pulsa **Descubrir temporada**. Si el nombre del release no se
parece al título de AniList, identifica primero la carpeta o ajusta el título local.

**«Falta la clave API de Jellyfin»**

Es opcional para escanear y planificar. Para crear la biblioteca o refrescar, créala en
Jellyfin → Panel → API Keys y pégala en Ajustes.

**«Una serie está mal emparejada»**

Abre su ficha, revisa los candidatos o edita el TMDB/TVmaze ID. Después sincroniza.
El plan usará el ID nuevo en el nombre de la carpeta.

**«Jellyfin sigue mostrando InuYasha: The Final Act como serie aparte»**

Ahora forma parte de `InuYasha` como `Season 07`, bajo el TMDB 35610. Si Jellyfin
conserva la ficha antigua, elimina solo esa serie de la biblioteca de Jellyfin (nunca
los archivos) y vuelve a escanear; también puedes refrescar metadatos de InuYasha.

**«El plan muestra conflictos»**

El destino ya existe y es distinto. Revisa la pestaña **Conflictos** del plan; no se
aplicarán automáticamente. No borres nada a mano: decide qué versión conservar y
vuelve a generar el plan.

**«Quiero revertir»**

En el detalle del plan aplicado, escribe `DESHACER`. En CLI:
`python -m series_manager undo <id>`. Los archivos en cuarentena también vuelven.

**«Se cambió el puerto y el panel no responde»**

Reinicia `python -m series_manager web` o `iniciar_panel.bat`.

---

## Publicación y privacidad

El repositorio público es <https://github.com/darknappy/SeriesManager>. Se publica sin
`config.json`, bases de datos, caché, informes, registros, cuarentena ni archivos de
medios. `config.json` puede contener rutas y claves locales; usa
`config.example.json` como plantilla.

Para trabajar desde una copia clonada:

```bat
git clone https://github.com/darknappy/SeriesManager.git
cd SeriesManager
python -m pip install -r requirements.txt
copy config.example.json config.json
python -m series_manager init
```

SeriesAdmin no sustituye a Jellyfin: prepara la biblioteca para que Jellyfin la
entienda.
