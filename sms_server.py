from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client
import re, os

app = FastAPI()

# ── Connexion Supabase via variables d'environnement ─────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Modèle SMS ────────────────────────────────────────────────────────────────
class SMS(BaseModel):
    message: str = ""
    sender:  str = ""

# ── Parser SMS MTN Bénin ──────────────────────────────────────────────────────
def parser_sms_mtn(message: str, sender: str) -> dict:
    """
    Formats MTN Bénin supportés :
    1. "Transfert 1000F a NOM(229...) DATE Frais:0F Solde:47088F ID:12002009086"
    2. "MTN MOMO: dépôt de 1000 FCFA. Société :NOM. Référence:X. solde:200.000 FCFA"
    3. "MTN MOMO: Vous avez reçu 2000 FCFA de NOM. Référence: X"
    4. "Depot recu 100F de NOM (229...) Solde:47988F ID:12002441081Frais:0F"
    """
    msg = message.strip()

    # ── Mapping réseau → account_id ──────────────────────────────────────────
    ACCOUNT_IDS = {"MTN": 1, "MOOV": 2, "CELTIS": 3, "ORANGE": 4}

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

    # Ignorer templates non résolus
    if msg in ("{message}", "{{message}}", "$message", "[message]", ""):
        result["raison"] = "test_non_resolu"
        return result

    msg_upper = msg.upper()
    msg_lower = msg.lower()
    sender_upper = (sender or "").upper()

    # ── Opérateur / Sender ────────────────────────────────────────────────────
    # Priorité 1 : contenu du message
    if "MTN" in msg_upper or "MOMO" in msg_upper:
        result["sender"] = "MTN"
    elif "MOOV" in msg_upper:
        result["sender"] = "MOOV"
    elif "ORANGE" in msg_upper:
        result["sender"] = "ORANGE"
    # Priorité 2 : mots-clés financiers → MTN au Bénin
    elif any(k in msg_upper for k in ("FCFA","XOF","TRANSFERT","DEPOT",
                                       "SOLDE","MOMO","RECU","REÇU")):
        result["sender"] = "MTN"
    # Priorité 3 : sender explicite (pas un numéro de téléphone)
    elif sender and sender not in ("{sender}","{{sender}}","$sender",
                                    "[from]","[sender]","") \
            and not sender.startswith("+") \
            and not sender.lstrip("+").isdigit():
        result["sender"] = sender
    else:
        result["sender"] = "MTN"  # Par défaut au Bénin = MTN

    # Remplir account_id automatiquement
    result["account_id"] = ACCOUNT_IDS.get(result["sender"])

    # ── Montant ───────────────────────────────────────────────────────────────
    m_mnt = re.search(
        r'(?:transfert|reçu|recu|dépôt|depot|paiement|envoyé)\s+'
        r'(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)',
        msg, re.IGNORECASE)
    if not m_mnt:
        m_mnt = re.search(r'(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)',
                          msg, re.IGNORECASE)
    if m_mnt:
        s = re.sub(r'[\s\.,]', '', m_mnt.group(1))
        try: result["amount"] = int(s)
        except: pass

    # ── Numéro de téléphone ───────────────────────────────────────────────────
    m_ph = re.search(r'\(?(229\d{7,11})\)?', msg)
    if not m_ph:
        m_ph = re.search(r'\b(229\d{7,11})\b', msg)
    if m_ph:
        result["phone_number"] = m_ph.group(1)

    # ── Nom destinataire / société ────────────────────────────────────────────
    # Format 1 : "Société : NOM"
    m_soc = re.search(r'[Ss]oci[eé]t[eé]\s*:\s*([^\.\,\;\n]+)', msg)
    if m_soc:
        result["nom_destinataire"] = m_soc.group(1).strip()
    else:
        # Format 2 : "a NOM PRENOM(229..." ou "a NOM PRENOM DATE"
        m_nom = re.search(
            r'\ba\s+([A-ZÀ-ÿa-zà-ÿ][A-ZÀ-ÿa-zà-ÿ\s\-]{2,60}?)'
            r'\s*(?:\(229|\d{4}-|\d{2}/)',
            msg, re.IGNORECASE)
        if m_nom:
            result["nom_destinataire"] = m_nom.group(1).strip()
        else:
            # Format 3 : "de NOM PRENOM" ou "de NOM BOUTIQUE"
            m_de = re.search(
                r'\bde\s+([A-ZÀ-ÿ][A-ZÀ-ÿa-zà-ÿ\s\-\.]{2,40}?)'
                r'\s*(?:\(|\.|,|Réf|Ref|numéro|$)',
                msg, re.IGNORECASE)
            if m_de:
                nom = m_de.group(1).strip()
                exclus = {"mtn","momo","moov","fcfa","xof","vous","avez",
                          "votre","compte","nouveau","solde","effectué"}
                phrases = ["effectué sur votre compte","sur votre compte",
                           "votre compte","effectué","votre"]
                if (nom.lower() not in exclus and len(nom) > 2 and
                        not any(p in nom.lower() for p in phrases)):
                    result["nom_destinataire"] = nom

    # ── Solde ─────────────────────────────────────────────────────────────────
    # Formats : "Solde:47088F" / "solde : 200.000 FCFA" / "Solde:47088F"
    m_sol = re.search(
        r'[Ss]olde\s*:?\s*(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)',
        msg, re.IGNORECASE)
    if m_sol:
        s = re.sub(r'[\s\.,]', '', m_sol.group(1))
        try: result["solde"] = int(s)
        except: pass

    # ── Frais ─────────────────────────────────────────────────────────────────
    m_fr = re.search(r'[Ff]rais\s*:?\s*(\d+)\s*(?:FCFA|XOF|F\b)?', msg)
    if m_fr:
        try: result["frais"] = int(m_fr.group(1))
        except: pass

    # ── Référence / ID ────────────────────────────────────────────────────────
    # "ID:12002441081Frais" → on prend uniquement les chiffres après ID:
    m_id = re.search(r'\bID\s*[:\s]*(\d{5,25})', msg, re.IGNORECASE)
    if m_id:
        result["reference_id"] = m_id.group(1)
    else:
        m_ref = re.search(
            r'(?:Réf(?:érence)?|Ref)\s*[:\s]+([A-Z0-9]{3,25})',
            msg, re.IGNORECASE)
        if m_ref:
            result["reference_id"] = m_ref.group(1)

    # ── Date transaction ──────────────────────────────────────────────────────
    m_dt = re.search(r'(\d{4}-\d{2}-\d{2}[\s,]+\d{2}:\d{2}:\d{2})', msg)
    if not m_dt:
        m_dt = re.search(r'(\d{4}-\d{2}-\d{2})', msg)
    if not m_dt:
        m_dt = re.search(r'(\d{2}/\d{2}/\d{4}[\s]+\d{2}:\d{2})', msg)
    if m_dt:
        result["date_transaction"] = m_dt.group(1).strip()

    # ── Raison ────────────────────────────────────────────────────────────────
    if any(k in msg_lower for k in ["transfert", "transfer"]):
        result["raison"] = "momo_transfert"
    elif any(k in msg_lower for k in ["vous avez reçu","vous avez recu",
                                       "avez reçu","avez recu",
                                       "dépôt","depot","credited","crédité",
                                       "depot recu"]):
        result["raison"] = "momo_depot"
    elif any(k in msg_lower for k in ["vous avez envoyé","vous avez envoye",
                                       "avez envoyé","avez envoye"]):
        result["raison"] = "momo_envoi"
    elif any(k in msg_lower for k in ["paiement effectué","paiement effectue",
                                       "vous avez payé","paiement de","débité"]):
        result["raison"] = "momo_paiement"
    elif any(k in msg_lower for k in ["retrait","withdraw","cash out"]):
        result["raison"] = "momo_retrait"
    elif any(k in msg_lower for k in ["solde","balance","votre solde"]):
        result["raison"] = "momo_solde"
    elif result["amount"]:
        result["raison"] = "momo_transaction"

    return result


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def home():
    return {"message": "✅ Graham POS — API SMS MTN opérationnelle"}


