# Aktienkurse – Home Assistant Integration

Verfolgt Aktien-, ETF-, Index- und Kryptokurse über Yahoo Finance (kein API-Key nötig)
und berechnet für dein Depot, **was beim Verkauf zum aktuellen Kurs übrig bliebe**,
inklusive Broker-Gebühren und deutscher Abgeltungsteuer.

## Installation

**Manuell:** Den Ordner `custom_components/stock_tracker` nach `<config>/custom_components/` kopieren und Home Assistant neu starten.

**HACS:** Repository als *Benutzerdefiniertes Repository* (Typ: Integration) hinzufügen.

Benötigt Home Assistant **2025.3** oder neuer.

## Einrichtung

1. *Einstellungen → Geräte & Dienste → Integration hinzufügen → „Aktienkurse“*
2. Globale Einstellungen festlegen (später änderbar über **Konfigurieren**):
   - **Allgemein:** Aktualisierungsintervall, Depotwährung (Standard: EUR)
   - **Verkaufsgebühren:** fixe Gebühr + prozentuale Gebühr, mindestens Mindestgebühr
     (z. B. 4,90 € + 0,25 %, min. 9,90 €; bei Neobrokern z. B. 1 € fix)
   - **Steuer:** Kirchensteuer, verbleibender Sparerpauschbetrag, Verlusttöpfe
3. Über **„Aktie hinzufügen“** Aktien anlegen:
   - Tickersymbol, optional Name
   - Anlageart (Aktie, Aktien-ETF, Mischfonds, …)
   - Stückzahl, Kaufkurs pro Stück, Kaufgebühren gesamt (in Depotwährung)
   - Stückzahl **0** = nur Kurs beobachten

Positionen änderst du über das Drei-Punkte-Menü am Eintrag → *Neu konfigurieren*.
Nach Nachkäufen trägst du die Gesamtstückzahl, den Durchschnittskurs und die Summe der Kaufgebühren ein.

| Beispiel | Symbol |
|---|---|
| SAP (XETRA) | `SAP.DE` |
| Apple (NASDAQ) | `AAPL` |
| iShares Core MSCI World | `EUNL.DE` |
| DAX | `^GDAXI` |
| Bitcoin in EUR | `BTC-EUR` |

## Entitäten

**Pro Aktie** (Gerät mit Namen der Aktie):

| Sensor | Beschreibung |
|---|---|
| Kurs | Aktueller Kurs in Handelswährung (Attribute: Börse, Tageshoch/-tief, Volumen, 52W) |
| Veränderung / Veränderung % | Zum Vortagesschluss |
| Marktstatus | Vorbörslich / Geöffnet / Nachbörslich / Geschlossen |

**Zusätzlich pro Position** (wenn Stückzahl > 0), in Depotwährung:

| Sensor | Berechnung |
|---|---|
| Positionswert | Stückzahl × Kurs × Wechselkurs |
| Einstandswert | Stückzahl × Kaufkurs + Kaufgebühren |
| Gewinn/Verlust (+ %) | Positionswert − Verkaufsgebühr − Einstandswert |
| Steuer bei Verkauf | Abgeltungsteuer + Soli (+ KiSt), wenn **nur diese Position** verkauft wird |
| Gewinn/Verlust nach Steuern | Gewinn/Verlust − Steuer |
| Nettoerlös | Was auf dem Verrechnungskonto ankommt |
| Tagesveränderung (Wert) | Stückzahl × Tagesveränderung |

**Gerät „Depot“:** dieselben Werte als Summe, wenn **das gesamte Depot** verkauft wird.
Gewinne und Verluste werden dabei gegeneinander verrechnet, der Freibetrag zählt nur einmal.

## Steuerberechnung

- Abgeltungsteuer 25 % + Solidaritätszuschlag 5,5 % = **26,375 %**.
  Mit Kirchensteuer 8 % sind es 27,82 %, mit 9 % sind es 27,99 % (die Kirchensteuer mindert die Abgeltungsteuer).
- Kauf- und Verkaufsgebühren mindern den steuerpflichtigen Gewinn.
- **Teilfreistellung:** Aktienfonds 30 %, Mischfonds 15 %, Immobilienfonds 60 %. Sie gilt für Gewinne und Verluste.
- **Verlustverrechnung:** Aktienverluste nur mit Aktiengewinnen, sonstige Verluste mit allem.
  Bestehende Verlusttöpfe und der Sparerpauschbetrag werden danach abgezogen.
- Die Steuer-Sensoren haben Attribute mit der Aufschlüsselung: steuerpflichtiger Betrag, Abgeltungsteuer, Soli, KiSt und genutzter Freibetrag.

**Nicht berücksichtigt:** ausländische Quellensteuer, Vorabpauschale, Haltefristen bei Krypto (dafür Anlageart „Steuerfrei“ wählen) und
Wechselkurseffekte beim Kauf. Der Kaufkurs wird bereits in Depotwährung eingegeben, das entspricht der deutschen Steuerlogik.
Die Werte sind eine Schätzung und **keine Steuerberatung**.

## Entwicklung / Tests

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

## Hinweis

Die Yahoo-Finance-API ist inoffiziell und kann sich ohne Ankündigung ändern. Kurse an Börsen sind teils verzögert (typisch 15 Minuten).
Fällt ein einzelner Kurs kurzzeitig aus, wird der zuletzt bekannte Wert weiterverwendet.
