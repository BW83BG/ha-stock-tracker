import json, sys, pathlib
base = pathlib.Path(sys.argv[1])

def build(t):
    general = {"name": t["general"], "data": {"scan_interval": t["interval"], "currency": t["currency"]},
               "data_description": {"scan_interval": t["interval_d"], "currency": t["currency_d"]}}
    fees = {"name": t["fees"], "description": t["fees_desc"],
            "data": {"sell_fee_fixed": t["fee_fixed"], "sell_fee_percent": t["fee_pct"], "sell_fee_min": t["fee_min"]}}
    tax = {"name": t["tax"], "description": t["tax_desc"],
           "data": {"church_tax": t["church"], "allowance_remaining": t["allow"], "stock_loss_pot": t["slp"], "general_loss_pot": t["glp"]},
           "data_description": {"allowance_remaining": t["allow_d"], "stock_loss_pot": t["slp_d"], "general_loss_pot": t["glp_d"]}}
    sections = {"general": general, "fees": fees, "tax": tax}
    pos_data = {"asset_type": t["asset_type"], "quantity": t["qty"], "purchase_price": t["pp"], "purchase_fees": t["pf"]}
    pos_desc = {"quantity": t["qty_d"], "purchase_price": t["pp_d"], "purchase_fees": t["pf_d"], "asset_type": t["asset_type_d"]}
    plan_section = {"name": t["plan"], "description": t["plan_desc"],
        "data": {"plan_active": t["p_active"], "plan_amount": t["p_amount"], "plan_interval": t["p_interval"],
                 "plan_day": t["p_day"], "plan_start": t["p_start"], "plan_fee_fixed": t["p_fee_fixed"],
                 "plan_fee_percent": t["p_fee_pct"], "plan_fee_included": t["p_fee_incl"]},
        "data_description": {"plan_amount": t["p_amount_d"], "plan_day": t["p_day_d"], "plan_start": t["p_start_d"],
                 "plan_fee_included": t["p_fee_incl_d"], "plan_active": t["p_active_d"]}}
    return {
        "config": {"step": {"user": {"title": t["title"], "description": t["setup_desc"], "sections": sections}},
                   "abort": {"already_configured": t["already"]}},
        "options": {"step": {"init": {"title": t["options"], "sections": sections}}},
        "config_subentries": {"stock": {
            "entry_type": t["stock"], "initiate_flow": {"user": t["add"]},
            "step": {
                "user": {"title": t["add"], "description": t["add_desc"],
                         "data": {"symbol": t["symbol"], "name": t["name_opt"], **pos_data},
                         "data_description": {"name": t["name_d"], **pos_desc},
                         "sections": {"savings_plan": plan_section}},
                "reconfigure": {"title": t["edit"], "description": t["edit_desc"],
                                "data": {"name": t["name"], **pos_data}, "data_description": pos_desc,
                                "sections": {"savings_plan": plan_section}}},
            "error": {"symbol_not_found": t["nf"], "cannot_connect": t["cc"],
                      "plan_start_required": t["e_start"], "plan_fee_too_high": t["e_fee"]},
            "abort": {"already_configured": t["dup"], "reconfigure_successful": t["upd"]}}},
        "selector": {
            "church_tax": {"options": t["church_opts"]},
            "asset_type": {"options": t["asset_opts"]},
            "plan_interval": {"options": t["interval_opts"]}},
        "entity": {"sensor": {
            "price": {"name": t["s_price"]}, "change": {"name": t["s_change"]}, "change_percent": {"name": t["s_change_pct"]},
            "market_state": {"name": t["s_market"], "state": t["market_states"]},
            "market_value": {"name": t["s_value"]}, "cost_basis": {"name": t["s_cost"]},
            "gain": {"name": t["s_gain"]}, "gain_percent": {"name": t["s_gain_pct"]},
            "tax": {"name": t["s_tax"]}, "gain_after_tax": {"name": t["s_gain_net"]},
            "net_proceeds": {"name": t["s_proceeds"]}, "day_change_value": {"name": t["s_day"]},
            "quantity": {"name": t["s_qty"]}, "average_cost": {"name": t["s_avg"]},
            "plan_next_execution": {"name": t["s_next"]}, "plan_quantity": {"name": t["s_pqty"]},
            "plan_invested": {"name": t["s_pinv"]}}},
    }

