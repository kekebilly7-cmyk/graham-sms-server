import os
import re
import json as _json
import urllib.request as _req
import urllib.error as _uerr

from fastapi import FastAPI, Request
from pydantic import BaseModel
from supabase import create_client

# ── Chargement .env local (dev) ──────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ════════════════════════════════════════════════════════════════════
# CONFIGURATION — variables d'environnement uniquement
# ════════════════════════════════════════════════════════════════════
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY", "")
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")

CLAUDE_SEUIL_CONFIANCE = 0.75   # En dessous → statut pending

if not CLAUDE_API_KEY:
    print("⚠️  CLAUDE_API_KEY non définie → fallback classique uniquement")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌  SUPABASE_URL ou SUPABASE_KEY non définie !")

supabase    = create_client(SUPABASE_URL, SUPABASE_KEY)
ACCOUNT_IDS = {"MTN": 1, "MOOV": 2, "CELTIS": 3, "ORANGE": 4}

app = FastAPI()


class SMS(BaseModel):
    message: str = ""
    sender:  str = ""


# ════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════

def est_financier(msg: str) -> bool:
    a_montant = bool(re.search(r'\d+\s*(?:FCFA|XOF|F\b)', msg, re.IGNORECASE))
    mots = ["transfert","transfer","depot","dépôt","reçu","recu",
            "envoyé","envoye","retrait","withdraw","paiement","solde",
            "momo","credited","debited"]
    a_mot = any(m in msg.lower() for m in mots)
    return a_montant and a_mot


def reseau_est_actif(account_id: int) -> bool:
    """
    Vérifie si le réseau est ON (actif=true) dans cash_sessions.
    Si aucune session du jour → considéré actif (pas encore ouvert).
    Si session trouvée et actif=False → réseau OFF, on n'update pas le cash.
    """
    from datetime import datetime, timezone, timedelta
    paris     = timezone(timedelta(hours=2))
    utc       = timezone.utc
    now_paris = datetime.now(paris)
    debut_paris = now_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    debut_utc   = debut_paris.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        res = supabase.table("cash_sessions").select("actif") \
                      .eq("account_id", account_id) \
                      .gte("created_at", debut_utc) \
                      .gt("opening_cash", 0) \
                      .order("created_at", desc=False) \
                      .limit(1).execute()
        if res.data:
            # actif peut être None (colonne pas encore remplie) → traiter comme True
            valeur = res.data[0].get("actif", True)
            if valeur is False:
                return False
        return True
    except Exception as e:
        print(f"[reseau_actif] erreur : {e}")
        return True   # en cas d'erreur → ne pas bloquer


# ════════════════════════════════════════════════════════════════════
# LOGIQUE 1 — ANALYSE IA (Claude API)
# ════════════════════════════════════════════════════════════════════

