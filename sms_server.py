from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client
import re, json

app = FastAPI()

# 🔑 Connexion Supabase
SUPABASE_URL = "https://cjwbryhwfofpoopcbmpn.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNqd2JyeWh3Zm9mcG9vcGNibXBuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYzNjYwNjMsImV4cCI6MjA5MTk0MjA2M30.rCjCQdFfHzbKf12XAIrwbOTkVCPcdEqOXD7WiBno4Uk"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Modèle SMS ────────────────────────────────────────────────────────────────
class SMS(BaseModel):
    message: str = ""
    sender: str = ""

# ── Parsing SMS MTN Bénin ─────────────────────────────────────────────────────
def parser_sms_mtn(message: str, sender: str) -> dict:
    """
    Parser SMS MTN Bénin — formats réels observés :
    1. "Transfert 1000F a NOM PRENOM(229...) DATE Frais: 0F Solde:47088F Ref: X ID: Y"
    2. "MTN MOMO: dépôt de 1000 FCFA effectué. Société :NOM. Référence : X. solde : 200.000 FCFA"
    3. "MTN MOMO: Vous avez reçu 2000 FCFA de NOM. Référence: X"
    """
    msg = message.strip()
    result = {
        "raw_message":      msg,
        "sender":         None,
        "phone_number":     None,
        "amount":           None,
        "reference_id":     None,
        "nom_destinataire": None,
        "solde":    None,
        "frais":            0,
        "date_transaction": None,
        "raison":           "inconnu",
    }

    # ── Ignorer templates non résolus ─────────────────────────────────────────
    if msg in ("{message}","{{message}}","$message","[message]",""):
        result["raison"] = "test_non_resolu"
        return result

    msg_upper = msg.upper()
    msg_lower = msg.lower()

    # ── Opérateur ─────────────────────────────────────────────────────────────
    sender_upper = sender.upper() if sender else ""
    if "MTN" in msg_upper or "MOMO" in msg_upper or "MTN" in sender_upper:
        result["sender"] = "MTN"
    elif "MOOV" in msg_upper or "MOOV" in sender_upper:
        result["sender"] = "MOOV"
    elif "ORANGE" in msg_upper or "ORANGE" in sender_upper:
        result["sender"] = "ORANGE"
    elif sender and sender not in ("{sender}","{{sender}}","$sender","[from]","[sender]",""):
        # Utiliser le sender directement comme opérateur
        result["sender"] = sender
    elif "FCFA" in msg_upper or "DEPOT" in msg_upper or "SOLDE" in msg_upper:
        result["sender"] = "MTN"
    else:
        result["sender"] = "INCONNU"

    # ── Montant ───────────────────────────────────────────────────────────────
    montant_match = re.search(
        r'(?:transfert|reçu|recu|dépôt|depot|paiement|envoyé)\s+(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)',
        msg, re.IGNORECASE)
    if not montant_match:
        montant_match = re.search(
            r'(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)',
            msg, re.IGNORECASE)
    if montant_match:
        s = re.sub(r'[\s\.,]', '', montant_match.group(1))
        try: result["amount"] = int(s)
        except: pass

    # ── Numéro de téléphone ───────────────────────────────────────────────────
    # Corrigé : 229 + 7 à 11 chiffres pour couvrir tous les formats Bénin
    phone_match = re.search(r'\(?(229\d{7,11})\)?', msg)
    if not phone_match:
        phone_match = re.search(r'\b(229\d{7,11})\b', msg)
    if phone_match:
        result["phone_number"] = phone_match.group(1)

    # ── Nom destinataire / société ────────────────────────────────────────────
    nom_match = re.search(
        r'\ba\s+([A-ZÀÂÇÉÈÊËÎÏÔÙÛÜŸA-zàâçéèêëîïôùûüÿ][A-ZÀÂÇÉÈÊËÎÏÔÙÛÜŸA-zàâçéèêëîïôùûüÿ\s\-]{2,60}?)\s*(?:\(229|\d{4}-|\d{2}/)',
        msg, re.IGNORECASE)
    if nom_match:
        result["nom_destinataire"] = nom_match.group(1).strip()
    else:
        soc_match = re.search(r'[Ss]oci[eé]t[eé]\s*:\s*([^\.\,\;\n]+)', msg)
        if soc_match:
            result["nom_destinataire"] = soc_match.group(1).strip()
        else:
            de_match = re.search(
                r'\bde\s+([A-ZÀÂÇÉÈÊËÎÏÔÙÛÜŸ][A-ZÀÂÇÉÈÊËÎÏÔÙÛÜŸA-zàâçéèêëîïôùûüÿ\s\-\.]{2,40}?)\s*(?:\(|\.|,|Réf|Ref|numéro|$)',
                msg, re.IGNORECASE)
            if de_match:
                nom = de_match.group(1).strip()
                exclus = {"mtn","momo","moov","fcfa","xof","vous","avez",
                          "votre","compte","nouveau","solde","effectué"}
                if nom.lower() not in exclus and len(nom) > 2:
                    result["nom_destinataire"] = nom

    # ── Solde restant ─────────────────────────────────────────────────────────
    # Corrigé : accepte "Solde:47088F" sans espace ET "solde : 200.000 FCFA"
    solde_match = re.search(
        r'[Ss]olde\s*:?\s*(\d[\d\s\.\,]*)\s*(?:FCFA|XOF|F\b)',
        msg, re.IGNORECASE)
    if solde_match:
        s = re.sub(r'[\s\.,]', '', solde_match.group(1))
        try: result["solde"] = int(s)
        except: pass

    # ── Frais ─────────────────────────────────────────────────────────────────
    frais_match = re.search(
        r'[Ff]rais\s*:?\s*(\d+)\s*(?:FCFA|XOF|F\b)?',
        msg, re.IGNORECASE)
    if frais_match:
        try: result["frais"] = int(frais_match.group(1))
        except: pass

    # ── Référence / ID ────────────────────────────────────────────────────────
    # Corrigé : "ID:12002441081Frais" → prendre seulement les chiffres après ID:
    id_match = re.search(r'\bID\s*[:\s]*(\d{5,25})', msg, re.IGNORECASE)
    if id_match:
        result["reference_id"] = id_match.group(1)
    else:
        ref_match = re.search(
            r'(?:Réf(?:érence)?|Ref)\s*[:\s]+([A-Z0-9]{3,25})',
            msg, re.IGNORECASE)
        if ref_match:
            result["reference_id"] = ref_match.group(1)

    # ── Date transaction ──────────────────────────────────────────────────────
    # Corrigé : accepte "2026-05-02 10:07:36" ET "2026-05-02, 10:07:36"
    date_match = re.search(
        r'(\d{4}-\d{2}-\d{2}[\s,]+\d{2}:\d{2}:\d{2})',
        msg)
    if not date_match:
        date_match = re.search(
            r'(\d{4}-\d{2}-\d{2})',
            msg)
    if not date_match:
        date_match = re.search(
            r'(\d{2}/\d{2}/\d{4}[\s]+\d{2}:\d{2})',
            msg)
    if date_match:
        result["date_transaction"] = date_match.group(1).strip()

    # ── Raison ────────────────────────────────────────────────────────────────
    if any(k in msg_lower for k in ["transfert","transfer"]):
        result["raison"] = "momo_transfert"
    elif any(k in msg_lower for k in ["vous avez reçu","vous avez recu",
                                       "avez reçu","avez recu","dépôt","depot",
                                       "credited","crédité"]):
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

    # ── Détecter l'opérateur ──────────────────────────────────────────────────
    msg_upper = msg.upper()
    if "MTN" in msg_upper or "MOMO" in msg_upper:
        result["sender"] = "MTN"
    elif "MOOV" in msg_upper:
        result["sender"] = "MOOV"
    elif "ORANGE" in msg_upper:
        result["sender"] = "ORANGE"
    elif sender and sender not in ("{sender}", "{{sender}}", "$sender",
                                    "[from]", "[sender]"):
        result["sender"] = sender
    else:
        result["sender"] = "INCONNU"

    # ── Ignorer les templates non résolus ─────────────────────────────────────
    if msg in ("{message}", "{{message}}", "$message", "[message]", ""):
        result["raison"] = "test_non_resolu"
        return result

    msg_lower = msg.lower()

    # ── Extraire le montant ───────────────────────────────────────────────────
    # Formats : 5000 FCFA / 5 000 F / 5,000 XOF / 5000F
    montant_match = re.search(
        r'(\d[\d\s,\.]*)\s*(?:FCFA|XOF|F(?:\b|CFA))',
        msg, re.IGNORECASE)
    if montant_match:
        montant_str = re.sub(r'[\s,\.]', '', montant_match.group(1))
        try:
            result["amount"] = int(float(montant_str))
        except: pass

    # ── Extraire le numéro de téléphone ───────────────────────────────────────
    # Priorité : numéro avec indicatif 229 puis sans
    phone_patterns = [
        r'\b(229\d{8,9})\b',           # 22961000000
        r'\b(\+229\d{8,9})\b',         # +22961000000
        r'(?:à|de|from|to)\s+(?:\w+\s+\w+\s+)?\((\d{8,11})\)',  # (numéro) après nom
        r'\b(0[679]\d{7,8})\b',        # 0961000000
    ]
    for pat in phone_patterns:
        m = re.search(pat, msg)
        if m:
            result["phone_number"] = m.group(1).replace("+","")
            break

    # ── Extraire la référence ─────────────────────────────────────────────────
    ref_match = re.search(
        r'(?:Réf(?:érence)?|Ref|TXN|Transaction\s*ID|ID)\s*[:\s#]*([A-Z0-9]{5,25})',
        msg, re.IGNORECASE)
    if ref_match:
        result["reference_id"] = ref_match.group(1)

    # ── Extraire le solde restant ─────────────────────────────────────────────
    solde_match = re.search(
        r'(?:[Nn]ouveau\s+solde|[Ss]olde|[Bb]alance|[Rr]estant?)\s*[:\s]*'
        r'(\d[\d\s,\.]*)\s*(?:FCFA|XOF|F\b)',
        msg, re.IGNORECASE)
    if solde_match:
        solde_str = re.sub(r'[\s,\.]', '', solde_match.group(1))
        try:
            result["solde"] = int(float(solde_str))
        except: pass

    # ── Extraire le nom du destinataire / expéditeur ──────────────────────────
    # Format spécial MTN Bénin : "Société : NOM" ou "Société:NOM"
    societe_match = re.search(r'[Ss]oci[eé]t[eé]\s*:\s*([^\.\,\n]+)', msg)
    if societe_match:
        result["nom_destinataire"] = societe_match.group(1).strip()
    else:
        nom_patterns = [
            r'(?:à|a)\s+([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ\s\-]{2,40}?)\s*(?:\(|\.|,|Ref|Nouveau|solde|$)',
            r'(?:de|from)\s+([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ\s\-\.]{2,40}?)\s*(?:\(|\.|,|Ref|Nouveau|solde|$)',
        ]
        for pat in nom_patterns:
            m = re.search(pat, msg, re.IGNORECASE)
            if m:
                nom = m.group(1).strip()
                mots_exclus = {"mtn","momo","moov","orange","fcfa","xof",
                               "vous","avez","reçu","envoyé","paiement",
                               "nouveau","solde","balance","ref","date"}
                phrases_exclues = ["effectué sur votre compte","sur votre compte",
                                   "votre compte","effectué","votre"]
                if (nom.lower() not in mots_exclus and len(nom) > 2 and
                        not any(p in nom.lower() for p in phrases_exclues)):
                    result["nom_destinataire"] = nom
                    break

    # ── Déterminer la raison ──────────────────────────────────────────────────
    if any(k in msg_lower for k in ["vous avez reçu", "vous avez recu",
                                     "credited", "a été crédité",
                                     "avez reçu", "avez recu"]):
        result["raison"] = "momo_depot"

    elif any(k in msg_lower for k in ["vous avez envoyé", "vous avez envoye",
                                       "avez envoyé", "avez envoye"]):
        result["raison"] = "momo_envoi"

    elif any(k in msg_lower for k in ["paiement effectué", "paiement effectue",
                                       "vous avez payé", "vous avez paye",
                                       "paiement de", "debited", "débité"]):
        result["raison"] = "momo_paiement"

    elif any(k in msg_lower for k in ["retrait", "withdraw", "cash out"]):
        result["raison"] = "momo_retrait"

    elif any(k in msg_lower for k in ["transfert", "transfer"]):
        result["raison"] = "momo_transfert"

    elif any(k in msg_lower for k in ["solde", "balance", "votre solde"]):
        result["raison"] = "momo_solde"

    elif result["amount"]:
        result["raison"] = "momo_transaction"

    return result


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def home():
    return {"message": "✅ API SMS Graham POS — opérationnelle"}


