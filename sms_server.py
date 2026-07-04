"""
════════════════════════════════════════════════════════════════════════════
SMS SERVER — Graham POS / Mobile Money Tracker
Déployé sur : https://graham-sms-server.onrender.com
════════════════════════════════════════════════════════════════════════════

Fonctionnalités :
  - Réception des SMS Mobile Money depuis l'app Android Tracker
  - Parsing IA (Claude Haiku) avec fallback regex automatique
  - Transactions < 75% de confiance → statut "pending" (confirmation manuelle)
  - Activation / dissociation des appareils Android
  - Endpoints Graham POS (confirmation manuelle des transactions pending)
  - Health check

Tables Supabase utilisées :
  - transactions      (données Mobile Money)
  - cash_sessions     (sessions de caisse par réseau)
  - cash_movements    (mouvements de caisse)
  - tracker_devices   (appareils Android associés)
  - mm_profiles       (profils commerçants + merchant_code)
"""

import os
import re
import json
import secrets
import logging
import datetime
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SEUIL_CONFIANCE_IA = 0.75   # En dessous → pending (confirmation manuelle)
IA_TIMEOUT_SECONDES = 8

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Client Claude Haiku (IA principale)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ════════════════════════════════════════════════════════════════════════════
# APP FASTAPI
# ════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Graham SMS Server",
    description="Serveur de réception SMS Mobile Money — Graham POS / Tracker Android",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ════════════════════════════════════════════════════════════════════════════
# MODÈLES PYDANTIC
# ════════════════════════════════════════════════════════════════════════════

class SmsPayload(BaseModel):
    """SMS reçu depuis l'app Android Tracker."""
    device_id:      str
    sender:         str
    body:           str
    timestamp:      int
    sim_slot:       int  = -1
    subscription_id:int  = -1
    sim_label:      str  = ""
    operator:       str  = ""
    amount:         float = 0.0
    phone:          str  = ""
    transaction_id: str  = ""
    direction:      str  = "IN"
    received_at:    int  = 0

class ActivationRequest(BaseModel):
    """Demande d'activation depuis l'app Android."""
    merchant_code: str
    device_id:     str
    device_name:   str = "Mon téléphone"
    sim_a_label:   str = ""
    sim_b_label:   str = ""

class ConfirmationRequest(BaseModel):
    """Confirmation manuelle d'une transaction pending (depuis Graham POS)."""
    raison: str  # momo_depot, momo_retrait, momo_transfert, momo_paiement, momo_envoi

# ════════════════════════════════════════════════════════════════════════════
# AUTHENTIFICATION — vérification du token Tracker
# ════════════════════════════════════════════════════════════════════════════

def verifier_token_tracker(authorization: str) -> dict:
    """
    Vérifie le Bearer token d'un appareil Android dans tracker_devices.
    Retourne les infos de l'appareil (device_id, user_uuid) si valide.
    Lève HTTPException 401 sinon.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token manquant")

    token = authorization.replace("Bearer ", "").strip()

    try:
        res = supabase.table("tracker_devices") \
                      .select("device_id, user_uuid, is_active, device_name") \
                      .eq("api_token", token) \
                      .execute()
    except Exception as e:
        logger.error(f"Erreur vérification token: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not res.data:
        raise HTTPException(status_code=401, detail="Token invalide ou appareil inconnu")

    device = res.data[0]
    if not device.get("is_active", False):
        raise HTTPException(status_code=403, detail="Appareil désactivé")

    # Mettre à jour last_seen_at en arrière-plan (best-effort)
    try:
        supabase.table("tracker_devices") \
                .update({"last_seen_at": datetime.datetime.utcnow().isoformat()}) \
                .eq("api_token", token) \
                .execute()
    except Exception:
        pass

    return device

# ════════════════════════════════════════════════════════════════════════════
# PARSING IA — Claude Haiku avec fallback regex
# ════════════════════════════════════════════════════════════════════════════

def parser_sms_avec_ia(body: str, sender: str) -> dict:
    """
    Parse un SMS Mobile Money avec Claude Haiku.

    Retourne un dict avec :
        raison, amount, phone, nom_destinataire,
        reference_id, solde, frais, confiance
    Retourne None si l'IA échoue ou timeout.

    La logique : IA d'abord (8s timeout, 75% seuil de confiance).
    Si l'IA échoue → fallback automatique sur le regex classique.
    Si l'IA réussit mais confiance < 75% → transaction stockée en "pending"
    pour confirmation manuelle dans Graham POS.
    """
    if not claude_client:
        logger.warning("Claude API non configuré — fallback regex")
        return None

    prompt = f"""Analyse ce SMS Mobile Money et extrais les informations.