def analyser_sms_ia(message: str) -> dict | None:
    if not CLAUDE_API_KEY:
        print("[IA] Clé API non configurée → fallback classique")
        return None
    if not message or not message.strip():
        return None

    prompt = f"""Tu es un expert en SMS Mobile Money Bénin/Afrique de l'Ouest (MTN, MOOV, CELTIS, ORANGE).
Analyse ce SMS et retourne UNIQUEMENT un objet JSON valide, sans texte avant ni après, sans markdown.

SMS :
\"\"\"{message}\"\"\"

Règles de classification pour le champ "raison" :
- "momo_depot"      : argent reçu sur le compte (crédit, dépôt, transfert entrant, cash in)
- "momo_retrait"    : argent retiré (cash out, retrait agence)
- "momo_transfert"  : argent envoyé à quelqu'un (transfert sortant)
- "momo_paiement"   : paiement marchand, achat, facture payée
- "momo_envoi"      : envoi d'argent (variante transfert)
- "momo_annulation" : transaction annulée ou échouée ou remboursée
- "momo_transaction": transaction générique si type indéterminé
- "inconnu"         : impossible à classifier

Retourne exactement ce JSON (tous les champs obligatoires) :
{{
  "raison":           "momo_depot",
  "sender":           "MTN",
  "amount":           25000,
  "frais":            150,
  "solde":            45000,
  "phone_number":     "22901234567",
  "nom_destinataire": "Jean KEKE",
  "reference_id":     "TXN123456",
  "confiance":        0.97
}}

Règles :
- amount, frais, solde : entiers en FCFA (0 si absent)
- phone_number : format 229XXXXXXXXX ou "" si absent
- confiance : float 0.0 à 1.0 selon ta certitude
- sender : "MTN", "MOOV", "CELTIS", "ORANGE" ou "MTN" par défaut"""

    try:
        payload = _json.dumps({
            "model": "claude-haiku-4-5",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}]
        }).encode("utf-8")

        request = _req.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload, method="POST",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
            }
        )
        with _req.urlopen(request, timeout=8) as resp:
            data = _json.loads(resp.read().decode())

        texte = ""
        for bloc in data.get("content", []):
            if bloc.get("type") == "text":
                texte += bloc["text"]

        texte = texte.strip()
        if "```" in texte:
            for p in texte.split("```"):
                p = p.strip()
                if p.startswith("json"): p = p[4:].strip()
                if p.startswith("{"): texte = p; break

        resultat = _json.loads(texte.strip())

        for c in ["raison","sender","amount","frais","solde",
                  "phone_number","nom_destinataire","reference_id","confiance"]:
            if c not in resultat:
                print(f"[IA] Champ manquant : {c}")
                return None

        resultat["amount"]    = int(resultat.get("amount",    0) or 0)
        resultat["frais"]     = int(resultat.get("frais",     0) or 0)
        resultat["solde"]     = int(resultat.get("solde",     0) or 0)
        resultat["confiance"] = float(resultat.get("confiance", 0) or 0)

        if not resultat.get("phone_number"):     resultat["phone_number"]     = None
        if not resultat.get("nom_destinataire"): resultat["nom_destinataire"] = None
        if not resultat.get("reference_id"):     resultat["reference_id"]     = None
        if not resultat.get("frais"):            resultat["frais"]            = 0
        if not resultat.get("solde"):            resultat["solde"]            = None

        print(f"[IA] ✅ {resultat['raison']} | "
              f"{resultat['amount']} F | confiance={resultat['confiance']:.0%}")
        return resultat

    except (_uerr.URLError, _uerr.HTTPError, TimeoutError) as e:
        print(f"[IA] ⚠️  Réseau/API indisponible → fallback classique ({e})")
        return None
    except (_json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[IA] ⚠️  Réponse invalide → fallback classique ({e})")
        return None
    except Exception as e:
        print(f"[IA] ⚠️  Erreur inattendue → fallback classique ({e})")
        return None


# ════════════════════════════════════════════════════════════════════
# LOGIQUE 2 — ANALYSE CLASSIQUE (original intact)
# ════════════════════════════════════════════════════════════════════

def parser_sms_classique(message: str, sender: str) -> dict:
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


# ════════════════════════════════════════════════════════════════════
# FONCTION PRINCIPALE — IA d'abord, classique en fallback
# ════════════════════════════════════════════════════════════════════

def parser_sms(message: str, sender: str) -> dict:
    msg = message.strip()

    resultat_ia = analyser_sms_ia(msg)

    if resultat_ia is not None and resultat_ia.get("confiance", 0) >= CLAUDE_SEUIL_CONFIANCE:
        print(f"[Parser] ✅ Source : IA | raison={resultat_ia['raison']}")
        classique = parser_sms_classique(msg, sender)
        return {
            "raw_message":      msg,
            "sender":           resultat_ia.get("sender", "MTN"),
            "account_id":       ACCOUNT_IDS.get(resultat_ia.get("sender", "MTN")),
            "phone_number":     resultat_ia.get("phone_number"),
            "amount":           resultat_ia["amount"] if resultat_ia["amount"] > 0 else None,
            "reference_id":     resultat_ia.get("reference_id"),
            "nom_destinataire": resultat_ia.get("nom_destinataire"),
            "solde":            resultat_ia.get("solde"),
            "frais":            resultat_ia.get("frais", 0),
            "date_transaction": classique.get("date_transaction"),
            "raison":           resultat_ia["raison"],
            "source_analyse":   "ia",
            "confiance_ia":     resultat_ia["confiance"],
            # statut déterminé par la confiance
            "statut":           "confirmed",
        }

    # Fallback classique
    raison_fallback = "faible confiance" if resultat_ia else "IA indisponible"
    print(f"[Parser] ⚠️  Fallback classique ({raison_fallback})")
    classique = parser_sms_classique(msg, sender)
    classique["source_analyse"] = "classique"
    classique["confiance_ia"]   = resultat_ia.get("confiance", 0.0) if resultat_ia else 0.0
    # Si confiance faible → pending, si IA indispo → confirmed (classique fiable)
    if resultat_ia and resultat_ia.get("confiance", 0) < CLAUDE_SEUIL_CONFIANCE:
        classique["statut"] = "pending"
        print(f"[Parser] 🟡 Confiance {resultat_ia['confiance']:.0%} < 75% → statut PENDING")
    else:
        classique["statut"] = "confirmed"
    return classique


# ════════════════════════════════════════════════════════════════════
# maj_current_cash — vérifie reseau_est_actif avant de toucher au cash
# ════════════════════════════════════════════════════════════════════

def maj_current_cash(account_id: int, amount: int, raison: str,
                     solde_avant: int, solde_apres: int,
                     statut_transaction: str = "confirmed"):
    """
    Met à jour le cash physique UNIQUEMENT si :
    1. Le réseau est ON (actif=True dans cash_sessions)
    2. La transaction est confirmed (pas pending)
    """
    # ── Vérification ON/OFF ───────────────────────────────────────────
    if not reseau_est_actif(account_id):
        print(f"⏭️  Réseau account_id={account_id} est OFF → cash non mis à jour")
        return

    # ── Vérification statut transaction ──────────────────────────────
    if statut_transaction == "pending":
        print(f"⏭️  Transaction PENDING → cash non mis à jour (attente confirmation)")
        return

    from datetime import datetime, timezone, timedelta
    paris       = timezone(timedelta(hours=2))
    utc         = timezone.utc
    now_paris   = datetime.now(paris)
    debut_paris = now_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    debut_utc   = debut_paris.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S")

    if solde_avant is not None and solde_apres is not None and solde_avant > 0:
        diff_sim = solde_apres - solde_avant
        delta    = amount if diff_sim < 0 else -amount
    else:
        if raison == "momo_depot":
            delta = +amount
        elif raison in ("momo_retrait","momo_transfert","momo_paiement","momo_envoi"):
            delta = -amount
        else:
            print(f"⏭️  raison={raison} sans solde — ignoré")
            return

    try:
        res = supabase.table("cash_sessions").select("*") \
                      .eq("account_id", account_id) \
                      .gte("created_at", debut_utc) \
                      .gt("opening_cash", 0) \
                      .order("created_at", desc=False) \
                      .limit(1).execute()

        if res.data:
            sess    = res.data[0]
            opening = float(sess.get("opening_cash") or 0)
            if opening <= 0:
                print(f"⏭️  Solde départ non saisi — ignoré")
                return
            _cc     = sess.get("current_cash")
            current = float(_cc) if _cc is not None else opening
            if current == 0:
                print(f"⏭️  Cash épuisé (0 F) — transaction ignorée")
                return
            nouveau = max(0, current + delta)
            supabase.table("cash_sessions") \
                    .update({"current_cash": nouveau}) \
                    .eq("id", sess["id"]).execute()
            sens = f"↑ +{abs(delta)}" if delta > 0 else f"↓ -{abs(delta)}"
            print(f"💵 {sens} F | {current} → {nouveau} F | SIM {solde_avant}→{solde_apres}")
        else:
            print(f"⚠️  Aucune session cash du jour (account_id={account_id})")
    except Exception as e:
        print(f"⚠️  Erreur maj_current_cash : {e}")


# ════════════════════════════════════════════════════════════════════
# ROUTES FastAPI
# ════════════════════════════════════════════════════════════════════

@app.get("/")
def home():
    ia_status = "✅ configurée" if CLAUDE_API_KEY else "⚠️  non configurée"
    return {
        "message":   "✅ Graham POS SMS Server v5 — opérationnel",
        "ia_status": ia_status,
        "supabase":  "✅ connecté" if SUPABASE_URL else "❌ non configuré",
    }


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
               body.get("body")    or body.get("sms") or
               body.get("key")     or "").strip()
    sender  = (body.get("sender")  or body.get("from") or
               body.get("number")  or "").strip()

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

    if not est_financier(message):
        print("⏭️  SMS non financier — ignoré")
        return {"status": "ignored", "reason": "non_financier"}

    parsed = parser_sms(message, sender)
    print(f"✅ Parsed [{parsed.get('source_analyse','?')}] "
          f"statut={parsed.get('statut','?')} : {parsed}")

    if parsed["raison"] == "test_non_resolu":
        print("⚠️  Template non résolu — ignoré")
        return {"status": "ignored"}

    try:
        try:
            res_prev    = supabase.table("transactions") \
                                  .select("solde") \
                                  .eq("account_id", parsed["account_id"]) \
                                  .not_.is_("solde", "null") \
                                  .order("created_at", desc=True) \
                                  .limit(1).execute()
            solde_avant = int(res_prev.data[0]["solde"]) if res_prev.data else None
        except:
            solde_avant = None

        payload = {k: v for k, v in parsed.items() if v is not None}
        res     = supabase.table("transactions").insert(payload).execute()
        id_ins  = res.data[0].get("id","?") if res.data else "?"
        solde_apres = parsed.get("solde")
        statut_tx   = parsed.get("statut", "confirmed")

        print(f"✅ ID:{id_ins} | {parsed.get('raison')} | "
              f"{parsed.get('amount')} F | statut={statut_tx} | "
              f"source={parsed.get('source_analyse','?')}")

        # Maj cash uniquement si confirmed ET réseau actif
        if parsed.get("amount") and parsed.get("account_id"):
            maj_current_cash(
                parsed["account_id"], parsed["amount"],
                parsed["raison"], solde_avant, solde_apres,
                statut_transaction=statut_tx)

        return {
            "status":         "ok",
            "id":             id_ins,
            "statut_tx":      statut_tx,
            "source_analyse": parsed.get("source_analyse", "?"),
        }

    except Exception as e1:
        print(f"❌ Erreur : {e1}")
        try:
            p_min = {
                "raw_message": parsed.get("raw_message", ""),
                "sender":      parsed.get("sender", "MTN"),
                "account_id":  parsed.get("account_id"),
                "raison":      parsed.get("raison", "inconnu"),
                "statut":      parsed.get("statut", "confirmed"),
            }
            if parsed.get("amount"):       p_min["amount"]       = parsed["amount"]
            if parsed.get("phone_number"): p_min["phone_number"] = parsed["phone_number"]
            if parsed.get("reference_id"): p_min["reference_id"] = parsed["reference_id"]
            res2 = supabase.table("transactions").insert(p_min).execute()
            id2  = res2.data[0].get("id","?") if res2.data else "?"
            print(f"✅ minimal ID:{id2}")
            if parsed.get("amount") and parsed.get("account_id"):
                maj_current_cash(parsed["account_id"], parsed["amount"],
                                 parsed["raison"], solde_avant,
                                 parsed.get("solde"),
                                 statut_transaction=p_min["statut"])
            return {"status": "ok_minimal", "id": id2}
        except Exception as e2:
            print(f"❌ Erreur finale : {e2}")
            return {"status": "error", "detail": str(e2)}


