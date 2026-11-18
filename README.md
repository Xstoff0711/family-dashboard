# Familiendashboard

Mealplaner (mit Keto-Markierung), Rezepte, Einkaufsliste, Ideen nach Jahreszeit (Krefeld) und Haushaltsaufgaben.
Läuft als ein einzelner Container (Python + SQLite, keine Abhängigkeiten).

## Auf den Homeserver bringen (Schritt für Schritt)

1. Zip auf den Server kopieren (vom PC aus, `homeserver` = Tailscale-Name oder IP):
   `scp familiendashboard.zip benutzer@homeserver:~/`
2. Auf dem Server entpacken und starten:
   ```bash
   unzip familiendashboard.zip && cd familiendashboard
   docker compose up -d --build
   curl http://127.0.0.1:8081/healthz     # sollte ok melden
   ```
3. Anschauen: vom PC im Tailnet `http://homeserver:8081` öffnen, falls der Port nicht nur lokal gebunden ist.
   Standardmäßig lauscht der Container nur auf `127.0.0.1`. Für einen ersten Blick ohne Caddy in der
   `docker-compose.yml` `"127.0.0.1:8081:8080"` durch `"8081:8080"` ersetzen (im Tailnet erreichbar), später wieder zurücksetzen.
4. Danach Caddy-Eintrag anlegen (siehe unten) und `caddy reload`.
5. Handy: Seite öffnen und "Zum Startbildschirm hinzufügen".

Auch direkt auf dem eigenen PC ausprobieren: Docker Desktop installieren, Zip entpacken, `docker compose up -d --build`, dann `http://localhost:8081`.

Update später: neue Zip drüberentpacken (`data/` bleibt), dann `docker compose up -d --build`.

## Starten

```bash
docker compose up -d --build
# Test lokal auf dem Server:
curl http://127.0.0.1:8081/healthz
```

Daten liegen in `./data/familie.db`. Backup = diese Datei kopieren.

## Namensvergabe mit Tailscale + Caddy

Es gibt zwei saubere Varianten, je nachdem ob du eine eigene Domain hast.

**A) Eigene Domain, Wildcard per DNS-Challenge (schöne Namen)**
Beispiel: `familie.home.deinedomain.de`, der DNS-Eintrag zeigt auf die Tailscale-IP (100.x.y.z) deines Homeservers.
Dafür braucht Caddy ein DNS-Plugin deines Anbieters für die Zertifikate.

```caddyfile
familie.home.deinedomain.de {
    reverse_proxy 127.0.0.1:8081
}
```

**B) Nur Tailscale, ohne eigene Domain**
Caddy kann Zertifikate von Tailscale nur für den Rechnernamen holen, also `homeserver.dein-tailnet.ts.net`.
Dann bekommt jeder Dienst einen eigenen Port oder Pfad:

```caddyfile
homeserver.dein-tailnet.ts.net:8443 {
    reverse_proxy 127.0.0.1:8081
}
```

Die App nutzt nur relative URLs und läuft deshalb auch unter einem Unterpfad (`handle_path /familie/* { reverse_proxy 127.0.0.1:8081 }`).

Namensvorschlag: **familie** (kurz, auf dem Handy leicht zu tippen). Auf dem Handy: Seite öffnen, "Zum Startbildschirm hinzufügen".

## Hinweise

- Anmeldung (seit v15): E-Mail + Passwort, jede Familie sieht nur ihre eigenen Daten. Den Port trotzdem nie ohne Caddy/TLS ins Internet freigeben.
- Erster Start nach dem Update: `docker compose logs familiendashboard` zeigt den Einrichtungscode. Dann die Seite öffnen und das erste Konto anlegen; alle bisherigen Daten gehören danach zu dieser Familie. Der Server legt vorher `data/familie.db.vor-auth-<Datum>` als Backup an.
- Weitere Familien/Mitglieder: im Profil (Klick auf den Kreis oben rechts) Einladungslink erzeugen (7 Tage gültig, einmal nutzbar). „Neue Familie einladen“ gibt es nur für das erste Konto (Betreiber).
- Rückweg auf v14: alte Zip entpacken. Die Altdaten (`kv`-Tabelle, `data/photos/*.jpg`) bleiben unverändert erhalten.
- Test: `python3 tests/test_auth.py` startet einen Testserver und prüft Anmeldung und Trennung der Familien.
- Zwei Personen gleichzeitig: Es gilt "letzte Änderung gewinnt" pro Bereich (Rezepte, Plan, ...). Beim Zurückkehren in die App wird neu geladen.

