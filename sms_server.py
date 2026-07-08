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
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY         = os.environ.get("SUPABASE_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")

SEUIL_CONFIANCE_IA  = 0.75
IA_TIMEOUT_SECONDES = 8

# Client normal (respecte RLS) — pour les opérations utilisateur
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Client admin (bypass RLS) — UNIQUEMENT pour :
#   1. Vérifier merchant_code lors de l'activation
#   2. Créer/mettre à jour tracker_devices
# Ne jamais utiliser pour lire des données personnelles des commerçants
supabase_admin = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY if SUPABASE_SERVICE_KEY else SUPABASE_KEY
)

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
        res = supabase_admin.table("tracker_devices") \
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
        supabase_admin.table("tracker_devices") \
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
    Fallback regex amélioré pour SMS Mobile Money béninois.
    Supporte : 1312F, 5000F, 5 000 XOF, 5,000 FCFA, 5.000F
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

    # ── Type de transaction ────────────────────────────────────────
    if any(k in texte for k in ["reçu", "recu", "vous avez reçu", "received",
                                  "credite", "crédité", "depot recu", "cash in"]):
        result["raison"] = "momo_depot"
    elif any(k in texte for k in ["retrait", "withdrawn", "cash out"]):
        result["raison"] = "momo_retrait"
    elif any(k in texte for k in ["transfert", "transfer", "vous avez envoyé",
                                   "envoyé", "envoye", "sent to"]):
        result["raison"] = "momo_transfert"
    elif any(k in texte for k in ["paiement", "payment", "payé", "paye"]):
        result["raison"] = "momo_paiement"
    else:
        result["confiance"] = 0.60

    # ── Montant — supporte 1312F, 5 000 XOF, 5,000 FCFA, 5.000F ──
    # Ordre : chercher d'abord le montant principal (dépôt/transfert)
    # puis n'importe quel montant
    patterns_montant = [
        # "transfert 1312F" ou "dépôt 5000 XOF"
        r'(?:transfert|depot|dépôt|reçu|recu|paiement|retrait|envoy[eé])\s+(?:de\s+)?(\d[\d\s]*(?:[.,]\d+)?)\s*(?:xof|fcfa|cfa|f\b)',
        # "1312F de" ou "5 000F"
        r'\b(\d[\d\s]*(?:[.,]\d+)?)\s*(?:xof|fcfa|cfa|f)\b',
        # Montant après "montant :"
        r'montant\s*:?\s*(\d[\d\s]*(?:[.,]\d+)?)',
    ]
    for pat in patterns_montant:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            raw = re.sub(r'[\s,.]', '', m.group(1))
            try:
                val = int(raw)
                if val > 0:
                    result["amount"] = val
                    break
            except ValueError:
                pass

    # ── Solde ─────────────────────────────────────────────────────
    m_solde = re.search(
        r'(?:solde|balance|nouveau solde)\s*:?\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(?:xof|fcfa|f\b)?',
        body, re.IGNORECASE
    )
    if m_solde:
        raw = re.sub(r'[\s,.]', '', m_solde.group(1))
        try:
            result["solde"] = int(raw)
        except ValueError:
            pass

    # ── Numéro de téléphone ───────────────────────────────────────
    # Format retrait : ",2290198765," (entre virgules)
    m_tel = re.search(r',\s*(\+?[0-9]{8,13})\s*,', body)
    if m_tel:
        result["phone"] = m_tel.group(1).strip()
    else:
        m_tel2 = re.search(r'\b((?:00229|229)[679]\d{7})\b', body)
        if m_tel2:
            result["phone"] = m_tel2.group(1)
        else:
            m_tel3 = re.search(r'\b([679]\d{7})\b', body)
            if m_tel3:
                result["phone"] = m_tel3.group(1)

    # ── Nom destinataire ──────────────────────────────────────────
    # Format retrait : "recu de NOM ,"
    m_nom_ret = re.search(
        r'recu\s+de\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\-\.]{0,40}?)\s*,',
        body, re.IGNORECASE)
    if m_nom_ret:
        result["nom_destinataire"] = m_nom_ret.group(1).strip()

    if not result["nom_destinataire"]:
        # Format dépôt : "a NOM le"
        m_nom_dep = re.search(
            r'\ba\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\-\.]{0,40}?)\s+le\s+\d',
            body, re.IGNORECASE)
        if m_nom_dep:
            result["nom_destinataire"] = m_nom_dep.group(1).strip()

    if not result["nom_destinataire"]:
        # Format MFS marchand
        m_mfs = re.search(
            r'\bde\s+MFS\s+([A-Z][A-Z0-9\s\-&\.]{2,40}?)(?:\s+\d{4}|\s+Ref|,|$)',
            body, re.IGNORECASE)
        if m_mfs:
            result["nom_destinataire"] = m_mfs.group(1).strip()

    # ── Référence transaction — chercher un vrai ID numérique ─────
    # Priorité aux IDs numériques longs (vrais IDs opérateurs)
    m_id = re.search(
        r'(?:id\s*:?\s*|ref\s*:?\s*|id:\s*)(\d{6,20})',
        body, re.IGNORECASE
    )
    if m_id:
        result["reference_id"] = m_id.group(1)
    else:
        # Fallback : chercher un code alphanumérique qui n'est pas un nom de société
        m_ref = re.search(
            r'(?:ref[eé]rence?\s*:?\s*|txid\s*:?\s*)([A-Z0-9]{6,20})\b',
            body, re.IGNORECASE
        )
        if m_ref:
            result["reference_id"] = m_ref.group(1)

    # ── Frais ─────────────────────────────────────────────────────
    m_frais = re.search(
        r'(?:frais|fees)\s*:?\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(?:xof|fcfa|f\b)?',
        body, re.IGNORECASE
    )
    if m_frais:
        raw = re.sub(r'[\s,.]', '', m_frais.group(1))
        try:
            result["frais"] = int(raw)
        except ValueError:
            pass

    logger.info(f"📋 Regex: raison={result['raison']} amount={result['amount']} solde={result['solde']}")
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
    Utilise supabase_admin (service_role) pour bypass le RLS de mm_profiles.
    """
    code = payload.merchant_code.strip()
    logger.info(f"🔍 Tentative d'activation — code reçu: '{code}' device: {payload.device_id[:8]}...")

    if len(code) != 8 or not code.isdigit():
        logger.warning(f"❌ Code invalide: '{code}'")
        return {"status": "error", "message": "Code invalide — 8 chiffres requis"}

    # Chercher le commerçant — supabase_admin bypass le RLS
    try:
        res = supabase_admin.table("mm_profiles") \
                            .select("id, nom_complet, nom_entreprise, merchant_code") \
                            .eq("merchant_code", code) \
                            .execute()
        logger.info(f"🔍 Lookup mm_profiles: {len(res.data)} résultat(s) pour code '{code}'")
    except Exception as e:
        logger.error(f"❌ Erreur lookup mm_profiles: {e}")
        return {"status": "error", "message": f"Erreur serveur : {str(e)}"}

    if not res.data:
        # Debug : lister tous les codes existants pour comparaison
        try:
            all_codes = supabase_admin.table("mm_profiles") \
                                       .select("merchant_code") \
                                       .execute()
            codes_existants = [r.get("merchant_code") for r in all_codes.data if r.get("merchant_code")]
            logger.warning(f"⚠ Code '{code}' non trouvé. Codes existants: {codes_existants}")
        except Exception:
            logger.warning(f"⚠ Code '{code}' non trouvé. Impossible de lister les codes.")
        return {"status": "error", "message": "Code incorrect ou inexistant"}

    profil    = res.data[0]
    user_uuid = profil["id"]
    user_name = (profil.get("nom_complet")
                 or profil.get("nom_entreprise")
                 or "Commerçant")

    api_token = secrets.token_hex(32)

    # Enregistrer l'appareil — sans association_active (colonne optionnelle)
    device_data = {
        "device_id":   payload.device_id,
        "user_uuid":   user_uuid,
        "device_name": payload.device_name,
        "api_token":   api_token,
        "role":        "CAPTEUR",
        "is_active":   True,
        "sim_a_label": payload.sim_a_label,
        "sim_b_label": payload.sim_b_label,
        "last_seen_at": datetime.datetime.utcnow().isoformat(),
    }

    try:
        supabase_admin.table("tracker_devices") \
                      .upsert(device_data, on_conflict="device_id") \
                      .execute()
        logger.info(f"✅ Appareil enregistré: {payload.device_id[:8]}... user={user_name}")
    except Exception as e:
        logger.error(f"❌ Erreur upsert tracker_devices: {e}")
        # Tentative insert simple en fallback
        try:
            supabase_admin.table("tracker_devices").insert(device_data).execute()
            logger.info(f"✅ Appareil inséré (fallback insert): {payload.device_id[:8]}...")
        except Exception as e2:
            logger.error(f"❌ Erreur insert fallback: {e2}")
            return {"status": "error", "message": f"Impossible d'enregistrer l'appareil : {str(e2)}"}

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
        res = supabase_admin.table("tracker_devices") \
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
        supabase_admin.table("tracker_devices").update({
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
    x_device_id: Optional[str] = Header(None),
    x_app_key:   Optional[str] = Header(None),
):
    """
    Reçoit un SMS depuis l'app Android Tracker.

    Logique intelligente :
    - L'app envoie TOUJOURS ses SMS, sans vérifier l'association
    - Le serveur vérifie si ce device_id est enregistré dans tracker_devices
    - Device connu + actif → traite la transaction pour ce commerçant
    - Device inconnu → ignore silencieusement (202), pas d'erreur

    Cette approche permet à l'app de capturer immédiatement sans configuration,
    et d'envoyer au serveur dès le premier lancement.
    """

    # Vérification clé d'application minimale (anti-spam)
    APP_KEY = os.environ.get("TRACKER_APP_KEY", "GRAHAM_TRACKER_2025")
    if x_app_key and x_app_key != APP_KEY:
        raise HTTPException(status_code=401, detail="Clé application invalide")

    device_id = x_device_id or payload.device_id
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id manquant")

    # ── Vérifier si ce device est enregistré et associé ───────────
    try:
        res = supabase_admin.table("tracker_devices") \
                            .select("user_uuid, is_active, sim_a_label, sim_b_label") \
                            .eq("device_id", device_id) \
                            .execute()
    except Exception as e:
        logger.error(f"Erreur lookup tracker_devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not res.data:
        # Device inconnu — ignorer silencieusement
        logger.info(f"📵 Device inconnu {device_id[:8]}... — SMS ignoré (pas encore associé)")
        return JSONResponse(status_code=202, content={
            "status":  "ignored",
            "reason":  "device_not_registered",
            "message": "Device non enregistré — associez d'abord via Mobile Money System"
        })

    device    = res.data[0]
    user_uuid = device.get("user_uuid")

    if not device.get("is_active", False) or not user_uuid:
        logger.info(f"📵 Device {device_id[:8]}... inactif ou non associé — ignoré")
        return JSONResponse(status_code=202, content={
            "status":  "ignored",
            "reason":  "device_inactive",
            "message": "Device inactif ou non associé à un compte"
        })

    # ── Mettre à jour last_seen_at ─────────────────────────────────
    try:
        supabase_admin.table("tracker_devices") \
                      .update({"last_seen_at": datetime.datetime.utcnow().isoformat()}) \
                      .eq("device_id", device_id).execute()
    except Exception:
        pass

    # ── Déduplication robuste ─────────────────────────────────────
    # ── Déduplication : hash = device_id + timestamp + corps complet ──
    # Le timestamp garantit l'unicité même si deux SMS ont le même contenu
    # (ex: deux transferts NOWORRI du même montant à des heures différentes)
    import hashlib
    sms_hash = hashlib.md5(
        f"{device_id}|{payload.timestamp}|{payload.body}".encode()
    ).hexdigest()

    try:
        existing = supabase.table("transactions") \
                           .select("id") \
                           .eq("device_id", device_id) \
                           .eq("sms_hash",  sms_hash) \
                           .execute()
        if existing.data:
            # Vrai doublon : même appareil, même timestamp, même corps → retry app
            logger.info(f"Vrai doublon ignoré (device+timestamp+body identiques)")
            return {"status": "duplicate", "id": existing.data[0]["id"]}
    except Exception:
        pass  # Colonne absente → continuer sans déduplication

    # ── Parsing IA + fallback regex ───────────────────────────────
    parsed, source = parser_sms(payload.body, payload.sender)

    confiance = float(parsed.get("confiance", 0.85))
    raison    = parsed.get("raison",           "momo_depot")
    amount    = int(parsed.get("amount",       payload.amount or 0))
    phone     = parsed.get("phone",            payload.phone  or "")
    nom_dest  = parsed.get("nom_destinataire", "")
    reference = parsed.get("reference_id",     payload.transaction_id or "")
    solde     = int(parsed.get("solde",        0))
    frais     = int(parsed.get("frais",        0))

    statut = "pending" if (source == "ia" and confiance < SEUIL_CONFIANCE_IA) \
             else "confirmed"

    operateur  = payload.operator or _detecter_operateur(payload.sender, payload.body)
    account_map = {"MTN": 1, "MOOV": 2, "CELTIS": 3, "CELTIIS": 3}
    account_id  = account_map.get(operateur.upper(), 1)

    # ── Extraction nom et téléphone AVANT l'insert ───────────────
    body_tx = payload.body

    # Format TERRAPAY/international : "Ref:+33775958076,FR,Billy KEKE,5000"
    if not nom_dest or not phone:
        m_ref = re.search(r'Ref:\s*(\+?[0-9]+),([A-Z]{2}),([^,\n]+),',
                          body_tx, re.IGNORECASE)
        if m_ref:
            if not phone:    phone    = m_ref.group(1).strip()
            if not nom_dest: nom_dest = m_ref.group(3).strip()

    if not nom_dest:
        # Format retrait : "5000F recu de NOM ,TEL, le DATE"
        m1 = re.search(r'recu\s+de\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\-\.]{0,40}?)\s*,',
                       body_tx, re.IGNORECASE)
        if m1: nom_dest = m1.group(1).strip()

    if not nom_dest:
        # Format dépôt : "depot XXXF a NOM le DATE" ou "depot XXXF a NOM ,TEL, le DATE"
        m2 = re.search(r'\ba\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\-\.]{0,40}?)(?:\s*,|\s+le\s+\d)',
                       body_tx, re.IGNORECASE)
        if m2: nom_dest = m2.group(1).strip()

    if not nom_dest:
        # Format MFS marchand : "de MFS NOM SP 2026"
        m3 = re.search(r'\bde\s+MFS\s+([A-Z][A-Z0-9\s\-&\.]{2,40}?)(?:\s+\d{4}|\s+Ref|,|$)',
                       body_tx, re.IGNORECASE)
        if m3: nom_dest = m3.group(1).strip()

    if not phone:
        m_t1 = re.search(r',\s*(\+?[0-9]{8,13})\s*,', body_tx)
        if m_t1: phone = m_t1.group(1).strip()
        else:
            m_t2 = re.search(r'\b((?:00229|229)[679]\d{7})\b', body_tx)
            if m_t2: phone = m_t2.group(1)
            else:
                m_t3 = re.search(r'\b([679]\d{7})\b', body_tx)
                if m_t3: phone = m_t3.group(1)

    logger.info(f"📋 {operateur} {amount}F {raison} | "
                f"nom='{nom_dest or '—'}' phone='{phone or '—'}' | {statut}")

    # ── Récupérer le solde précédent AVANT l'insert (pour calcul delta) ──
    solde_precedent = 0
    if solde > 0:
        try:
            res_prev = supabase.table("transactions") \
                               .select("solde") \
                               .eq("account_id", account_id) \
                               .not_.is_("solde", "null") \
                               .gt("solde", 0) \
                               .order("created_at", desc=True) \
                               .limit(1).execute()
            if res_prev.data:
                solde_precedent = int(res_prev.data[0].get("solde") or 0)
                logger.info(f"📊 Solde précédent: {solde_precedent}F → Solde actuel: {solde}F → Delta: {solde - solde_precedent:+}F")
        except Exception as e:
            logger.warning(f"Impossible de lire le solde précédent: {e}")

    # ── Insertion en base ─────────────────────────────────────────
    insert_data = {
        "account_id":       account_id,
        "raison":           raison,
        "amount":           amount,
        "phone_number":     phone or None,
        "nom_destinataire": nom_dest or None,
        "reference_id":     reference or None,
        "solde":            solde if solde > 0 else None,
        "frais":            frais,
        "statut":           statut,
        "raw_message":      payload.body,
        "sender":           payload.sender,
        "sms_hash":         sms_hash,
    }

    optional_fields = {
        "confiance_ia":   confiance,
        "source_parsing": source,
        "device_id":      device_id,
        "user_uuid":      user_uuid,
        "sim_label":      payload.sim_label or None,
        "sim_slot":       payload.sim_slot if payload.sim_slot != -1 else None,
        "direction":      payload.direction or "IN",
        "sms_timestamp":  payload.timestamp or None,
    }

    try:
        res_ins = supabase.table("transactions").insert(
            {**insert_data, **optional_fields}
        ).execute()
    except Exception as e:
        logger.warning(f"Insert complet échoué ({e}) — tentative minimale")
        try:
            res_ins = supabase.table("transactions").insert(insert_data).execute()
        except Exception as e2:
            logger.error(f"Erreur insertion: {e2}")
            raise HTTPException(status_code=500, detail=str(e2))

    tx_id_str = str(res_ins.data[0]["id"]) if res_ins.data else ""
    logger.info(f"✅ Transaction id={tx_id_str} | {operateur} {amount}F {raison}")

    # ── Mise à jour cash physique ─────────────────────────────────
    if amount > 0 and statut == "confirmed":
        try:
            maj_current_cash(
                account_id      = account_id,
                amount          = amount,
                raison          = raison,
                solde_nouveau   = solde,
                solde_ancien    = solde_precedent,
                transaction_id  = tx_id_str
            )
        except Exception as e_cash:
            logger.error(f"Erreur maj cash: {e_cash}")

    return {
        "status":   "success",
        "id":       tx_id_str,
        "statut":   statut,
        "raison":   raison,
        "amount":   amount,
        "source":   source,
        "message":  "Transaction enregistrée"
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

@app.get("/api/debug/cash/{account_id}")
def debug_cash(account_id: int):
    """
    Diagnostic rapide de l'état du cash pour un compte.
    Appeler depuis le navigateur pour vérifier sans envoyer de SMS.
    """
    try:
        res = supabase_admin.table("cash_sessions").select("*") \
                      .eq("account_id", account_id) \
                      .eq("actif", True) \
                      .gt("opening_cash", 0) \
                      .order("created_at", desc=True) \
                      .limit(1).execute()
        session = res.data[0] if res.data else None
    except Exception as e:
        session = None

    try:
        mvs = supabase.table("cash_movements").select("*") \
                      .eq("account_id", account_id) \
                      .order("created_at", desc=True) \
                      .limit(5).execute()
        mouvements = mvs.data or []
    except Exception:
        mouvements = []

    return {
        "account_id":     account_id,
        "session_active": session is not None,
        "session":        session,
        "derniers_mouvements": mouvements,
        "message": "Session active ✅" if session else
                   "❌ Aucune session active — saisir cash départ dans Mobile Money System"
    }


@app.post("/api/test/cash")
def test_maj_cash(account_id: int = 1, amount: int = 1000, type_op: str = "DEPOT"):
    """
    Tester manuellement la mise à jour cash.
    type_op: DEPOT ou RETRAIT
    """
    raison = "momo_depot" if type_op == "DEPOT" else "momo_retrait"
    maj_current_cash(account_id, amount, raison, transaction_id="TEST")
    return {"status": "ok", "type_op": type_op, "amount": amount,
            "message": f"Test {type_op} {amount}F exécuté — voir les logs Render"}


def debug_code(code: str):
    """
    Endpoint de diagnostic — vérifie si un code existe dans mm_profiles.
    À utiliser depuis le navigateur pour diagnostiquer les problèmes d'association.
    Exemple : https://graham-sms-server.onrender.com/api/debug/code/12345678
    """
    try:
        res = supabase_admin.table("mm_profiles") \
                            .select("id, nom_complet, nom_entreprise, merchant_code") \
                            .eq("merchant_code", code.strip()) \
                            .execute()

        # Compter le total des profils
        total = supabase_admin.table("mm_profiles").select("id, merchant_code").execute()
        nb_total = len(total.data) if total.data else 0
        codes_existants = [
            r.get("merchant_code", "NULL")
            for r in (total.data or [])
        ]

        return {
            "code_recherché":   code.strip(),
            "trouvé":          len(res.data) > 0,
            "résultat":        res.data,
            "total_profils":   nb_total,
            "codes_existants": codes_existants,
            "supabase_admin_ok": True
        }
    except Exception as e:
        return {
            "code_recherché":    code.strip(),
            "trouvé":           False,
            "erreur":           str(e),
            "supabase_admin_ok": False
        }



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

def reseau_est_actif(account_id: int) -> bool:
    """Vérifie si le réseau est ON dans cash_sessions."""
    from datetime import datetime, timezone, timedelta
    paris     = timezone(timedelta(hours=2))
    now_paris = datetime.now(paris)
    debut_utc = now_paris.replace(hour=0, minute=0, second=0, microsecond=0) \
                         .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        res = supabase_admin.table("cash_sessions").select("actif") \
                      .eq("account_id", account_id) \
                      .gte("created_at", debut_utc) \
                      .gt("opening_cash", 0) \
                      .order("created_at", desc=False) \
                      .limit(1).execute()
        if res.data:
            val = res.data[0].get("actif", True)
            if val is False:
                return False
        return True
    except Exception as e:
        logger.error(f"reseau_est_actif error: {e}")
        return True


def maj_current_cash(account_id: int, amount: int, raison: str,
                     solde_nouveau: int = 0, solde_ancien: int = 0,
                     transaction_id: str = ""):
    """
    Logique agent Mobile Money :
      DEPOT  (client donne cash → merchant crédite SIM) :
        physique +amount | virtuel -amount
      RETRAIT (client retire cash ← merchant débite SIM) :
        physique -amount | virtuel +amount

    Détection par delta SIM en priorité, fallback sur raison.
    """
    if amount <= 0:
        logger.info(f"⏭ Cash ignoré — amount={amount}")
        return

    # ── 1. Déterminer le type ────────────────────────────────────
    if solde_nouveau > 0 and solde_ancien > 0:
        delta_sim = solde_nouveau - solde_ancien
        if delta_sim < 0:
            type_op = "DEPOT"    # SIM a diminué → merchant a envoyé mobile → reçu cash
            dp = +amount; dv = -amount
        elif delta_sim > 0:
            type_op = "RETRAIT"  # SIM a augmenté → merchant a reçu mobile → donné cash
            dp = -amount; dv = +amount
        else:
            logger.info("⏭ Delta SIM = 0 → pas de maj cash")
            return
        logger.info(f"💡 {type_op} détecté via solde SIM ({solde_ancien}→{solde_nouveau})")
    else:
        if raison == "momo_depot":
            type_op = "DEPOT";   dp = +amount; dv = -amount
        elif raison in ("momo_retrait","momo_transfert","momo_paiement","momo_envoi"):
            type_op = "RETRAIT"; dp = -amount; dv = +amount
        else:
            logger.info(f"⏭ raison={raison} — pas de maj cash")
            return
        logger.info(f"💡 {type_op} détecté via raison={raison}")

    # ── 2. Vérifier réseau ON ─────────────────────────────────────
    if not reseau_est_actif(account_id):
        logger.info(f"⏭ Réseau {account_id} OFF"); return

    # ── 3. Chercher la session active (sans vérification de date stricte) ─
    try:
        res = supabase_admin.table("cash_sessions").select("*") \
                      .eq("account_id", account_id) \
                      .eq("actif", True) \
                      .gt("opening_cash", 0) \
                      .order("created_at", desc=True) \
                      .limit(1).execute()
    except Exception as e:
        logger.error(f"Erreur lecture session: {e}"); return

    if not res.data:
        logger.info("⏭ Aucune session active — saisir cash départ dans Mobile Money System")
        return

    sess = res.data[0]
    sess_id = sess["id"]

    # Cash physique
    current_p = float(sess.get("current_cash") or sess.get("opening_cash") or 0)
    nouveau_p = max(0.0, current_p + dp)

    # Cash virtuel
    current_v = float(sess.get("current_virtuel") or sess.get("opening_virtuel") or 0)
    if current_v == 0 and solde_ancien > 0:
        current_v = float(solde_ancien)
    nouveau_v = float(solde_nouveau) if solde_nouveau > 0 else max(0.0, current_v + dv)

    logger.info(
        f"💵 {type_op} {amount}F | "
        f"Physique: {current_p:.0f}→{nouveau_p:.0f} | "
        f"Virtuel: {current_v:.0f}→{nouveau_v:.0f}"
    )

    # ── 4. Mettre à jour la session ───────────────────────────────
    try:
        supabase_admin.table("cash_sessions").update({
            "current_cash":    nouveau_p,
            "current_virtuel": nouveau_v,
        }).eq("id", sess_id).execute()
    except Exception as e:
        logger.error(f"Erreur update session cash: {e}"); return

    # ── 5. Enregistrer mouvement immuable ─────────────────────────
    mv_base = {
        "account_id": account_id,
        "amount":     dp,
        "type":       type_op,
        "cash_apres": nouveau_p,
    }
    mv_ext = {
        "type_operation":   type_op,
        "ancien_physique":  current_p,
        "nouveau_physique": nouveau_p,
        "ancien_virtuel":   current_v,
        "nouveau_virtuel":  nouveau_v,
    }
    if transaction_id:
        mv_base["transaction_id"] = transaction_id
    try:
        supabase_admin.table("cash_movements").insert({**mv_base, **mv_ext}).execute()
    except Exception:
        try:
            supabase_admin.table("cash_movements").insert(mv_base).execute()
        except Exception as e2:
            logger.error(f"Erreur insert mouvement: {e2}")


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
