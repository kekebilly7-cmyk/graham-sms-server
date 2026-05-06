from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client
import re

app = FastAPI()

SUPABASE_URL = "https://cjwbryhwfofpoopcbmpn.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNqd2JyeWh3Zm9mcG9vcGNibXBuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYzNjYwNjMsImV4cCI6MjA5MTk0MjA2M30.rCjCQdFfHzbKf12XAIrwbOTkVCPcdEqOXD7WiBno4Uk"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ACCOUNT_IDS = {"MTN": 1, "MOOV": 2, "CELTIS": 3, "ORANGE": 4}

class SMS(BaseModel):
    message: str = ""
    sender:  str = ""

def est_financier(msg: str) -> bool:
    a_montant = bool(re.search(r'\d+\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE))
    mots = ["transfert","transfer","depot","dépôt","reçu","recu",
            "envoyé","envoye","retrait","withdraw","paiement","solde",
            "momo","credited","debited"]
    a_mot = any(m in msg.lower() for m in mots)
    return a_montant and a_mot

def parser_sms(message: str, sender: str) -> dict:
    msg = message.strip()
    result = {
        "raw_message": msg, "sender": None, "account_id": None,
        "phone_number": None, "amount": None, "reference_id": None,
        "nom_destinataire": None, "solde": None, "frais": 0,
        "date_transaction": None, "raison": "inconnu",
    }
    if msg in ("{message}", "{{message}}", "$message", "[message]", ""):
        result["raison"] = "test_non_resolu"
        return result

    msg_upper = msg.upper()
    msg_lower = msg.lower()

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

    m = re.search(
        r'(?:transfert|reçu|recu|dépôt|depot|paiement|envoyé|retrait)\s+'
        r'(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE)
    if not m:
        m = re.search(r'(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE)
    if m:
        s = re.sub(r'[\s\.,]', '', m.group(1))
        try: result["amount"] = int(s)
        except: pass

    ph = re.search(r'\(?(229\d{7,11})\)?', msg)
    if not ph: ph = re.search(r'\b(229\d{7,11})\b', msg)
    if ph: result["phone_number"] = ph.group(1)

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
                excl = {"mtn","momo","moov","fcfa","vous","avez","votre","compte","solde"}
                bad  = ["effectué sur votre compte","votre compte"]
                if n.lower() not in excl and len(n) > 2 and not any(b in n.lower() for b in bad):
                    result["nom_destinataire"] = n

    sol = re.search(r'[Ss]olde\s*:?\s*(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE)
    if sol:
        s = re.sub(r'[\s\.,]', '', sol.group(1))
        try: result["solde"] = int(s)
        except: pass

    fr = re.search(r'[Ff]rais\s*:?\s*(\d+)', msg)
    if fr:
        try: result["frais"] = int(fr.group(1))
        except: pass

    id_m = re.search(r'\bID\s*[:\s]*(\d{5,25})', msg, re.IGNORECASE)
    if id_m:
        result["reference_id"] = id_m.group(1)
    else:
        ref = re.search(r'(?:Réf(?:érence)?|Ref)\s*[:\s]+([A-Z0-9]{3,25})', msg, re.IGNORECASE)
        if ref: result["reference_id"] = ref.group(1)

    dt = re.search(r'(\d{4}-\d{2}-\d{2}[\s,]+\d{2}:\d{2}:\d{2})', msg)
    if not dt: dt = re.search(r'(\d{4}-\d{2}-\d{2})', msg)
    if not dt: dt = re.search(r'(\d{2}/\d{2}/\d{4}[\s]+\d{2}:\d{2})', msg)
    if dt: result["date_transaction"] = dt.group(1).strip()

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

def maj_current_cash(account_id: int, amount: int, raison: str):
    """
    DEPOT  → client donne cash à l'agent → cash physique AUGMENTE
    RETRAIT/TRANSFERT → agent donne cash au client → cash physique DIMINUE
    """
    from datetime import datetime, timezone, timedelta
    paris = timezone(timedelta(hours=2))
    aujourd_hui = datetime.now(paris).date().isoformat()

    if raison == "momo_depot":
        delta = +amount   # Cash augmente
    elif raison in ("momo_retrait", "momo_transfert",
                    "momo_paiement", "momo_envoi"):
        delta = -amount   # Cash diminue
    else:
        print(f"⏭️  raison={raison} — pas de maj cash")
        return

    try:
        res = supabase.table("cash_sessions").select("*")\
                      .eq("account_id", account_id)\
                      .gte("created_at", f"{aujourd_hui}T00:00:00")\
                      .order("created_at", desc=True)\
                      .limit(1).execute()

        if res.data:
            sess    = res.data[0]
            current = float(sess.get("current_cash") or
                           sess.get("opening_cash") or 0)
            nouveau = max(0, current + delta)
            supabase.table("cash_sessions")\
                    .update({"current_cash": nouveau})\
                    .eq("id", sess["id"]).execute()
            print(f"💵 cash {current} → {nouveau} F  (delta:{delta:+} | {raison})")
        else:
            print(f"⚠️  Aucune session cash du jour (account_id={account_id})")
            print(f"   Le caissier doit saisir le montant de départ d'abord.")
    except Exception as e:
        print(f"⚠️  Erreur maj_current_cash : {e}")

@app.get("/")
def home():
    return {"message": "✅ Graham POS SMS Server v3 — opérationnel"}

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
               body.get("body") or body.get("sms") or
               body.get("key") or "").strip()
    sender = (body.get("sender") or body.get("from") or
              body.get("number") or "").strip()

    if not message and "key" in body:
        key_val = str(body["key"])
        lignes = key_val.split("\n", 1)
        if len(lignes) == 2:
            m_num = re.search(r'[\+\d]{8,15}', lignes[0])
            if m_num and not sender:
                sender = m_num.group(0)
            message = lignes[1].strip()
        else:
            message = key_val.strip()

    print(f"📱 Sender  : {sender}")
    print(f"💬 Message : {message[:80]}")

    if not est_financier(message):
        print("⏭️  SMS non financier — ignoré")
        return {"status": "ignored", "reason": "non_financier"}

    parsed = parser_sms(message, sender)
    print("✅ Parsed  :", parsed)

    if parsed["raison"] == "test_non_resolu":
        print("⚠️  Template non résolu — ignoré")
        return {"status": "ignored"}

    try:
        payload = {k: v for k, v in parsed.items() if v is not None}
        res = supabase.table("transactions").insert(payload).execute()
        id_ins = res.data[0].get("id", "?") if res.data else "?"
        print(f"✅ transactions ID:{id_ins} | {parsed.get('raison')} | "
              f"{parsed.get('amount')} F | solde:{parsed.get('solde')} F")

        # Mettre à jour current_cash automatiquement
        if parsed.get("amount") and parsed.get("account_id"):
            maj_current_cash(
                parsed["account_id"],
                parsed["amount"],
                parsed["raison"])

        return {"status": "ok", "id": id_ins}

    except Exception as e1:
        print(f"❌ Erreur : {e1}")
        try:
            p_min = {
                "raw_message": parsed.get("raw_message", ""),
                "sender": parsed.get("sender", "MTN"),
                "account_id": parsed.get("account_id"),
                "raison": parsed.get("raison", "inconnu"),
            }
            if parsed.get("amount"): p_min["amount"] = parsed["amount"]
            if parsed.get("phone_number"): p_min["phone_number"] = parsed["phone_number"]
            if parsed.get("reference_id"): p_min["reference_id"] = parsed["reference_id"]
            res2 = supabase.table("transactions").insert(p_min).execute()
            id2 = res2.data[0].get("id", "?") if res2.data else "?"
            print(f"✅ minimal ID:{id2}")
            if parsed.get("amount") and parsed.get("account_id"):
                maj_current_cash(parsed["account_id"], parsed["amount"], parsed["raison"])
            return {"status": "ok_minimal", "id": id2}
        except Exception as e2:
            print(f"❌ Erreur finale : {e2}")
            return {"status": "error", "detail": str(e2)}

@app.post("/sms/test")
def test_sms(sms: SMS):
    msg = sms.message or \
          "Transfert 5000F a KEKE BILLY(22961000000) 2026-05-06 10:07:36 " \
          "Frais:0F Solde:47088F ID:12002009086"
    parsed = parser_sms(msg, sms.sender or "MTN")
    payload = {k: v for k, v in parsed.items() if v is not None}
    res = supabase.table("transactions").insert(payload).execute()
    id_t = res.data[0].get("id", "?") if res.data else "?"
    if parsed.get("amount") and parsed.get("account_id"):
        maj_current_cash(parsed["account_id"], parsed["amount"], parsed["raison"])
    print(f"✅ TEST ID:{id_t}")
    return {"status": "test_ok", "id": id_t, "parsed": parsed}