SMS reçu de : {sender}
Contenu : {body}

Réponds UNIQUEMENT en JSON valide avec ces champs exactement :
{{
  "raison": "momo_depot|momo_retrait|momo_transfert|momo_paiement|momo_envoi",
  "amount": <montant en nombre entier, 0 si non trouvé>,
  "phone": "<numéro de téléphone de la contrepartie, vide si absent>",
  "nom_destinataire": "<nom affiché, vide si absent>",
  "reference_id": "<référence/ID de transaction, vide si absent>",
  "solde": <solde après transaction en nombre entier, 0 si non trouvé>,
  "frais": <frais de transaction en nombre entier, 0 si non trouvé>,
  "confiance": <score de confiance entre 0.0 et 1.0>
}}

Règles :
- momo_depot = argent reçu sur la SIM (dépôt entrant)
- momo_retrait = argent retiré en espèces
- momo_transfert = envoi d'argent vers un autre numéro
- momo_paiement = paiement d'un service ou marchand
- momo_envoi = envoi depuis ton numéro vers autre numéro
- confiance = ta certitude sur la classification (1.0 = certitude totale)
- Si le SMS est ambigu ou incomplet, baisse la confiance en dessous de 0.75

Ne réponds qu'avec le JSON, aucun texte autour."""

    try:
        import signal

        def timeout_handler(signum, frame):
            raise TimeoutError("IA timeout")

        # Sur Linux (Render), on peut utiliser signal.alarm
        try:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(IA_TIMEOUT_SECONDES)
        except (AttributeError, OSError):
            pass  # Windows ne supporte pas SIGALRM

        response = claude_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )

        try:
            signal.alarm(0)  # Annuler le timeout
        except (AttributeError, OSError):
            pass

        texte = response.content[0].text.strip()
        # Nettoyer les backticks éventuels
        if texte.startswith("```"):
            texte = texte.split("```")[1]
            if texte.startswith("json"):
                texte = texte[4:]
        texte = texte.strip()

        resultat = json.loads(texte)
        logger.info(f"✅ IA parsed: raison={resultat.get('raison')} confiance={resultat.get('confiance')}")
        return resultat

    except TimeoutError:
        logger.warning(f"⏱ IA timeout après {IA_TIMEOUT_SECONDES}s — fallback regex")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"⚠ IA JSON invalide: {e} — fallback regex")
        return None
    except Exception as e:
        logger.error(f"❌ IA erreur: {e} — fallback regex")
        return None