@app.post("/sms")
async def recevoir_sms(request: Request):
    """Reçoit les SMS depuis SMS Forwarder et les enregistre dans Supabase."""
    try:
        body = await request.json()
    except:
        try:
            form = await request.form()
            body = dict(form)
        except:
            body = {}

    print("📩 Body reçu :", body)

    # Extraire message et sender — supporte tous les formats SMS Forwarder
    message = (body.get("message") or body.get("text") or
               body.get("body")   or body.get("sms")  or
               body.get("key")    or body.get("content") or "").strip()

    sender = (body.get("sender") or body.get("from") or
              body.get("number") or body.get("phone") or
              body.get("sim")    or "").strip()

    # Format natif SMS Forwarder : "key" = "sortant : +NUMERO\nMESSAGE"
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
    print(f"💬 Message : {message}")

    # Parser
    parsed = parser_sms_mtn(message, sender)
    print("✅ Parsed  :", parsed)

    # Ignorer templates non résolus
    if parsed["raison"] == "test_non_resolu":
        print("⚠️  Template non résolu — ignoré")
        return {"status": "ignored"}

    # Insérer dans Supabase
    try:
        payload = {k: v for k, v in parsed.items() if v is not None}
        res     = supabase.table("transactions").insert(payload).execute()
        id_ins  = res.data[0].get("id","?") if res.data else "?"
        print(f"✅ SUPABASE OK — ID: {id_ins} | "
              f"{parsed.get('raison')} | "
              f"{parsed.get('amount')} FCFA | "
              f"{parsed.get('nom_destinataire','—')}")
        return {"status": "ok", "id": id_ins}

    except Exception as e1:
        print(f"❌ Erreur tentative 1 : {e1}")
        # Retry minimal
        try:
            p_min = {
                "raw_message": parsed.get("raw_message",""),
                "sender":      parsed.get("sender","INCONNU"),
                "raison":      parsed.get("raison","inconnu"),
            }
            if parsed.get("amount"):       p_min["amount"]       = parsed["amount"]
            if parsed.get("phone_number"): p_min["phone_number"] = parsed["phone_number"]
            if parsed.get("reference_id"): p_min["reference_id"] = parsed["reference_id"]
            res2   = supabase.table("transactions").insert(p_min).execute()
            id2    = res2.data[0].get("id","?") if res2.data else "?"
            print(f"✅ SUPABASE OK (minimal) — ID: {id2}")
            return {"status": "ok_minimal", "id": id2}
        except Exception as e2:
            print(f"❌ Erreur tentative 2 : {e2}")
            return {"status": "error", "detail": str(e2)}


@app.post("/sms/test")
def test_sms(sms: SMS):
    """Simule un vrai SMS MTN pour tester le parser."""
    msg     = sms.message or \
              "Transfert 5000F a KEKE BILLY(22961000000) 2026-05-02 10:07:36 " \
              "Frais:0F Solde:47088F ID:12002009086"
    parsed  = parser_sms_mtn(msg, sms.sender or "MTN")
    payload = {k: v for k, v in parsed.items() if v is not None}
    res     = supabase.table("transactions").insert(payload).execute()
    id_t    = res.data[0].get("id","?") if res.data else "?"
    print(f"✅ TEST enregistré — ID: {id_t}")
    return {"status": "test_ok", "id": id_t, "parsed": parsed}