## Rezeptfotos

Fotos pro Rezept liegen als Dateien in `./data/photos/`. Sie werden im Browser auf 1000 px verkleinert, bevor sie hochgeladen werden.
Backup: den ganzen Ordner `./data` sichern (Datenbank + Fotos).

## Events (Familie) und Ausgehen (Erwachsene)

Die Events kommen direkt von der Veranstaltungsseite der Stadt Krefeld (`krefeld.de/events`).
Der Server ruft sie höchstens einmal täglich ab (beim Öffnen der Seite oder per Knopf "↻ Aktualisieren") und
liest Titel, Datum, Uhrzeit, Kurzbeschreibung und den Link zur Detailseite aus.

- Tab **Heute**: bis zu 3 Vorschläge, zuerst Events der Kategorie "Kinder & Familie", dann feste Ideen der Jahreszeit. Tab **Ideen**: vollständige Liste aller Veranstaltungen des nächsten Monats (Terminserien mit jedem Datum), filter- und durchsuchbar.
- Tab **Ausgehen**: Konzerte, Theater, Märkte, Party, Film/Lesungen und Ausstellungen des nächsten Monats, filterbar, mit "Nur Fr–So".
  Mit 📍 merkst du dir Termine für einen Abend zu zweit.

Hinweise:
- Es gibt keinen offiziellen Feed. Ändert die Stadt ihr Seitenlayout, muss der Auslesecode angepasst werden (`parse_krefeld` in `server.py`).
- Die Stadtseite nennt in der Liste keinen Ort. Details stehen hinter dem Link.
- Daten und Zeiten bitte immer kurz auf der Webseite gegenprüfen.
- `EVENTS_AUTO=0` in `.env` schaltet die tägliche Automatik aus.

Weitere Einstellungen in `.env`: `EVENTS_WINDOW_DAYS` (Standard 31), `EVENTS_REFRESH_HOURS` (24).

## Orte und Zeitraum (v11)

Der Server liest krefeld.de und kindaling.de (Regionen Düsseldorf und Köln) und speichert alle Events
aus rund 26 Städten der Region. In der App wählst du unter „Ideen" und „Ausgehen" per Dropdown
**📍 Orte** aus, woher Events angezeigt werden: Umkreis-Schnellwahl (10/25/40/60 km um Krefeld, Luftlinie grob),
einzelne Städte an-/abhaken, „Alle"/„Keine". Die Auswahl ist für alle Geräte gleich. Darunter schaltest du
zwischen „Diese Woche" und „Ganzer Monat" um. Es werden immer ALLE Events des Zeitraums gezeigt, nach Tag gruppiert, mit Uhrzeit.
Die „3 Vorschläge für heute" nutzen dieselbe Ortsauswahl.

Optional per Umgebungsvariable: `KINDALING_REGIONS` (Standard `duesseldorf,koeln`), `EVENTS_CITIES`.

Vergangenes wird ausgeblendet (v13): Events, die vor heute geendet haben, und eintägige Events von heute,
deren Startzeit schon vorbei ist. Mehrtägige, noch laufende Events stehen unter „Läuft gerade".

## Haushalt (v14)

Feste Woche Mo–So mit ‹ › zum Blättern, Fortschrittsbalken, ＋ an jedem Tag. 🔁 kennzeichnet wiederkehrende
Aufgaben, ein Farbpunkt zeigt die Zuständigkeit (blau = Ich, pink = Partnerin, beide = Beide).
Über ⋯ an jeder Aufgabe: auf morgen/anderes Datum verschieben, diesmal überspringen (unten „Übersprungen“ → wieder einblenden),
bearbeiten, Serie löschen. Rhythmen: täglich, wöchentlich, alle 2 Wochen, monatlich, einmalig.
Offenes aus den letzten 14 Tagen steht als „Überfällig“ oben (auch auf „Heute“ als Hinweis).
Sonntags gibt es den Wochenabschluss. 📋 Vorlagen: fertige Aufgaben zum Antippen (auch „Alle Aufgaben entfernen“).
Aufgaben mit 🛒 öffnen direkt die Einkaufsliste.