@app.post("/sms/test")
def test_sms(sms: SMS):
    msg    = sms.message or \
             "Transfert 5000F a KEKE BILLY(22961000000) 2026-05-06 10:07:36 " \
             "Frais:0F Solde:47088F ID:12002009086"
    parsed = parser_sms(msg, sms.sender or "MTN")
    payload = {k: v for k, v in parsed.items() if v is not None}
    res  = supabase.table("transactions").insert(payload).execute()
    id_t = res.data[0].get("id","?") if res.data else "?"
    if parsed.get("amount") and parsed.get("account_id"):
        maj_current_cash(parsed["account_id"], parsed["amount"],
                         parsed["raison"], None, parsed.get("solde"),
                         statut_transaction=parsed.get("statut","confirmed"))
    print(f"✅ TEST ID:{id_t} | source={parsed.get('source_analyse','?')} "
          f"| statut={parsed.get('statut','?')}")
    return {
        "status":         "test_ok",
        "id":             id_t,
        "parsed":         parsed,
        "source_analyse": parsed.get("source_analyse","?"),
        "statut_tx":      parsed.get("statut","?"),
    }


@app.post("/sms/test-ia")
async def test_ia_seul(request: Request):
    """Teste l'analyse sans insérer en base."""
    try:
        body = await request.json()
    except:
        return {"error": "Body JSON invalide"}

    msg = body.get("message", "")
    if not msg:
        return {"error": "Champ 'message' manquant"}

    resultat_ia = analyser_sms_ia(msg)
    classique   = parser_sms_classique(msg, "")

    confiance = resultat_ia.get("confiance", 0) if resultat_ia else 0.0
    return {
        "ia":              resultat_ia,
        "classique":       classique,
        "source_utilisee": "ia" if (resultat_ia and confiance >= CLAUDE_SEUIL_CONFIANCE)
                           else "classique",
        "statut_prevu":    "confirmed" if (resultat_ia and confiance >= CLAUDE_SEUIL_CONFIANCE)
                           else ("pending" if resultat_ia else "confirmed"),
        "ia_configuree":   bool(CLAUDE_API_KEY),
    }