def parser_sms_regex(body: str, sender: str) -> dict:
    """
    Fallback regex pour parser un SMS Mobile Money béninois.
    Toujours disponible, confiance fixe à 0.85 si trouvé.

    Supporte MTN MoMo, Moov Money (Flooz), Celtiis Cash.
    Format montant : "5000F", "5 000 XOF", "5,000 FCFA", "1312F"
    """
    texte = body.lower()
    result = {
        "raison":           "momo_depot",
        "amount":           0,
        "phone":            "",
        "nom_destinataire": "",
        "reference_id":     "",
        "solde":            0,
        "frais":            0,
        "confiance":        0.85
    }

    # ── Détection du type de transaction ──────────────────────────
    if any(k in texte for k in ["reçu", "recu", "depot", "dépôt", "received", "credite", "crédité"]):
        result["raison"] = "momo_depot"
    elif any(k in texte for k in ["retrait", "withdrawn", "retire", "retiré"]):
        result["raison"] = "momo_retrait"
    elif any(k in texte for k in ["transfert", "transfer", "envoyé", "envoye", "sent"]):
        result["raison"] = "momo_transfert"
    elif any(k in texte for k in ["paiement", "payment", "payé", "paye", "achat"]):
        result["raison"] = "momo_paiement"
    else:
        result["confiance"] = 0.60  # Ambigu → pending

    # ── Montant ───────────────────────────────────────────────────
    # Supporte : 1312F, 5000 XOF, 5,000 FCFA, 5 000F, 1.312 F
    patterns_montant = [
        r'(\d[\d\s,.]*)\s*(?:xof|fcfa|cfa)\b',
        r'(\d[\d\s,.]*)\s*f\b',
        r'montant\s*:?\s*(\d[\d\s,.]*)',
    ]
    for pat in patterns_montant:
        m = re.search(pat, texte, re.IGNORECASE)
        if m:
            raw = re.sub(r'[\s,.\']', '', m.group(1))
            try:
                result["amount"] = int(raw)
                break
            except ValueError:
                pass

    # ── Solde ─────────────────────────────────────────────────────
    m_solde = re.search(
        r'(?:solde|balance|nouveau solde)\s*:?\s*(\d[\d\s,.]*)\s*(?:xof|fcfa|f\b)?',
        texte, re.IGNORECASE
    )
    if m_solde:
        raw = re.sub(r'[\s,.\']', '', m_solde.group(1))
        try:
            result["solde"] = int(raw)
        except ValueError:
            pass

    # ── Numéro de téléphone ───────────────────────────────────────
    m_phone = re.search(
        r'(?:de|à|from|to|vers|destinataire)?\s*[+]?(?:229)?\s*([679]\d[\s]?\d{2}[\s]?\d{2}[\s]?\d{2})',
        body
    )
    if m_phone:
        result["phone"] = re.sub(r'\s', '', m_phone.group(1))

    # ── Référence transaction ─────────────────────────────────────
    m_ref = re.search(
        r'(?:txid|ref[:\s#]|id[:\s#]|reference[:\s#])\s*([A-Z0-9\-]{4,20})',
        body, re.IGNORECASE
    )
    if m_ref:
        result["reference_id"] = m_ref.group(1)

    # ── Frais ─────────────────────────────────────────────────────
    m_frais = re.search(
        r'(?:frais|fees|commission)\s*:?\s*(\d[\d\s,.]*)\s*(?:xof|fcfa|f\b)?',
        texte, re.IGNORECASE
    )
    if m_frais:
        raw = re.sub(r'[\s,.\']', '', m_frais.group(1))
        try:
            result["frais"] = int(raw)
        except ValueError:
            pass

    logger.info(f"📋 Regex parsed: raison={result['raison']} amount={result['amount']}")
    return result


def parser_sms(body: str, sender: str) -> tuple[dict, str]:
    """
    Orchestration IA + fallback regex.

    Retourne (résultat_parsing, source) où source = "ia" ou "regex".

    Logique :
    1. Tenter l'IA (Claude Haiku) avec timeout 8s
    2. Si l'IA réussit ET confiance >= 75% → utiliser le résultat IA
    3. Si l'IA réussit MAIS confiance < 75% → utiliser résultat IA mais
       la transaction sera stockée en "pending" pour confirmation manuelle
    4. Si l'IA échoue (timeout, erreur, JSON invalide) → fallback regex
    """
    resultat_ia = parser_sms_avec_ia(body, sender)

    if resultat_ia is not None:
        return resultat_ia, "ia"
    else:
        # Fallback regex
        resultat_regex = parser_sms_regex(body, sender)
        return resultat_regex, "regex"

# ════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health_check():
    """Vérification que le serveur est en ligne."""
    return {
        "status":    "ok",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "version":   "2.0.0",
        "ia_active": claude_client is not None
    }


# ────────────────────────────────────────────────────────────────────────────
# ACTIVATION / DISSOCIATION — app Android Tracker
# ────────────────────────────────────────────────────────────────────────────

@app.post("/api/activate")
def activer_tracker(payload: ActivationRequest):
    """
    Associe un téléphone Android à un compte Mobile Money System.

    L'app Android envoie le code à 8 chiffres visible dans les Paramètres
    du logiciel Mobile Money System PC. On vérifie ce code dans mm_profiles,
    on génère un token unique pour ce téléphone, et on crée l'entrée dans
    tracker_devices. Ce token sera utilisé pour authentifier tous les SMS
    envoyés par ce téléphone.
    """
    code = payload.merchant_code.strip()

    if len(code) != 8 or not code.isdigit():
        return {"status": "error", "message": "Code invalide — 8 chiffres requis"}

    # Chercher le commerçant propriétaire de ce code
    try:
        res = supabase.table("mm_profiles") \
                      .select("id, nom_complet, nom_entreprise") \
                      .eq("merchant_code", code) \
                      .execute()
    except Exception as e:
        logger.error(f"Erreur lookup merchant_code: {e}")
        return {"status": "error", "message": f"Erreur serveur : {str(e)}"}

    if not res.data:
        return {"status": "error", "message": "Code incorrect ou inexistant"}

    profil     = res.data[0]
    user_uuid  = profil["id"]
    user_name  = (profil.get("nom_complet")
                  or profil.get("nom_entreprise")
                  or "Commerçant")

    # Générer un token sécurisé unique pour ce téléphone
    api_token = secrets.token_hex(32)

    # Enregistrer l'appareil
    try:
        supabase.table("tracker_devices").upsert({
            "device_id":          payload.device_id,
            "user_uuid":          user_uuid,
            "device_name":        payload.device_name,
            "api_token":          api_token,
            "role":               "CAPTEUR",
            "is_active":          True,
            "association_active": True,
            "sim_a_label":        payload.sim_a_label,
            "sim_b_label":        payload.sim_b_label,
            "last_seen_at":       datetime.datetime.utcnow().isoformat(),
        }, on_conflict="device_id").execute()
    except Exception as e:
        logger.error(f"Erreur enregistrement device: {e}")
        return {"status": "error", "message": f"Impossible d'enregistrer l'appareil : {str(e)}"}

    logger.info(f"✅ Activation: device={payload.device_id[:8]}... user={user_name}")
    return {
        "status":     "success",
        "api_token":  api_token,
        "user_uuid":  user_uuid,
        "user_name":  user_name,
        "message":    f"Téléphone associé au compte {user_name}"
    }