en = dict(
 title="Stock prices", setup_desc="Global settings. Afterwards add individual stocks via \"Add stock\".",
 already="Already set up. Add stocks via \"Add stock\".", options="Settings",
 general="General", interval="Update interval", interval_d="How often prices are fetched (seconds).",
 currency="Portfolio currency", currency_d="Purchase prices and fees are entered in this currency; foreign prices are converted.",
 fees="Broker sell fees", fees_desc="Fee per sale: fixed + percentage of the sale value, but at least the minimum fee.",
 fee_fixed="Fixed fee", fee_pct="Percentage fee", fee_min="Minimum fee",
 tax="Tax (Germany)", tax_desc="Capital gains tax 25 % + solidarity surcharge (+ church tax). Update the values below during the year.",
 church="Church tax", allow="Remaining saver's allowance", allow_d="Unused Sparerpauschbetrag for this year (1,000 € single / 2,000 € joint).",
 slp="Stock loss pot", slp_d="Realised stock losses this year (only offset against stock gains).",
 glp="General loss pot", glp_d="Other realised losses this year (offset against all gains).",
 stock="Stock", add="Add stock", add_desc="Yahoo Finance ticker, e.g. AAPL, SAP.DE, EUNL.DE, ^GDAXI, BTC-EUR. Leave quantity at 0 to only track the price. Amounts in {currency}.",
 edit="Edit {symbol}", edit_desc="Amounts in {currency}. {executions} calculated savings plan executions have been added to quantity, purchase price and fees below; compare with your broker and correct if necessary. After additional one-off purchases enter the total quantity, the average purchase price and the sum of all purchase fees.",
 symbol="Ticker symbol", name="Name", name_opt="Name (optional)", name_d="Leave empty to use the name from Yahoo Finance.",
 asset_type="Asset type", asset_type_d="Determines partial exemption and loss offsetting.",
 qty="Quantity", qty_d="Number of shares held (0 = watch only).",
 pp="Purchase price per share", pp_d="Average purchase price in portfolio currency, without fees.",
 pf="Purchase fees (total)", pf_d="Sum of all fees paid when buying.",
 nf="Symbol not found. Check the spelling on finance.yahoo.com.", cc="Could not connect to Yahoo Finance.",
 dup="This stock is already tracked.", upd="Stock updated.",
 church_opts={"none": "None", "8": "8 % (Bavaria, Baden-Württemberg)", "9": "9 % (other states)"},
 asset_opts={"stock": "Stock", "equity_fund": "Equity fund / ETF (30 % exemption)", "mixed_fund": "Mixed fund (15 % exemption)",
             "real_estate_fund": "Real estate fund (60 % exemption)", "other": "Other (bonds, ETCs, ...)", "tax_free": "Tax-free (e.g. crypto held > 1 year)"},
 s_price="Price", s_change="Change", s_change_pct="Change %", s_market="Market state",
 market_states={"pre": "Pre-market", "open": "Open", "post": "After hours", "closed": "Closed"},
 s_value="Market value", s_cost="Cost basis", s_gain="Profit/loss", s_gain_pct="Profit/loss %",
 s_tax="Tax on sale", s_gain_net="Profit/loss after tax", s_proceeds="Net proceeds", s_day="Day change (value)",
 plan="Savings plan", plan_desc="Optional. Executions are calculated automatically from historical closing prices (non-trading day: next trading day) and added to the position. Plan fees are separate from the sell fees.",
 p_active="Active", p_active_d="Switch off to pause the plan.", p_amount="Amount per execution", p_amount_d="0 = no savings plan.",
 p_interval="Interval", p_day="Execution day", p_day_d="Day of month (later days are moved to the month's end). Ignored for weekly plans.",
 p_start="Start date", p_start_d="First possible execution. For weekly plans this is the first execution.",
 p_fee_fixed="Fee per execution (fixed)", p_fee_pct="Fee per execution (%)", p_fee_incl="Fee included in amount",
 p_fee_incl_d="On: the fee is deducted from the amount. Off: the fee is charged on top.",
 e_start="Please enter a start date for the savings plan.", e_fee="The plan fee is as high as the amount.",
 interval_opts={"weekly": "Weekly", "biweekly": "Every 2 weeks", "monthly": "Monthly", "bimonthly": "Every 2 months",
                "quarterly": "Quarterly", "semiannual": "Every 6 months", "annual": "Yearly"},
 s_qty="Quantity", s_avg="Average cost per share", s_next="Next savings plan execution", s_pqty="Savings plan shares", s_pinv="Savings plan invested",
)
de = dict(
 title="Aktienkurse", setup_desc="Globale Einstellungen. Danach einzelne Aktien über \"Aktie hinzufügen\" anlegen.",
 already="Bereits eingerichtet. Aktien über \"Aktie hinzufügen\" anlegen.", options="Einstellungen",
 general="Allgemein", interval="Aktualisierungsintervall", interval_d="Wie oft die Kurse abgerufen werden (Sekunden).",
 currency="Depotwährung", currency_d="Kaufpreise und Gebühren werden in dieser Währung eingegeben; Fremdwährungskurse werden umgerechnet.",
 fees="Verkaufsgebühren des Brokers", fees_desc="Gebühr pro Verkauf: Fixbetrag + Prozentsatz vom Verkaufswert, mindestens aber die Mindestgebühr.",
 fee_fixed="Fixe Gebühr", fee_pct="Prozentuale Gebühr", fee_min="Mindestgebühr",
 tax="Steuer (Deutschland)", tax_desc="Abgeltungsteuer 25 % + Solidaritätszuschlag (+ Kirchensteuer). Die Werte unten im Jahresverlauf aktuell halten.",
 church="Kirchensteuer", allow="Verbleibender Sparerpauschbetrag", allow_d="Noch nicht ausgeschöpfter Freibetrag in diesem Jahr (1.000 € ledig / 2.000 € verheiratet).",
 slp="Verlusttopf Aktien", slp_d="Realisierte Aktienverluste in diesem Jahr (nur mit Aktiengewinnen verrechenbar).",
 glp="Allgemeiner Verlusttopf", glp_d="Sonstige realisierte Verluste in diesem Jahr (mit allen Gewinnen verrechenbar).",
 stock="Aktie", add="Aktie hinzufügen", add_desc="Yahoo-Finance-Symbol, z. B. AAPL, SAP.DE, EUNL.DE, ^GDAXI, BTC-EUR. Stückzahl 0 lassen, um nur den Kurs zu verfolgen. Beträge in {currency}.",
 edit="{symbol} bearbeiten", edit_desc="Beträge in {currency}. {executions} errechnete Sparplan-Ausführungen wurden unten in Stückzahl, Kaufkurs und Gebühren übernommen. Mit dem Broker abgleichen und ggf. korrigieren. Nach Einmalkäufen die Gesamtstückzahl, den durchschnittlichen Kaufkurs und die Summe aller Kaufgebühren eintragen.",
 symbol="Tickersymbol", name="Name", name_opt="Name (optional)", name_d="Leer lassen, um den Namen von Yahoo Finance zu verwenden.",
 asset_type="Anlageart", asset_type_d="Bestimmt Teilfreistellung und Verlustverrechnung.",
 qty="Stückzahl", qty_d="Anzahl gehaltener Anteile (0 = nur beobachten).",
 pp="Kaufkurs pro Stück", pp_d="Durchschnittlicher Kaufkurs in Depotwährung, ohne Gebühren.",
 pf="Kaufgebühren (gesamt)", pf_d="Summe aller beim Kauf gezahlten Gebühren.",
 nf="Symbol nicht gefunden. Prüfe die Schreibweise auf finance.yahoo.com.", cc="Keine Verbindung zu Yahoo Finance möglich.",
 dup="Diese Aktie wird bereits verfolgt.", upd="Aktie aktualisiert.",
 church_opts={"none": "Keine", "8": "8 % (Bayern, Baden-Württemberg)", "9": "9 % (übrige Bundesländer)"},
 asset_opts={"stock": "Aktie", "equity_fund": "Aktienfonds / ETF (30 % Teilfreistellung)", "mixed_fund": "Mischfonds (15 % Teilfreistellung)",
             "real_estate_fund": "Immobilienfonds (60 % Teilfreistellung)", "other": "Sonstige (Anleihen, ETCs, …)", "tax_free": "Steuerfrei (z. B. Krypto > 1 Jahr gehalten)"},
 s_price="Kurs", s_change="Veränderung", s_change_pct="Veränderung %", s_market="Marktstatus",
 market_states={"pre": "Vorbörslich", "open": "Geöffnet", "post": "Nachbörslich", "closed": "Geschlossen"},
 s_value="Positionswert", s_cost="Einstandswert", s_gain="Gewinn/Verlust", s_gain_pct="Gewinn/Verlust %",
 s_tax="Steuer bei Verkauf", s_gain_net="Gewinn/Verlust nach Steuern", s_proceeds="Nettoerlös", s_day="Tagesveränderung (Wert)",
 plan="Sparplan", plan_desc="Optional. Ausführungen werden automatisch anhand historischer Schlusskurse berechnet (an Nicht-Handelstagen: nächster Handelstag) und zur Position addiert. Sparplan-Gebühren sind unabhängig von den Verkaufsgebühren.",
 p_active="Aktiv", p_active_d="Ausschalten, um den Sparplan zu pausieren.", p_amount="Sparrate pro Ausführung", p_amount_d="0 = kein Sparplan.",
 p_interval="Intervall", p_day="Ausführungstag", p_day_d="Tag im Monat (spätere Tage werden aufs Monatsende gelegt). Bei wöchentlichen Plänen ohne Bedeutung.",
 p_start="Startdatum", p_start_d="Frühestmögliche Ausführung. Bei wöchentlichen Plänen die erste Ausführung.",
 p_fee_fixed="Gebühr pro Ausführung (fix)", p_fee_pct="Gebühr pro Ausführung (%)", p_fee_incl="Gebühr in Sparrate enthalten",
 p_fee_incl_d="An: Die Gebühr wird von der Sparrate abgezogen. Aus: Die Gebühr kommt zusätzlich dazu.",
 e_start="Bitte ein Startdatum für den Sparplan angeben.", e_fee="Die Sparplan-Gebühr ist so hoch wie die Sparrate.",
 interval_opts={"weekly": "Wöchentlich", "biweekly": "Alle 2 Wochen", "monthly": "Monatlich", "bimonthly": "Alle 2 Monate",
                "quarterly": "Vierteljährlich", "semiannual": "Halbjährlich", "annual": "Jährlich"},
 s_qty="Stückzahl", s_avg="Ø Einstandskurs", s_next="Nächste Sparplan-Ausführung", s_pqty="Sparplan-Anteile", s_pinv="Sparplan investiert",
)
assert en.keys() == de.keys()
for path, t in ((base / "strings.json", en), (base / "translations/en.json", en), (base / "translations/de.json", de)):
    path.write_text(json.dumps(build(t), indent=2, ensure_ascii=False) + "\n")
print("ok")