@app.post("/sms")
async def recevoir_sms(request: Request):
    """
    Accepte JSON ou form-data.
    Supporte les deux formats SMS Forwarder :
    - {"message": "...", "sender": "..."}
    - {"from": "...", "text": "..."}
    """
    try:
        body = await request.json()
    except:
        try:
            form = await request.form()
            body = dict(form)
        except:
            body = {}

    print("📩 Body reçu :", body)

    # Récupérer message et sender — supporte tous les formats SMS Forwarder
    message = (
        body.get("message") or
        body.get("text") or
        body.get("body") or
        body.get("sms") or
        body.get("key") or      # ← SMS Forwarder utilise "key"
        body.get("content") or
        ""
    ).strip()

    sender = (
        body.get("sender") or
        body.get("from") or
        body.get("number") or
        body.get("phone") or
        body.get("sim") or
        ""
    ).strip()

    # Si "key" contient "sortant : NUMERO\nMESSAGE", extraire le numéro et le message
    if not sender and "key" in body:
        key_val = body["key"]
        # Format: "sortant : +33767407631\nMTN MOMO: ..."
        lines = key_val.split("\n", 1)
        if len(lines) == 2:
            # Première ligne contient le numéro
            num_match = re.search(r'[\+\d]{8,15}', lines[0])
            if num_match:
                sender = num_match.group(0)
            message = lines[1].strip()
        else:
            message = key_val.strip()

    # Récupérer aussi "time" si disponible
    time_str = body.get("time", "")

    print(f"📱 Sender: {sender}")
    print(f"💬 Message: {message}")

    # Parser le SMS
    parsed = parser_sms_mtn(message, sender)
    print("✅ Parsed :", parsed)

    # Ignorer les tests non résolus
    if parsed["raison"] == "test_non_resolu":
        print("⚠️ Message template non résolu — ignoré")
        return {"status": "ignored", "reason": "template_not_resolved"}

    # Insérer dans Supabase — retry sans colonnes optionnelles si erreur
    try:
        payload = {k: v for k, v in parsed.items() if v is not None}
        payload.pop("time_received", None)  # colonne inexistante
        result  = supabase.table("transactions").insert(payload).execute()
        id_ins  = result.data[0].get("id","?") if result.data else "?"
        print(f"✅ ENREGISTRÉ DANS SUPABASE — ID: {id_ins}")
        print(f"   Montant : {parsed.get('amount','—')} FCFA")
        print(f"   Raison  : {parsed.get('raison','—')}")
        print(f"   Nom     : {parsed.get('nom_destinataire','—')}")
        print(f"   Tel     : {parsed.get('phone_number','—')}")
        print(f"   Ref     : {parsed.get('reference_id','—')}")
        return {"status": "ok", "id": id_ins, "parsed": parsed}
    except Exception as e:
        print(f"❌ Erreur Supabase (tentative 1) : {e}")
        # Retry avec seulement les colonnes de base
        try:
            payload_min = {
                "raw_message":  parsed.get("raw_message",""),
                "sender":     parsed.get("sender","INCONNU"),
                "raison":       parsed.get("raison","inconnu"),
            }
            if parsed.get("amount"):      payload_min["amount"]       = parsed["amount"]
            if parsed.get("phone_number"):payload_min["phone_number"] = parsed["phone_number"]
            if parsed.get("reference_id"):payload_min["reference_id"] = parsed["reference_id"]
            result2 = supabase.table("transactions").insert(payload_min).execute()
            id2     = result2.data[0].get("id","?") if result2.data else "?"
            print(f"✅ ENREGISTRÉ (minimal) — ID: {id2}")
            return {"status": "ok_minimal", "id": id2}
        except Exception as e2:
            print(f"❌ Erreur Supabase (tentative 2) : {e2}")
            return {"status": "error", "detail": str(e2)}


@app.post("/sms/test")
def test_sms(sms: SMS):
    """Endpoint de test pour simuler un vrai SMS MTN."""
    msg = sms.message if sms.message else \
          "MTN MOMO: Vous avez reçu 2000 FCFA de ISS service (22961000000). Référence: TXN983451. Nouveau solde: 45000 FCFA. Date: 02/05/2026 10:35"
    parsed  = parser_sms_mtn(msg, sms.sender or "MTN")
    payload = {k: v for k, v in parsed.items() if v is not None}
    result  = supabase.table("transactions").insert(payload).execute()
    print(f"✅ TEST enregistré — ID: {result.data[0].get('id','?') if result.data else '?'}")
    return {"status": "test_ok", "parsed": parsed}
