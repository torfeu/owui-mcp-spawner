# Bug: specs von OWUI-Uploads werden verworfen, obwohl die korrekten bereits vorliegen

Gefunden: 2026-07-27 · Betrifft: alle Versionen bis 0.1.2 · Schweregrad: mittel (falsche Metadaten, kein Datenverlust)

**Status: behoben in 0.1.3** — Umsetzung unten unter *Umgesetzt*.

## Kurzfassung

Wird ein fertiges OpenWebUI-Tool-JSON hochgeladen, speichert der Spawner dessen `specs` **wortwörtlich**. Diese specs enthalten je nach erzeugendem OWUI oft nur die **erste Zeile** des Docstrings — bei hart umbrochenen Docstrings endet die Beschreibung dann mitten im Satz.

Der Witz: der Upload-Pfad hat die **richtigen** specs zu diesem Zeitpunkt bereits in der Hand. Er validiert den Code im Venv der Instanz und bekommt dabei von `validate_tool_code` das vollständige, aus dem Code erzeugte Schema zurück — und wirft es weg.

## Beobachtet

Auf dem t630, `openhab_smart_home_en` 0.5.0 (als OWUI-JSON hochgeladen):

```
describe_tools("openhab_smart_home_en.get_device_status")
→ "Reads the CURRENT live state of any OpenHAB item — controllable or"
```

Der Satz bricht nach „controllable or" ab. Der tatsächliche Docstring derselben Funktion hat **774 Zeichen über 21 Zeilen**, inklusive Anwendungsbeispielen.

Zum Vergleich `gesetze_im_internet` (über das Framework angelegt): dort kommt der vollständige, mehrabsätzige Docstring durch, inklusive der `NOTE:`-Passage.

## Erwartet

Die gespeicherten `specs` beschreiben die Funktionen so vollständig, wie der Code es hergibt — unabhängig davon, ob das Tool aus Code erzeugt oder als JSON hochgeladen wurde.

## Reproduktion

1. Ein Tool mit mehrzeiligem, hart umbrochenem Docstring in OpenWebUI anlegen und dort als JSON exportieren
2. Dieses JSON im Spawner hochladen (`POST /api/instances/upload`)
3. `GET /api/instances/{id}/specs` oder den Info-Dialog öffnen
4. Beschreibung ist auf die erste Docstring-Zeile gekürzt

## Ursache

`_provision_new_tool` in `app/api_helpers.py`:

```python
# Schritt 2 — validiert im Venv der Instanz, liefert vollständige specs
validation = await asyncio.to_thread(validate_tool_code, code, str(python_path(venv)))

# Schritt 3 — schreibt beim Upload das hochgeladene JSON wortwörtlich
if persist_json is not None:
    payload = persist_json if isinstance(persist_json, list) else [persist_json]
    tool_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
else:
    generated = generate_openwebui_json(code, tool_id, name, description, validation=validation)
```

`validation["tools"]` ist genau das, was `generate_openwebui_json` im else-Zweig als `"specs"` einsetzt (`app/tool_editor.py`). Im if-Zweig bleibt es ungenutzt.

Der verbatim-Pfad hat einen berechtigten Kern — `meta`, `manifest`, `id` und sonstige OWUI-Felder des Uploads sollen erhalten bleiben. Er schüttet nur das Kind mit aus: `specs` ist das eine Feld, das **nicht** erhalten bleiben sollte, weil es aus dem `content` desselben Uploads exakt ableitbar ist.

## Auswirkung

- **Info-Dialog** (0.1.3) zeigt abgeschnittene Funktionsbeschreibungen
- **`GET /api/instances/{id}/specs`** und `?include=specs` liefern sie weiter
- **MCP Tool Router**: das Modell bekommt für betroffene Instanzen schlechtere Anleitung, als der Code hergibt — bei einem Satz, der mitten im Wort endet, sogar irreführende
- **Nicht betroffen ist die Laufzeit**: `mcp_runner` baut seine Schemas über `schema_gen` selbst aus dem Code (`tool_loader.get_mcp_tool_defs`). Ein Aufruf funktioniert also, nur die *Beschreibung* ist schlechter. Genau deshalb ist der Fehler bisher unbemerkt geblieben — er wird erst sichtbar, seit die specs überhaupt angezeigt werden.

## Lösung: automatisch, kein Code-Editor

### A. Neue Uploads — specs beim Persistieren ersetzen

Im verbatim-Zweig die specs durch die soeben validierten austauschen, alles andere aus dem Upload behalten:

```python
if persist_json is not None:
    payload = persist_json if isinstance(persist_json, list) else [persist_json]
    if payload and validation.get("tools"):
        # Aus demselben `content` abgeleitet und damit immer aktueller als das,
        # was ein fremdes OWUI mitgeschickt hat. Alle übrigen Felder (meta,
        # manifest, id, …) bleiben unangetastet.
        payload[0]["specs"] = validation["tools"]
    tool_file.write_text(...)
```