# ── Route pour confirmer une transaction pending depuis Graham POS ──
@app.post("/transactions/{tx_id}/confirmer")
async def confirmer_transaction(tx_id: int, request: Request):
    """
    Appelé par Graham POS quand le caissier confirme une transaction pending.
    Body : {"raison": "momo_depot"} (la raison corrigée par le caissier)
    """
    try:
        body = await request.json()
    except:
        body = {}

    nouvelle_raison = body.get("raison", "")

    try:
        # Récupérer la transaction
        res_tx = supabase.table("transactions").select("*").eq("id", tx_id).execute()
        if not res_tx.data:
            return {"error": f"Transaction {tx_id} introuvable"}

        tx = res_tx.data[0]
        raison_finale = nouvelle_raison or tx.get("raison", "momo_transaction")

        # Mettre à jour statut + raison
        supabase.table("transactions").update({
            "statut": "confirmed",
            "raison": raison_finale,
        }).eq("id", tx_id).execute()

        # Maintenant mettre à jour le cash physique
        account_id = tx.get("account_id")
        amount     = tx.get("amount") or 0
        solde      = tx.get("solde")

        if amount and account_id:
            # Récupérer solde avant
            try:
                res_prev = supabase.table("transactions") \
                                   .select("solde") \
                                   .eq("account_id", account_id) \
                                   .not_.is_("solde", "null") \
                                   .neq("id", tx_id) \
                                   .order("created_at", desc=True) \
                                   .limit(1).execute()
                solde_avant = int(res_prev.data[0]["solde"]) if res_prev.data else None
            except:
                solde_avant = None

            maj_current_cash(account_id, amount, raison_finale,
                             solde_avant, solde, statut_transaction="confirmed")

        print(f"✅ Transaction {tx_id} confirmée → raison={raison_finale}")
        return {"status": "ok", "tx_id": tx_id, "raison": raison_finale}

    except Exception as e:
        print(f"❌ Erreur confirmation {tx_id} : {e}")
        return {"status": "error", "detail": str(e)}


# ── Route pour ignorer une transaction pending ──────────────────────
@app.post("/transactions/{tx_id}/ignorer")
async def ignorer_transaction(tx_id: int):
    """Marque une transaction pending comme ignorée (cash non touché)."""
    try:
        supabase.table("transactions").update({
            "statut": "ignored"
        }).eq("id", tx_id).execute()
        print(f"✅ Transaction {tx_id} ignorée")
        return {"status": "ok", "tx_id": tx_id}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
