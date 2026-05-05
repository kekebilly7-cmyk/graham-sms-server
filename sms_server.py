from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client
import re

app = FastAPI()

# ── Connexion Supabase ────────────────────────────────────────────────────────
SUPABASE_URL = "https://cjwbryhwfofpoopcbmpn.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNqd2JyeWh3Zm9mcG9vcGNibXBuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYzNjYwNjMsImV4cCI6MjA5MTk0MjA2M30.rCjCQdFfHzbKf12XAIrwbOTkVCPcdEqOXD7WiBno4Uk"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Mapping réseau → account_id ───────────────────────────────────────────────
ACCOUNT_IDS = {"MTN": 1, "MOOV": 2, "CELTIS": 3, "ORANGE": 4}

class SMS(BaseModel):
    message: str = ""
    sender:  str = ""

# ── Vérifier si SMS est financier ─────────────────────────────────────────────
def est_financier(msg: str) -> bool:
    """Retourne True uniquement si le SMS contient une transaction financière."""
    msg_up = msg.upper()
    # Doit contenir un montant
    a_montant = bool(re.search(
        r'\d+\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE))
    # ET un mot-clé financier
    mots_financiers = [
        "transfert","transfer","depot","dépôt","reçu","recu",
        "envoyé","envoye","retrait","withdraw","paiement","solde",
        "momo","mtn money","credited","debited"
    ]
    a_mot = any(m in msg.lower() for m in mots_financiers)
    return a_montant and a_mot

# ── Parser SMS ────────────────────────────────────────────────────────────────
def parser_sms(message: str, sender: str) -> dict:
    msg = message.strip()

    result = {
        "raw_message":      msg,
        "sender":           None,
        "account_id":       None,
        "phone_number":     None,
        "amount":           None,
        "reference_id":     None,
        "nom_destinataire": None,
        "solde":            None,
        "frais":            0,
        "date_transaction": None,
        "raison":           "inconnu",
    }

    if msg in ("{message}", "{{message}}", "$message", "[message]", ""):
        result["raison"] = "test_non_resolu"
        return result

    msg_upper = msg.upper()
    msg_lower = msg.lower()

    # ── Sender ────────────────────────────────────────────────────────────────
    if "MTN" in msg_upper or "MOMO" in msg_upper:
        result["sender"] = "MTN"
    elif "MOOV" in msg_upper:
        result["sender"] = "MOOV"
    elif "CELTIS" in msg_upper:
        result["sender"] = "CELTIS"
    elif "ORANGE" in msg_upper:
        result["sender"] = "ORANGE"
    elif any(k in msg_upper for k in ("FCFA","XOF","TRANSFERT","DEPOT","SOLDE")):
        result["sender"] = "MTN"
    elif (sender and sender not in ("{sender}","[from]","[sender]","")
          and not sender.startswith("+")
          and not sender.lstrip("+").isdigit()):
        result["sender"] = sender
    else:
        result["sender"] = "MTN"

    result["account_id"] = ACCOUNT_IDS.get(result["sender"])

    # ── Montant ───────────────────────────────────────────────────────────────
    m = re.search(
        r'(?:transfert|reçu|recu|dépôt|depot|paiement|envoyé|retrait)\s+'
        r'(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)',
        msg, re.IGNORECASE)
    if not m:
        m = re.search(r'(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE)
    if m:
        s = re.sub(r'[\s\.,]', '', m.group(1))
        try: result["amount"] = int(s)
        except: pass

    # ── Téléphone ─────────────────────────────────────────────────────────────
    ph = re.search(r'\(?(229\d{7,11})\)?', msg)
    if not ph:
        ph = re.search(r'\b(229\d{7,11})\b', msg)
    if ph:
        result["phone_number"] = ph.group(1)

    # ── Nom ───────────────────────────────────────────────────────────────────
    soc = re.search(r'[Ss]oci[eé]t[eé]\s*:\s*([^\.\,\;\n]+)', msg)
    if soc:
        result["nom_destinataire"] = soc.group(1).strip()
    else:
        nom = re.search(
            r'\ba\s+([A-ZÀ-ÿa-zà-ÿ][A-ZÀ-ÿa-zà-ÿ\s\-]{2,60}?)'
            r'\s*(?:\(229|\d{4}-|\d{2}/)', msg, re.IGNORECASE)
        if nom:
            result["nom_destinataire"] = nom.group(1).strip()
        else:
            de = re.search(
                r'\bde\s+([A-ZÀ-ÿ][A-ZÀ-ÿa-zà-ÿ\s\-\.]{2,40}?)'
                r'\s*(?:\(|\.|,|Réf|Ref|numéro|$)', msg, re.IGNORECASE)
            if de:
                n = de.group(1).strip()
                excl = {"mtn","momo","moov","fcfa","vous","avez",
                        "votre","compte","solde","effectué"}
                bad  = ["effectué sur votre compte","votre compte"]
                if (n.lower() not in excl and len(n) > 2
                        and not any(b in n.lower() for b in bad)):
                    result["nom_destinataire"] = n

    # ── Solde ─────────────────────────────────────────────────────────────────
    sol = re.search(
        r'[Ss]olde\s*:?\s*(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE)
    if sol:
        s = re.sub(r'[\s\.,]', '', sol.group(1))
        try: result["solde"] = int(s)
        except: pass

    # ── Frais ─────────────────────────────────────────────────────────────────
    fr = re.search(r'[Ff]rais\s*:?\s*(\d+)', msg)
    if fr:
        try: result["frais"] = int(fr.group(1))
        except: pass

    # ── Référence ─────────────────────────────────────────────────────────────
    id_m = re.search(r'\bID\s*[:\s]*(\d{5,25})', msg, re.IGNORECASE)
    if id_m:
        result["reference_id"] = id_m.group(1)
    else:
        ref = re.search(
            r'(?:Réf(?:érence)?|Ref)\s*[:\s]+([A-Z0-9]{3,25})', msg, re.IGNORECASE)
        if ref:
            result["reference_id"] = ref.group(1)

    # ── Date ──────────────────────────────────────────────────────────────────
    dt = re.search(r'(\d{4}-\d{2}-\d{2}[\s,]+\d{2}:\d{2}:\d{2})', msg)
    if not dt: dt = re.search(r'(\d{4}-\d{2}-\d{2})', msg)
    if not dt: dt = re.search(r'(\d{2}/\d{2}/\d{4}[\s]+\d{2}:\d{2})', msg)
    if dt: result["date_transaction"] = dt.group(1).strip()

    # ── Raison ────────────────────────────────────────────────────────────────
    if any(k in msg_lower for k in ["transfert","transfer"]):
        result["raison"] = "momo_transfert"
    elif any(k in msg_lower for k in ["vous avez reçu","avez reçu","avez recu",
                                       "dépôt","depot","crédité","depot recu"]):
        result["raison"] = "momo_depot"
    elif any(k in msg_lower for k in ["vous avez envoyé","avez envoyé"]):
        result["raison"] = "momo_envoi"
    elif any(k in msg_lower for k in ["paiement effectué","paiement de","débité"]):
        result["raison"] = "momo_paiement"
    elif any(k in msg_lower for k in ["retrait","withdraw","cash out"]):
        result["raison"] = "momo_retrait"
    elif result["amount"]:
        result["raison"] = "momo_transaction"

    return result

# ── Insérer mouvement cash automatique ───────────────────────────────────────
def inserer_cash_movement(account_id: int, amount: int, raison: str):
    """
    momo_depot   → depot_recu   (client te donne cash → ton liquide augmente)
    momo_retrait → retrait_paye (tu donnes cash au client → ton liquide diminue)
    """
    type_cash = None
    if raison == "momo_depot":
        type_cash = "depot_recu"
    elif raison in ("momo_retrait", "momo_paiement"):
        type_cash = "retrait_paye"

    if type_cash and account_id and amount:
        try:
            supabase.table("cash_movements").insert({
                "account_id": account_id,
                "amount":     float(amount),
                "type":       type_cash,
            }).execute()
            print(f"💵 cash_movements → {type_cash} : {amount} F")
        except Exception as e:
            print(f"⚠️  cash_movements erreur : {e}")

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def home():
    return {"message": "✅ Graham POS SMS Server v2 — opérationnel"}

@app.post("/sms")
async def recevoir_sms(request: Request):
    try:
        body = await request.json()
    except:
        try:
            form = await request.form()
            body = dict(form)
        except:
            body = {}

    print("📩 Body :", body)

    message = (body.get("message") or body.get("text") or
               body.get("body")   or body.get("sms")  or
               body.get("key")    or "").strip()
    sender  = (body.get("sender") or body.get("from") or
               body.get("number") or "").strip()

    # Format natif SMS Forwarder
    if not message and "key" in body:
        key_val = str(body["key"])
        lignes  = key_val.split("\n", 1)
        if len(lignes) == 2:
            m_num = re.search(r'[\+\d]{8,15}', lignes[0])
            if m_num and not sender:
                sender = m_num.group(0)
            message = lignes[1].strip()
        else:
            message = key_val.strip()

    print(f"📱 Sender  : {sender}")
    print(f"💬 Message : {message[:80]}")

    # ── Filtrer les SMS non financiers ────────────────────────────────────────
    if not est_financier(message):
        print("⏭️  SMS non financier — ignoré")
        return {"status": "ignored", "reason": "non_financier"}

    parsed = parser_sms(message, sender)
    print("✅ Parsed  :", parsed)

    if parsed["raison"] == "test_non_resolu":
        print("⚠️  Template non résolu — ignoré")
        return {"status": "ignored"}

    # ── Insérer dans transactions ─────────────────────────────────────────────
    try:
        payload = {k: v for k, v in parsed.items() if v is not None}
        res     = supabase.table("transactions").insert(payload).execute()
        id_ins  = res.data[0].get("id","?") if res.data else "?"
        print(f"✅ transactions ID:{id_ins} | {parsed.get('raison')} | "
              f"{parsed.get('amount')} F | solde:{parsed.get('solde')} F")

        # ── Insérer dans cash_movements si depot ou retrait ───────────────────
        if parsed.get("amount") and parsed.get("account_id"):
            inserer_cash_movement(
                parsed["account_id"],
                parsed["amount"],
                parsed["raison"])

        return {"status": "ok", "id": id_ins}

    except Exception as e1:
        print(f"❌ Erreur : {e1}")
        try:
            p_min = {
                "raw_message": parsed.get("raw_message",""),
                "sender":      parsed.get("sender","MTN"),
                "account_id":  parsed.get("account_id"),
                "raison":      parsed.get("raison","inconnu"),
            }
            if parsed.get("amount"):       p_min["amount"]       = parsed["amount"]
            if parsed.get("phone_number"): p_min["phone_number"] = parsed["phone_number"]
            if parsed.get("reference_id"): p_min["reference_id"] = parsed["reference_id"]
            res2 = supabase.table("transactions").insert(p_min).execute()
            id2  = res2.data[0].get("id","?") if res2.data else "?"
            print(f"✅ minimal ID:{id2}")
            return {"status": "ok_minimal", "id": id2}
        except Exception as e2:
            print(f"❌ Erreur finale : {e2}")
            return {"status": "error", "detail": str(e2)}

@app.post("/sms/test")
def test_sms(sms: SMS):
    msg    = sms.message or \
             "Transfert 5000F a KEKE BILLY(22961000000) 2026-05-05 10:07:36 " \
             "Frais:0F Solde:47088F ID:12002009086"
    parsed = parser_sms(msg, sms.sender or "MTN")
    payload= {k: v for k, v in parsed.items() if v is not None}
    res    = supabase.table("transactions").insert(payload).execute()
    id_t   = res.data[0].get("id","?") if res.data else "?"
    if parsed.get("amount") and parsed.get("account_id"):
        inserer_cash_movement(parsed["account_id"], parsed["amount"], parsed["raison"])
    print(f"✅ TEST ID:{id_t}")
    return {"status": "test_ok", "id": id_t, "parsed": parsed}