Drei Zeilen, an einer Stelle, ohne neuen Subprozess: die Validierung läuft in diesem Pfad ohnehin, ihr Ergebnis liegt in derselben Funktion bereits vor. Kein `exec()` im Manager-Prozess — der Worker läuft wie gehabt isoliert im Venv der Instanz.

### B (gewählt). Bestehende Installationen — einmalige Migration beim Start

Ein Hintergrund-Task im Lifespan, nach dem Muster von `_migrate_existing_venv_deps`: für jede Instanz den Code aus dem Tool-JSON im zugehörigen Venv validieren, die specs vergleichen und **nur bei Unterschied** zurückschreiben. Marker `runtime/.specs_migrated`, damit es sich nicht wiederholt; bei einer Instanz, die sich gerade nicht validieren lässt, wird der Marker nicht gesetzt und beim nächsten Start erneut versucht.

Entscheidend gegenüber der Reinstall-Variante unten: **es braucht keinen Klick und kein Entsperren.** Genau der Fall, der den Fehler ausgelöst hat — `openhab_smart_home_en` steht auf `locked: true`, das Dashboard zeigt für gesperrte Instanzen gar keinen Edit-Code-Knopf, und `PUT /tool-code` würde mit `403` antworten — heilt damit von selbst.

Die Sperre wird dabei bewusst ignoriert: `content` bleibt unangetastet, nur ein abgeleitetes Feld wird neu berechnet. „Ändere dieses Tool nicht" darf nicht heißen „beschreibe es für immer falsch".

### B-Alternative (verworfen). Über „Reinstall" mitheilen

`POST /api/instances/{id}/reinstall` installiert heute nur die Abhängigkeiten neu (`app/routes/tools.py:156`) und wäre die naheliegende Reparaturaktion. Verworfen, weil `require_not_locked` auch dort sitzt: ausgerechnet die gesperrten Instanzen — die man am meisten schützt — bekämen ihre Reparatur nur nach Entsperren. Dazu bliebe es ein manueller Klick pro Instanz.

### Erwogen und geprüft

- **Kann die Ersetzung etwas kaputtmachen?** Die erzeugten specs sind dieselben, die OWUI bei jedem über das Framework angelegten Tool schon heute bekommt — das Format ist erprobt. Sie können mehr (`Literal` → enum, `Annotated` → description, fehlende Parameter), nie weniger. Im Test tauchte bei einem Beispiel-Upload ein Parameter auf, den die hochgeladenen specs überhaupt nicht kannten.
- **Nur bei Unterschied schreiben**, sonst ändert sich bei jedem Start die mtime und der specs-Cache (`_specs_cache`) wird ohne Grund verworfen.
- **Nicht im Vordergrund.** Eine Validierung pro Instanz ist ein Subprozess; bei zehn Instanzen verzögerte das den Start und damit den Autostart spürbar. Deshalb `asyncio.create_task`.

## Umgesetzt

- `app/api_helpers.py` — im verbatim-Zweig von `_provision_new_tool` werden die specs durch `validation["tools"]` ersetzt, alle übrigen Felder des Uploads bleiben unangetastet
- `app/admin_server.py` — `_migrate_tool_specs()` als Hintergrund-Task im Lifespan, Marker `runtime/.specs_migrated`
- `tests/test_specs_repair.py` — Upload behält `meta`/`manifest`/`content` und bekommt die erzeugten specs; Migration repariert, lässt Übereinstimmendes in Ruhe (mtime unverändert), überspringt Tools ohne `content`, setzt bei fehlgeschlagener Validierung keinen Marker

Praxistest mit einem OWUI-artigen Upload (abgeschnittene Beschreibung, ein fehlender Parameter): nach dem Upload 7 Zeilen Beschreibung statt einer und beide Parameter; nach künstlichem Zurücksetzen auf den alten Zustand hat der Neustart die Datei selbst repariert (`Rebuilt tool specs from code for 1 instance(s)`).

## Verworfene Alternativen

**Beim Lesen nachgenerieren** (im specs-Endpunkt oder im Info-Dialog): bräuchte pro Aufruf einen Validierungs-Subprozess und macht aus einem Lesezugriff einen Schreibzugriff. Der Endpunkt ist bewusst ein billiges `json.load`.

**Der Runner schreibt seine Schemas beim Start zurück**: verlockend, weil `get_mcp_tool_defs()` dort ohnehin die Wahrheit berechnet. Aber: gestoppte Instanzen blieben veraltet (und genau für die ist der Info-Dialog gedacht), und ein Subprozess, der in `tools/` schreibt, während der Editor dieselbe Datei speichern kann, ist eine Rennbedingung, die man sich nicht ohne Not einhandelt.

**Nur warnen** („specs möglicherweise veraltet" im Info-Dialog): verlagert die Arbeit zum Nutzer, obwohl die richtige Antwort maschinell vorliegt.