@app.post("/api/dissociate")
def dissocier_tracker(
    device_id: str,
    authorization: Optional[str] = Header(None)
):
    """
    Invalide l'association d'un téléphone Android.
    L'historique des transactions est conservé dans Supabase.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token manquant")

    token = authorization.replace("Bearer ", "").strip()

    # Vérifier que ce token correspond bien à ce device
    try:
        res = supabase.table("tracker_devices") \
                      .select("device_id") \
                      .eq("device_id", device_id) \
                      .eq("api_token", token) \
                      .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not res.data:
        raise HTTPException(status_code=403, detail="Token ou device_id invalide")

    # Invalider l'association
    try:
        supabase.table("tracker_devices").update({
            "is_active":          False,
            "association_active": False,
            "api_token":          None,
            "user_uuid":          None,
        }).eq("device_id", device_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    logger.info(f"🔓 Dissociation: device={device_id[:8]}...")
    return {"status": "success", "message": "Téléphone dissocié avec succès"}


# ────────────────────────────────────────────────────────────────────────────
# RÉCEPTION SMS — depuis l'app Android Tracker
# ────────────────────────────────────────────────────────────────────────────

@app.post("/api/transactions/sms", status_code=201)
def recevoir_sms(
    payload: SmsPayload,
    authorization: Optional[str] = Header(None)
):
    """
    Reçoit un SMS Mobile Money capturé par l'app Android Tracker.

    Authentification : token Bearer généré lors de l'activation.
    Le token identifie l'appareil et le commerçant (user_uuid).

    Parsing : IA Claude Haiku en premier, fallback regex si échec.
    Confiance < 75% → statut "pending" (alerte rouge dans Graham POS).
    """

    # ── Authentification ──────────────────────────────────────────
    device_info = verifier_token_tracker(authorization)
    user_uuid   = device_info.get("user_uuid")

    if not user_uuid:
        raise HTTPException(
            status_code=403,
            detail="Appareil non associé à un compte"
        )

    # ── Déduplication ─────────────────────────────────────────────
    if payload.transaction_id:
        try:
            existing = supabase.table("transactions") \
                               .select("id") \
                               .eq("reference_id",  payload.transaction_id) \
                               .eq("device_id",     payload.device_id) \
                               .execute()
            if existing.data:
                return {
                    "status":  "duplicate",
                    "id":      existing.data[0]["id"],
                    "message": "Transaction déjà reçue"
                }
        except Exception:
            pass

    # ── Parsing IA + fallback regex ───────────────────────────────
    parsed, source = parser_sms(payload.body, payload.sender)

    confiance   = float(parsed.get("confiance", 0.85))
    raison      = parsed.get("raison",           "momo_depot")
    amount      = int(parsed.get("amount",       payload.amount or 0))
    phone       = parsed.get("phone",            payload.phone  or "")
    nom_dest    = parsed.get("nom_destinataire", "")
    reference   = parsed.get("reference_id",     payload.transaction_id or "")
    solde       = int(parsed.get("solde",        0))
    frais       = int(parsed.get("frais",        0))

    # Statut selon confiance IA
    if source == "ia" and confiance < SEUIL_CONFIANCE_IA:
        statut = "pending"
        logger.warning(
            f"⚠ Transaction pending — confiance IA trop basse: {confiance:.0%}"
        )
    else:
        statut = "confirmed"

    # Déterminer account_id selon l'opérateur
    operateur   = payload.operator or _detecter_operateur(payload.sender, payload.body)
    account_map = {"MTN": 1, "MOOV": 2, "CELTIS": 3, "CELTIIS": 3}
    account_id  = account_map.get(operateur.upper(), 1)

    # ── Insertion en base ─────────────────────────────────────────
    try:
        res = supabase.table("transactions").insert({
            "account_id":       account_id,
            "user_uuid":        user_uuid,
            "device_id":        payload.device_id,
            "raison":           raison,
            "amount":           amount,
            "phone_number":     phone,
            "nom_destinataire": nom_dest,
            "reference_id":     reference,
            "solde":            solde,
            "frais":            frais,
            "statut":           statut,
            "confiance_ia":     confiance,
            "source_parsing":   source,
            "raw_message":      payload.body,
            "sender":           payload.sender,
            "sim_label":        payload.sim_label,
            "sim_slot":         payload.sim_slot,
            "direction":        payload.direction,
            "sms_timestamp":    payload.timestamp,
        }).execute()
    except Exception as e:
        logger.error(f"Erreur insertion transaction: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    tx_id = res.data[0]["id"] if res.data else None

    logger.info(
        f"📱 SMS reçu: {operateur} {amount}F "
        f"statut={statut} source={source} confiance={confiance:.0%}"
    )

    return {
        "status":       "success",
        "id":           tx_id,
        "statut":       statut,
        "raison":       raison,
        "amount":       amount,
        "confiance_ia": confiance,
        "source":       source,
        "message":      "Transaction enregistrée"
    }


# ────────────────────────────────────────────────────────────────────────────
# CONFIRMATION MANUELLE — depuis Graham POS (transactions pending)
# ────────────────────────────────────────────────────────────────────────────

@app.post("/transactions/{transaction_id}/confirmer")
def confirmer_transaction(
    transaction_id: int,
    payload: ConfirmationRequest
):
    """
    Confirme manuellement une transaction en statut 'pending'.
    Appelé depuis Graham POS quand le caissier choisit le bon type.

    Cette fonction existait dans la version précédente — conservée et
    étendue pour mettre à jour aussi sim_label si disponible.
    """
    raisons_valides = {
        "momo_depot", "momo_retrait", "momo_transfert",
        "momo_paiement", "momo_envoi", "ignored"
    }
    if payload.raison not in raisons_valides:
        raise HTTPException(
            status_code=400,
            detail=f"Raison invalide. Valeurs acceptées : {raisons_valides}"
        )

    nouveau_statut = "confirmed" if payload.raison != "ignored" else "ignored"

    try:
        res = supabase.table("transactions").update({
            "raison":  payload.raison,
            "statut":  nouveau_statut,
        }).eq("id", transaction_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not res.data:
        raise HTTPException(status_code=404, detail="Transaction introuvable")

    logger.info(f"✅ Transaction #{transaction_id} confirmée: {payload.raison}")
    return {
        "status":  "ok",
        "id":      transaction_id,
        "raison":  payload.raison,
        "statut":  nouveau_statut,
        "message": "Transaction confirmée"
    }


# ────────────────────────────────────────────────────────────────────────────
# LISTE DES TRANSACTIONS PENDING — pour Graham POS
# ────────────────────────────────────────────────────────────────────────────

@app.get("/transactions/pending")
def lister_pending(account_id: int = 0):
    """
    Retourne les transactions en attente de confirmation manuelle.
    Utilisé par Graham POS pour afficher le badge rouge et les alertes.
    """
    try:
        query = supabase.table("transactions") \
                        .select("*") \
                        .eq("statut", "pending") \
                        .order("created_at", desc=True)
        if account_id > 0:
            query = query.eq("account_id", account_id)
        res = query.execute()
        return {"pending": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════════════════════════════════════════
# UTILITAIRES INTERNES
# ════════════════════════════════════════════════════════════════════════════

def _detecter_operateur(sender: str, body: str) -> str:
    """Détecte l'opérateur Mobile Money depuis l'expéditeur et le corps du SMS."""
    combined = f"{sender} {body}".upper()
    if "MTN" in combined or "MOMO" in combined:
        return "MTN"
    elif "MOOV" in combined or "FLOOZ" in combined:
        return "MOOV"
    elif "CELTIIS" in combined or "CELTIS" in combined:
        return "CELTIS"
    return "MTN"  # défaut


# ════════════════════════════════════════════════════════════════════════════
# DÉMARRAGE
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
